import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App from "./App";
import { resetPollIntervalMs, setPollIntervalMs } from "./polling";

const HEALTH = {
  status: "ok",
  service: "LiveReview",
  version: "0.1.3",
  environment: "development",
  database: "ok",
  storage: "configured",
  storage_missing: [],
};

const UPLOAD_CREATED = {
  upload_id: "u1",
  task_id: "t1",
  filename: "live.ts",
  size: 20,
  chunk_size: 10,
  total_chunks: 2,
  received_chunks: [],
  status: "uploading",
};

function jsonResponse(status: number, body: unknown): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: () => Promise.resolve(body),
  } as Response;
}

function taskResponse(
  status: string,
  error: string | null = null,
  metadata: unknown = null,
  coverage: unknown = null,
  clips: unknown[] = [],
) {
  return {
    id: "t1",
    kind: "ingest",
    status,
    progress: status === "succeeded" ? 100 : 0,
    filename: "live.ts",
    object_key: "liverreview/original/live_1_abcd.ts",
    size: 20,
    error,
    metadata,
    coverage,
    clips,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
  };
}

const METADATA = {
  format_name: "mpegts",
  duration_seconds: 3725,
  video_codec: "h264",
  width: 1920,
  height: 1080,
  frame_rate: "25/1",
  audio_codec: "aac",
  sample_rate: 48000,
  channels: 2,
  stream_count: 2,
  bit_rate: 3500000,
  content_hash: "a".repeat(64),
  probed_at: "2026-01-01T00:00:01Z",
};

const COVERAGE = {
  clip_count: 2,
  checked_at: "2026-01-01T00:10:00Z",
  issues: [] as Array<{ code: string; message: string }>,
  source_format: "mov,mp4,m4a,3gp,3g2,mj2",
  source_duration_seconds: 3725,
};

function clipResponse(overrides: Record<string, unknown> = {}) {
  return {
    index: 0,
    start_seconds: 0,
    end_seconds: 1862.5,
    duration_seconds: 1862.5,
    size_bytes: 512 * 1024 * 1024,
    status: "uploaded",
    error: null,
    object_key: "liverreview/clip/clip_000_1_abcd.mp4",
    download_url: "https://mock-tos.local/bucket/liverreview/clip/clip_000.mp4?X-Tos-Expires=3600",
    ...overrides,
  };
}

/** 用可组合的路由表替换 fetch：每个断言只关心自己那条链路的响应。 */
function mockApi(routes: Array<(url: string, init?: RequestInit) => Response | undefined>) {
  globalThis.fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    for (const route of routes) {
      const response = route(url, init);
      if (response) return response;
    }
    throw new Error(`未覆盖的请求：${url}`);
  }) as unknown as typeof fetch;
}

const healthRoute = (url: string) => (url.startsWith("/health") ? jsonResponse(200, HEALTH) : undefined);
const createRoute = (url: string) =>
  url === "/api/uploads" ? jsonResponse(201, UPLOAD_CREATED) : undefined;
const chunkRoute = (url: string) => (url.includes("/chunks/") ? jsonResponse(204, null) : undefined);

function selectFile() {
  const input = screen.getByLabelText("选择录屏文件");
  const file = new File([new Uint8Array(20)], "live.ts", { type: "video/mp2t" });
  fireEvent.change(input, { target: { files: [file] } });
}

describe("上传与任务链路", () => {
  const originalFetch = globalThis.fetch;

  beforeEach(() => {
    globalThis.fetch = vi.fn();
    window.location.hash = "";
    // 缩短轮询间隔，用例不必等待真实的 2 秒
    setPollIntervalMs(10);
  });

  afterEach(() => {
    globalThis.fetch = originalFetch;
    resetPollIntervalMs();
    vi.restoreAllMocks();
  });

  it("初始展示上传入口与说明", () => {
    mockApi([healthRoute]);
    render(<App />);
    expect(screen.getByLabelText("选择录屏文件")).toBeInTheDocument();
    expect(screen.getByText(/选择一场直播录屏/)).toBeInTheDocument();
  });

  it("分片上传完成后轮询到任务成功", async () => {
    let polls = 0;
    mockApi([
      healthRoute,
      createRoute,
      chunkRoute,
      (url) => (url.endsWith("/complete") ? jsonResponse(200, { object_key: "k", size: 20 }) : undefined),
      (url) => {
        if (!url.startsWith("/api/tasks/")) return undefined;
        polls += 1;
        return jsonResponse(200, taskResponse(polls === 1 ? "processing" : "succeeded"));
      },
    ]);

    render(<App />);
    selectFile();

    await waitFor(() => expect(screen.getByText(/任务：已完成/)).toBeInTheDocument());
    expect(screen.getByText(/liverreview\/original\/live_1_abcd\.ts/)).toBeInTheDocument();
  });

  it("上传过程中展示字节进度与百分比", async () => {
    // 用未决的分片请求把界面钉在「上传中」，观察进度呈现
    let releaseChunks: (() => void) | undefined;
    const held = new Promise<void>((resolve) => {
      releaseChunks = resolve;
    });

    mockApi([
      healthRoute,
      createRoute,
      (url) => {
        if (!url.includes("/chunks/")) return undefined;
        // 分片请求挂起，直到用例显式放行
        return new Promise<Response>((resolve) => {
          void held.then(() => resolve(jsonResponse(204, null)));
        }) as unknown as Response;
      },
    ]);

    render(<App />);
    selectFile();

    expect(await screen.findByText(/^上传中 0%/)).toBeInTheDocument();
    expect(screen.getByRole("progressbar")).toHaveAttribute("aria-valuenow", "0");
    // 上传期间文件选择被禁用，避免并发切换导致的分片串号
    expect(screen.getByLabelText("选择录屏文件")).toBeDisabled();

    releaseChunks?.();
  });

  it("分片被拒时展示后端给出的原因并可重新发起", async () => {
    mockApi([
      healthRoute,
      createRoute,
      (url) =>
        url.includes("/chunks/")
          ? jsonResponse(400, { detail: "分片 0 大小异常：收到 5 字节，期望 10 字节" })
          : undefined,
    ]);

    render(<App />);
    selectFile();

    expect(await screen.findByText("上传未完成")).toBeInTheDocument();
    expect(screen.getByText(/大小异常/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "重新发起上传" })).toBeInTheDocument();
  });

  it("缺片时提示缺片数量", async () => {
    mockApi([
      healthRoute,
      createRoute,
      chunkRoute,
      (url) =>
        url.endsWith("/complete")
          ? jsonResponse(409, {
              detail: {
                message: "还有 1 个分片未上传",
                missing_chunks: [1],
                received_chunks: [0],
                total_chunks: 2,
              },
            })
          : undefined,
    ]);

    render(<App />);
    selectFile();

    expect(await screen.findByText(/还有 1 个分片未上传/)).toBeInTheDocument();
    expect(screen.getByText(/缺少 1 个分片/)).toBeInTheDocument();
  });

  it("任务失败时展示原因，重试后继续轮询到成功", async () => {
    let taskPolls = 0;
    mockApi([
      healthRoute,
      createRoute,
      chunkRoute,
      (url) => (url.endsWith("/complete") ? jsonResponse(200, { object_key: "k", size: 20 }) : undefined),
      (url) => (url.endsWith("/retry") ? jsonResponse(200, taskResponse("uploaded")) : undefined),
      (url) => {
        if (!url.startsWith("/api/tasks/")) return undefined;
        taskPolls += 1;
        // 首次轮询为失败；重试之后的轮询返回成功
        return taskPolls === 1
          ? jsonResponse(200, taskResponse("failed", "StorageServerError: request_id=mock-0001"))
          : jsonResponse(200, taskResponse("succeeded"));
      },
    ]);

    render(<App />);
    selectFile();

    expect(await screen.findByText(/失败原因：.*request_id=mock-0001/)).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "重新执行任务" }));

    // 重试必须真正重启轮询：同一次上传内点重试不能停在中途状态
    await waitFor(() => expect(screen.getByText(/任务：已完成/)).toBeInTheDocument());
    expect(screen.queryByText(/失败原因/)).not.toBeInTheDocument();
  });

  it("任务完成后可删除已上传视频并回到初始态", async () => {
    const deleted: string[] = [];
    mockApi([
      healthRoute,
      createRoute,
      chunkRoute,
      (url) => (url.endsWith("/complete") ? jsonResponse(200, { object_key: "k", size: 20 }) : undefined),
      // DELETE 分支必须排在任务查询之前：两者共用同一路径，后者会把删除请求也接走
      (url, init) => {
        if (init?.method === "DELETE") {
          deleted.push(url);
          return jsonResponse(204, null);
        }
        return undefined;
      },
      (url) => (url.startsWith("/api/tasks/") ? jsonResponse(200, taskResponse("succeeded")) : undefined),
    ]);
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);

    render(<App />);
    selectFile();

    const button = await screen.findByRole("button", { name: "删除视频" });
    fireEvent.click(button);

    await waitFor(() => expect(deleted).toEqual(["/api/tasks/t1"]));
    expect(confirm).toHaveBeenCalled();
    // 删除后回到初始态：不再残留任务状态与对象键
    await waitFor(() => expect(screen.getByText(/选择一场直播录屏/)).toBeInTheDocument());
    expect(screen.queryByText(/对象键/)).not.toBeInTheDocument();
  });

  it("删除确认被取消时不发起请求", async () => {
    let deleteCalls = 0;
    mockApi([
      healthRoute,
      createRoute,
      chunkRoute,
      (url) => (url.endsWith("/complete") ? jsonResponse(200, { object_key: "k", size: 20 }) : undefined),
      (url, init) => {
        if (init?.method === "DELETE") {
          deleteCalls += 1;
          return jsonResponse(204, null);
        }
        return url.startsWith("/api/tasks/") ? jsonResponse(200, taskResponse("succeeded")) : undefined;
      },
    ]);
    vi.spyOn(window, "confirm").mockReturnValue(false);

    render(<App />);
    selectFile();

    fireEvent.click(await screen.findByRole("button", { name: "删除视频" }));

    await waitFor(() => expect(window.confirm).toHaveBeenCalled());
    expect(deleteCalls).toBe(0);
    expect(screen.getByRole("button", { name: "删除视频" })).toBeInTheDocument();
  });

  it("删除失败时展示后端原因，任务状态仍可见", async () => {
    mockApi([
      healthRoute,
      createRoute,
      chunkRoute,
      (url) => (url.endsWith("/complete") ? jsonResponse(200, { object_key: "k", size: 20 }) : undefined),
      (url, init) => {
        if (init?.method === "DELETE") {
          return jsonResponse(502, { detail: "删除失败：mock server failure，request_id=mock-request-id-0001" });
        }
        return url.startsWith("/api/tasks/") ? jsonResponse(200, taskResponse("succeeded")) : undefined;
      },
    ]);
    vi.spyOn(window, "confirm").mockReturnValue(true);

    render(<App />);
    selectFile();

    fireEvent.click(await screen.findByRole("button", { name: "删除视频" }));

    expect(await screen.findByText(/删除失败：.*request_id=mock-request-id-0001/)).toBeInTheDocument();
    expect(screen.getByText(/任务：已完成/)).toBeInTheDocument();
  });

  it("切换到服务状态页展示存储状态", async () => {
    mockApi([healthRoute]);
    render(<App />);
    fireEvent.click(screen.getByRole("button", { name: "服务状态" }));
    expect(await screen.findByText(/对象存储：已配置/)).toBeInTheDocument();
  });

  it("探测成功后展示媒体信息，便于核对与实际媒体是否一致", async () => {
    mockApi([
      healthRoute,
      createRoute,
      chunkRoute,
      (url) => (url.endsWith("/complete") ? jsonResponse(200, { object_key: "k", size: 20 }) : undefined),
      (url) =>
        url.startsWith("/api/tasks/")
          ? jsonResponse(200, taskResponse("succeeded", null, METADATA))
          : undefined,
    ]);

    render(<App />);
    selectFile();

    await waitFor(() => expect(screen.getByText(/任务：已完成/)).toBeInTheDocument());
    expect(screen.getByText("1:02:05")).toBeInTheDocument();
    expect(screen.getByText("1920×1080")).toBeInTheDocument();
    expect(screen.getByText("25/1")).toBeInTheDocument();
    expect(screen.getByText("h264")).toBeInTheDocument();
    expect(screen.getByText("aac · 48.0 kHz · 立体声")).toBeInTheDocument();
    expect(screen.getByText("mpegts")).toBeInTheDocument();
  });

  it("元数据字段缺失时显示占位而不是空值", async () => {
    // TS 录屏常见：没有帧率、没有分辨率、没有音轨，不能显示成 0 或空白
    const sparse = {
      ...METADATA,
      duration_seconds: null,
      width: null,
      height: null,
      frame_rate: null,
      audio_codec: null,
      sample_rate: null,
      channels: null,
      format_name: null,
    };

    mockApi([
      healthRoute,
      createRoute,
      chunkRoute,
      (url) => (url.endsWith("/complete") ? jsonResponse(200, { object_key: "k", size: 20 }) : undefined),
      (url) =>
        url.startsWith("/api/tasks/")
          ? jsonResponse(200, taskResponse("succeeded", null, sparse))
          : undefined,
    ]);

    render(<App />);
    selectFile();

    await waitFor(() => expect(screen.getByText(/任务：已完成/)).toBeInTheDocument());
    const metadata = screen.getByLabelText("媒体信息");
    expect(metadata.querySelectorAll("dd")).toHaveLength(6);
    for (const value of Array.from(metadata.querySelectorAll("dd"))) {
      expect(value.textContent).not.toBe("");
    }
    expect(screen.queryByText("1920×1080")).not.toBeInTheDocument();
  });

  it("未探测的任务不展示媒体信息", async () => {
    mockApi([
      healthRoute,
      createRoute,
      chunkRoute,
      (url) => (url.endsWith("/complete") ? jsonResponse(200, { object_key: "k", size: 20 }) : undefined),
      (url) => (url.startsWith("/api/tasks/") ? jsonResponse(200, taskResponse("processing")) : undefined),
    ]);

    render(<App />);
    selectFile();

    await waitFor(() => expect(screen.getByText(/任务：处理中/)).toBeInTheDocument());
    expect(screen.queryByLabelText("媒体信息")).not.toBeInTheDocument();
  });

  it("探测失败时展示失败原因与重试入口", async () => {
    mockApi([
      healthRoute,
      createRoute,
      chunkRoute,
      (url) => (url.endsWith("/complete") ? jsonResponse(200, { object_key: "k", size: 20 }) : undefined),
      (url) =>
        url.startsWith("/api/tasks/")
          ? jsonResponse(
              200,
              taskResponse("failed", "ProbeError: ffprobe 探测失败（退出码 1）：Invalid data found")
            )
          : undefined,
    ]);

    render(<App />);
    selectFile();

    expect(await screen.findByText(/失败原因：ProbeError/)).toBeInTheDocument();
    expect(screen.getAllByText(/Invalid data found/).length).toBeGreaterThan(0);
    expect(screen.getByRole("button", { name: "重新执行任务" })).toBeInTheDocument();
    expect(screen.queryByLabelText("媒体信息")).not.toBeInTheDocument();
  });
});

describe("切分结果展示", () => {
  const originalFetch = globalThis.fetch;

  beforeEach(() => {
    globalThis.fetch = vi.fn();
  });

  afterEach(() => {
    globalThis.fetch = originalFetch;
    vi.restoreAllMocks();
  });

  function mockTask(task: unknown, status = 200) {
    mockApi([
      healthRoute,
      createRoute,
      chunkRoute,
      (url) => (url.endsWith("/complete") ? jsonResponse(200, { object_key: "k", size: 20 }) : undefined),
      (url) => (url.startsWith("/api/tasks/") ? jsonResponse(status, task) : undefined),
    ]);
  }

  it("展示片段数、总时长与覆盖校验通过", async () => {
    mockTask(
      taskResponse("succeeded", null, METADATA, COVERAGE, [
        clipResponse({ index: 0 }),
        clipResponse({ index: 1, start_seconds: 1862.5, end_seconds: 3725, duration_seconds: 1862.5 }),
      ])
    );

    render(<App />);
    selectFile();

    expect(await screen.findByText(/任务：已完成/)).toBeInTheDocument();
    const split = screen.getByLabelText("切分结果");
    const values = Array.from(split.querySelectorAll("dd")).map((node) => node.textContent);
    expect(values).toEqual(["2", "1:02:05", "通过"]);
  });

  it("按原视频时间顺序列出片段，并给出可下载链接", async () => {
    mockTask(
      taskResponse("succeeded", null, METADATA, COVERAGE, [
        clipResponse({ index: 0 }),
        clipResponse({ index: 1, start_seconds: 1862.5, end_seconds: 3725 }),
      ])
    );

    render(<App />);
    selectFile();

    await waitFor(() => expect(screen.getByText(/任务：已完成/)).toBeInTheDocument());
    const table = screen.getByRole("table");
    const rows = Array.from(table.querySelectorAll("tbody tr"));
    expect(rows).toHaveLength(2);
    // 序号从 1 起按时间顺序递增，区间与时间轴一致
    expect(rows[0].querySelector("td")?.textContent).toBe("1");
    expect(rows[1].querySelector("td")?.textContent).toBe("2");
    expect(rows[0].textContent).toContain("0:00 ~ 31:03");
    expect(rows[1].textContent).toContain("31:03 ~ 1:02:05");
    expect(table.querySelectorAll("a")).toHaveLength(2);
  });

  it("覆盖校验有问题时逐条展示", async () => {
    mockTask(
      taskResponse("failed", "SplitError: 覆盖校验发现 1 处问题", METADATA, {
        ...COVERAGE,
        issues: [{ code: "gap", message: "第 1 片（止于 1862.5s）与第 2 片（起于 1900.0s）之间缺失 37.5s" }],
      }, [clipResponse()])
    );

    render(<App />);
    selectFile();

    await waitFor(() => expect(screen.getByText(/失败原因：SplitError/)).toBeInTheDocument());
    expect(screen.getByText(/缺失 37.5s/)).toBeInTheDocument();
    expect(screen.getByLabelText("切分结果").textContent).toContain("1 处问题");
  });

  it("片段上传失败时展示该片段的原因", async () => {
    mockTask(
      taskResponse("failed", "StorageServerError: 上传失败", METADATA, COVERAGE, [
        clipResponse({ status: "failed", error: "code=NoSuchBucket，request_id=abc" }),
      ])
    );

    render(<App />);
    selectFile();

    await waitFor(() => expect(screen.getByText(/失败原因：StorageServerError/)).toBeInTheDocument());
    expect(screen.getByText(/request_id=abc/)).toBeInTheDocument();
  });

  it("未切分的任务不展示切分结果", async () => {
    mockTask(taskResponse("processing", null, METADATA));

    render(<App />);
    selectFile();

    await waitFor(() => expect(screen.getByText(/任务：处理中/)).toBeInTheDocument());
    expect(screen.queryByLabelText("切分结果")).not.toBeInTheDocument();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });

  it("段数为零时不渲染空表格", async () => {
    mockTask(
      taskResponse("succeeded", null, METADATA, {
        ...COVERAGE,
        clip_count: 0,
        issues: [{ code: "missing_end", message: "没有任何片段，整场视频未被覆盖" }],
      }, [])
    );

    render(<App />);
    selectFile();

    await waitFor(() => expect(screen.getByText(/任务：已完成/)).toBeInTheDocument());
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
    expect(screen.getByText(/整场视频未被覆盖/)).toBeInTheDocument();
  });
});

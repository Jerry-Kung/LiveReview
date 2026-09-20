import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App from "./App";
import { resetPollIntervalMs, setPollIntervalMs } from "./polling";
import { SESSION } from "./test/fixtures";

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

/** 上传会话查到一半：第 0 片已在服务端，第 1 片还缺。 */
const UPLOAD_STATUS_HALF = { ...UPLOAD_CREATED, received_chunks: [0], received_bytes: 10, error: null };

/** 上传会话分片已收齐，但入库还没跑完。 */
const UPLOAD_STATUS_FULL = {
  ...UPLOAD_CREATED,
  received_chunks: [0, 1],
  received_bytes: 20,
  error: null,
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
function mockApi(
  routes: Array<(url: string, init?: RequestInit) => Response | undefined>,
  tasks: unknown[] = []
) {
  globalThis.fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    // 会话与任务列表都是壳层启动时就发的：默认给已登录 + 空列表，用例不必逐个声明
    if (url === "/api/auth/session") return jsonResponse(200, SESSION);
    if (url.startsWith("/api/tasks?")) return jsonResponse(200, { items: tasks });
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

/**
 * 渲染应用并等过「正在载入」。
 *
 * 登录态存在 HttpOnly Cookie 里，前端读不到，因此壳层挂载后要先问一次后端才能决定
 * 显示登录页还是工作台。这一步是异步的，直接对页面断言会撞上还没落定的载入态。
 */
async function renderApp() {
  render(<App />);
  await waitFor(() => expect(screen.queryByText("正在载入…")).not.toBeInTheDocument());
}

function selectFile() {
  const input = screen.getByLabelText("选择录屏文件");
  const file = new File([new Uint8Array(20)], "live.ts", { type: "video/mp2t" });
  fireEvent.change(input, { target: { files: [file] } });
}

/**
 * 切到某个子页。
 *
 * V0.4.1 起任务详情拆成三个子页，识别与复盘的内容不再与切片同屏：涉及它们的用例
 * 必须显式切页，否则断言的是「没切过去时看不到」——那不是被验证的行为。
 *
 * 子页要先等到开放（`aria-disabled` 摘掉）再点：子导航三项常显，未开放的点不动，
 * 点早了什么也不会发生。
 */
async function gotoSection(name: "视频信息" | "内容理解" | "复盘分析") {
  let link: HTMLElement | null = null;
  // 子导航在任务详情出现之后才存在（上传阶段显示的是上传页），因此先等它渲染出来，
  // 再等它开放：未开放的项点不动，点早了什么也不会发生。
  await waitFor(() => {
    link = screen.queryByRole("link", { name: new RegExp(`^${name}`) });
    expect(link).not.toBeNull();
    expect(link).not.toHaveAttribute("aria-disabled", "true");
  });
  fireEvent.click(link as unknown as HTMLElement);
}

describe("上传与任务链路", () => {
  const originalFetch = globalThis.fetch;

  beforeEach(() => {
    globalThis.fetch = vi.fn();
    window.location.hash = "";
    // 未完成上传的凭据存在 localStorage 里：逐用例清空，免得互相污染
    window.localStorage.clear();
    // 缩短轮询间隔，用例不必等待真实的 2 秒
    setPollIntervalMs(10);
  });

  afterEach(() => {
    globalThis.fetch = originalFetch;
    resetPollIntervalMs();
    window.localStorage.clear();
    vi.restoreAllMocks();
  });

  it("初始展示上传入口与说明", async () => {
    mockApi([healthRoute]);
    await renderApp();
    // 挂载时会先查一次有没有没传完的上传，上传入口在那之后才出现
    expect(await screen.findByText(/上传一场直播录屏/)).toBeInTheDocument();
    expect(screen.getByLabelText("选择录屏文件")).toBeInTheDocument();
  });

  it("重开页面后接着上次没传完的上传", async () => {
    // 上一次传了 1/2 片就关了页面：服务端留着第 0 片，重开页面提示继续
    window.localStorage.setItem(
      "livereview.pending-upload",
      JSON.stringify({
        upload_id: "u1",
        task_id: "t1",
        filename: "live.ts",
        size: 20,
        phase: "uploading",
        saved_at: Date.now(),
      })
    );

    const resumedChunks: string[] = [];
    mockApi([
      healthRoute,
      (url) => (url === "/api/uploads/u1" ? jsonResponse(200, UPLOAD_STATUS_HALF) : undefined),
      (url) => {
        if (!url.includes("/chunks/")) return undefined;
        resumedChunks.push(url);
        return jsonResponse(204, null);
      },
    ]);

    await renderApp();

    // 提示里带上已传体积，用户能确认这就是上次那次上传
    expect(await screen.findByText(/这次上传还没传完/)).toBeInTheDocument();
    expect(screen.getByText(/已传 10 B \/ 20 B/)).toBeInTheDocument();
    expect(screen.queryByText(/上传一场直播录屏/)).not.toBeInTheDocument();

    // 补传只发缺的那一片：第 0 片服务端已经有，不该再传一遍
    const input = screen.getByLabelText("选择同一个文件继续上传");
    const file = new File([new Uint8Array(20)], "live.ts", { type: "video/mp2t" });
    fireEvent.change(input, { target: { files: [file] } });

    await waitFor(() => expect(resumedChunks.some((url) => url.endsWith("/chunks/1"))).toBe(true));
    expect(resumedChunks.some((url) => url.endsWith("/chunks/0"))).toBe(false);
  });

  it("分片已收齐但没入库时，重开页面直接把完成请求补上", async () => {
    window.localStorage.setItem(
      "livereview.pending-upload",
      JSON.stringify({
        upload_id: "u1",
        task_id: "t1",
        filename: "live.ts",
        size: 20,
        phase: "assembling",
        saved_at: Date.now(),
      })
    );

    let completed = 0;
    mockApi([
      healthRoute,
      (url) => (url === "/api/uploads/u1" ? jsonResponse(200, UPLOAD_STATUS_FULL) : undefined),
      (url) => {
        if (!url.endsWith("/complete")) return undefined;
        completed += 1;
        return jsonResponse(200, { object_key: "k", size: 20 });
      },
      (url) => (url.startsWith("/api/tasks/") ? jsonResponse(200, taskResponse("processing")) : undefined),
    ]);

    await renderApp();

    // 不必再选一次文件：分片已经全在服务端，剩下的是后端的事
    await waitFor(() => expect(completed).toBe(1));
    // 进入任务详情：处理流程一栏给出当前状态
    expect(await screen.findByText(/任务：处理中/)).toBeInTheDocument();
    // 凭据用完即清，下次打开页面不该再捡起这次上传
    expect(window.localStorage.getItem("livereview.pending-upload")).toBeNull();
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

    await renderApp();
    selectFile();

    await waitFor(() => expect(screen.getByText(/任务：已完成/)).toBeInTheDocument());
    // 任务头只给用户可读的摘要，内部对象键不出现在页面上
    expect(screen.getByRole("heading", { name: "live.ts" })).toBeInTheDocument();
    expect(screen.queryByText(/liverreview\//)).not.toBeInTheDocument();
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

    await renderApp();
    selectFile();

    expect(await screen.findByText("上传中")).toBeInTheDocument();
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

    await renderApp();
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

    await renderApp();
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

    await renderApp();
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

    await renderApp();
    selectFile();

    const button = await screen.findByRole("button", { name: "删除视频" });
    fireEvent.click(button);

    await waitFor(() => expect(deleted).toEqual(["/api/tasks/t1"]));
    expect(confirm).toHaveBeenCalled();
    // 删除后回到初始态：不再残留任务状态与对象键
    await waitFor(() => expect(screen.getByText(/上传一场直播录屏/)).toBeInTheDocument());
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

    await renderApp();
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

    await renderApp();
    selectFile();

    fireEvent.click(await screen.findByRole("button", { name: "删除视频" }));

    expect(await screen.findByText(/删除失败：.*request_id=mock-request-id-0001/)).toBeInTheDocument();
    expect(screen.getByText(/任务：已完成/)).toBeInTheDocument();
  });

  it("页眉不出现后端、对象存储与版本这些内部字样", async () => {
    mockApi([healthRoute]);
    await renderApp();

    await waitFor(() => expect(screen.getByText(/还没有任务/)).toBeInTheDocument());
    expect(screen.queryByText("后端")).not.toBeInTheDocument();
    expect(screen.queryByText("对象存储")).not.toBeInTheDocument();
    expect(screen.queryByText("版本")).not.toBeInTheDocument();
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

    await renderApp();
    selectFile();

    await waitFor(() => expect(screen.getByText(/任务：已完成/)).toBeInTheDocument());
    // 时长用等宽数字呈现，便于与源文件逐项核对
    expect(screen.getByText("1:02:05")).toBeInTheDocument();
    expect(screen.getAllByText(/1920×1080/).length).toBeGreaterThan(0);
    expect(screen.getByText("25/1")).toBeInTheDocument();
    expect(screen.getByText("h264")).toBeInTheDocument();
    expect(screen.getByText("aac")).toBeInTheDocument();
    expect(screen.getByText("48.0 kHz")).toBeInTheDocument();
    expect(screen.getByText("立体声")).toBeInTheDocument();
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

    await renderApp();
    selectFile();

    await waitFor(() => expect(screen.getByText(/任务：已完成/)).toBeInTheDocument());
    const metadata = screen.getByLabelText("媒体信息");
    expect(metadata.querySelectorAll("dd")).toHaveLength(9);
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

    await renderApp();
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

    await renderApp();
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
    // 路由写在 hash 上：逐用例复位，免得上一个用例留下的视图影响下一个
    window.location.hash = "";
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

    await renderApp();
    selectFile();

    try {
      expect(await screen.findByText(/任务：已完成/)).toBeInTheDocument();
    } finally {
      require("fs").writeFileSync("dbg.json", (screen.getByRole("main") as HTMLElement).innerHTML, "utf8");
    }
    const split = screen.getByLabelText("切分结果", { selector: "section" });
    const values = Array.from(split.querySelectorAll("dd")).map((node) => node.textContent);
    expect(values).toEqual(["2", "1:02:05", "通过"]);
  });

  it("按原视频时间顺序列出片段，并给出在整场中的位置", async () => {
    mockTask(
      taskResponse("succeeded", null, METADATA, COVERAGE, [
        clipResponse({ index: 0 }),
        clipResponse({ index: 1, start_seconds: 1862.5, end_seconds: 3725 }),
      ])
    );

    await renderApp();
    selectFile();

    await waitFor(() => expect(screen.getByText(/任务：已完成/)).toBeInTheDocument());
    // 页面自 V0.2 起有两张表（切片与识别明细），这里精确定位切片表
    const table = document.querySelector(".clips--split") as HTMLTableElement;
    const rows = Array.from(table.querySelectorAll("tbody tr"));
    expect(rows).toHaveLength(2);
    // 序号从 1 起按时间顺序递增，区间与时间轴一致
    expect(rows[0].querySelector("td")?.textContent).toBe("01");
    expect(rows[1].querySelector("td")?.textContent).toBe("02");
    expect(rows[0].textContent).toContain("0:00 ~ 31:03");
    expect(rows[1].textContent).toContain("31:03 ~ 1:02:05");
    // 位置条按原视频时间轴给出起止区间，第二片接在第一片之后
    const spans = table.querySelectorAll<HTMLElement>(".axis-span");
    expect(spans).toHaveLength(2);
    expect(spans[0].style.left).toBe("0%");
    expect(spans[1].style.left).toBe("50%");
  });

  it("不提供切片回看入口，片段只以文字信息呈现", async () => {
    mockTask(
      taskResponse("succeeded", null, METADATA, COVERAGE, [clipResponse({ index: 0 })])
    );

    await renderApp();
    selectFile();

    await waitFor(() => expect(screen.getByText(/任务：已完成/)).toBeInTheDocument());
    const table = document.querySelector(".clips--split") as HTMLTableElement;
    // 撤销切片回看：表内既没有下载/播放链接，也没有 video 元素
    // （页面上另有子导航的 <a>，它们不是切片入口，因此这里只在表内断言）
    expect(table.querySelectorAll("a")).toHaveLength(0);
    expect(document.querySelector("video")).toBeNull();
  });

  it("覆盖校验有问题时逐条展示", async () => {
    mockTask(
      taskResponse("failed", "SplitError: 覆盖校验发现 1 处问题", METADATA, {
        ...COVERAGE,
        issues: [{ code: "gap", message: "第 1 片（止于 1862.5s）与第 2 片（起于 1900.0s）之间缺失 37.5s" }],
      }, [clipResponse()])
    );

    await renderApp();
    selectFile();

    await waitFor(() => expect(screen.getByText(/失败原因：SplitError/)).toBeInTheDocument());
    expect(screen.getByText(/缺失 37.5s/)).toBeInTheDocument();
    expect(screen.getByLabelText("切分结果", { selector: "section" }).textContent).toContain("1 处问题");
  });

  it("片段上传失败时展示该片段的原因", async () => {
    mockTask(
      taskResponse("failed", "StorageServerError: 上传失败", METADATA, COVERAGE, [
        clipResponse({ status: "failed", error: "code=NoSuchBucket，request_id=abc" }),
      ])
    );

    await renderApp();
    selectFile();

    await waitFor(() => expect(screen.getByText(/失败原因：StorageServerError/)).toBeInTheDocument());
    expect(screen.getByText(/request_id=abc/)).toBeInTheDocument();
  });

  it("未切分的任务不展示切分结果", async () => {
    mockTask(taskResponse("processing", null, METADATA));

    await renderApp();
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

    await renderApp();
    selectFile();

    await waitFor(() => expect(screen.getByText(/任务：已完成/)).toBeInTheDocument());
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
    expect(screen.getByText(/整场视频未被覆盖/)).toBeInTheDocument();
  });
});

describe("工作台壳层", () => {
  const originalFetch = globalThis.fetch;

  beforeEach(() => {
    globalThis.fetch = vi.fn();
    // 路由写在 hash 上：逐用例复位，免得上一个用例留下的视图影响下一个
    window.location.hash = "";
  });

  afterEach(() => {
    globalThis.fetch = originalFetch;
    vi.restoreAllMocks();
  });

  it("已登录时页眉展示品牌与当前账号，不再出现登录入口", async () => {
    mockApi([healthRoute]);
    await renderApp();

    expect(screen.getByRole("heading", { name: "LiveReview" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /测试用户/ })).toBeInTheDocument();
    // 已经登录了，页面上不该再有第二个登录入口
    expect(screen.queryByRole("button", { name: "用户登录" })).not.toBeInTheDocument();
    expect(screen.queryByText("后端")).not.toBeInTheDocument();
  });

  it("账号菜单里可退出登录，退出后回到登录页", async () => {
    mockApi([
      healthRoute,
      (url) => (url === "/api/auth/logout" ? jsonResponse(204, null) : undefined),
    ]);
    await renderApp();

    fireEvent.click(screen.getByRole("button", { name: /测试用户/ }));
    fireEvent.click(screen.getByRole("menuitem", { name: /退出登录/ }));

    // 退回登录页并说明是主动退出，而不是让用户面对一个没有解释的登录框
    expect(await screen.findByLabelText("账号")).toBeInTheDocument();
    expect(screen.getByText("你已退出登录。")).toBeInTheDocument();
  });

  it("侧栏最近任务列出既有任务，点击后在工作区展示该任务结果", async () => {
    const history = taskResponse("succeeded", null, METADATA, COVERAGE, [clipResponse({ index: 0 })]);
    mockApi([healthRoute, (url) => (url === "/api/tasks/t1" ? jsonResponse(200, history) : undefined)], [
      history,
    ]);

    await renderApp();

    const item = await screen.findByRole("button", { name: /live\.ts/ });
    fireEvent.click(item);

    // 打开后进入任务详情：返回入口、任务名与媒体信息都在
    expect(await screen.findByRole("button", { name: "返回任务列表" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "live.ts" })).toBeInTheDocument();
    expect(screen.getByLabelText("媒体信息")).toBeInTheDocument();
  });

  it("没有任务时给出空态说明", async () => {
    mockApi([healthRoute], []);
    await renderApp();

    expect(await screen.findByText(/还没有任务/)).toBeInTheDocument();
  });

  it("任务列表读取失败时说明原因，不影响工作区上传入口", async () => {
    globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url === "/api/auth/session") return jsonResponse(200, SESSION);
      if (url.startsWith("/health")) return jsonResponse(200, HEALTH);
      if (url.startsWith("/api/tasks?")) return jsonResponse(503, { detail: "任务列表暂不可用" });
      throw new Error(`未覆盖的请求：${url}`);
    }) as unknown as typeof fetch;

    await renderApp();

    expect(await screen.findByText("任务列表暂不可用")).toBeInTheDocument();
    expect(screen.getByLabelText("选择录屏文件")).toBeInTheDocument();
  });
});

describe("识别结果（V0.2）", () => {
  const originalFetch = globalThis.fetch;

  beforeEach(() => {
    globalThis.fetch = vi.fn();
    window.location.hash = "";
    setPollIntervalMs(10);
  });

  afterEach(() => {
    globalThis.fetch = originalFetch;
    resetPollIntervalMs();
    vi.restoreAllMocks();
  });

  const UNDERSTANDING_DONE = {
    status: "succeeded",
    progress: 100,
    clip_count: 1,
    segment_count: 2,
    failed_clip_count: 0,
    error: null,
    started_at: "2026-01-01T00:20:00Z",
    finished_at: "2026-01-01T00:26:00Z",
    model_name: "test-model",
  };

  const TRANSCRIPT = {
    task_id: "t1",
    filename: "live.ts",
    status: "succeeded",
    clip_count: 1,
    succeeded_clip_count: 1,
    failed_clip_count: 0,
    segment_count: 2,
    model_name: "test-model",
    clips: [
      { index: 0, start_seconds: 0, end_seconds: 1862.5, status: "succeeded", segment_count: 2, error: null, warnings: [] },
    ],
    segments: [
      {
        index: 0,
        clip_index: 0,
        start_seconds: 0.5,
        end_seconds: 3.2,
        duration_seconds: 2.7,
        content: "欢迎来到直播间",
        clip_start_seconds: 0.5,
        clip_end_seconds: 3.2,
        out_of_range: false,
      },
      {
        index: 1,
        clip_index: 0,
        start_seconds: 4,
        end_seconds: 7.5,
        duration_seconds: 3.5,
        content: "今天这款到手价 199 元",
        clip_start_seconds: 4,
        clip_end_seconds: 7.5,
        out_of_range: false,
      },
    ],
    // 全文由后端拼好，前端原样呈现
    text: "# 语音识别全文：live.ts\n\n── 片段 0（00:00:00.000 - 00:31:02.500），2 条语音\n[00:00:00.500 - 00:00:03.200] 欢迎来到直播间\n",
  };

  /** 完成任务响应：带一片已识别的切片。 */
  function identifiedTask() {
    return {
      ...taskResponse("succeeded", null, METADATA, COVERAGE, [
        clipResponse({
          understanding_status: "succeeded",
          understanding_segment_count: 2,
          understanding_attempts: 1,
          understanding_at: "2026-01-01T00:26:00Z",
          understanding_warnings: [],
        }),
      ]),
      understanding: UNDERSTANDING_DONE,
    };
  }

  it("展示识别汇总，并可按片查看识别状态", async () => {
    mockApi([
      healthRoute,
      createRoute,
      chunkRoute,
      (url) => (url.endsWith("/complete") ? jsonResponse(200, { object_key: "k", size: 20 }) : undefined),
      (url) => {
        if (url === "/api/tasks/t1") return jsonResponse(200, identifiedTask());
        return undefined;
      },
    ]);

    await renderApp();
    selectFile();

    await new Promise((r) => setTimeout(r, 200));
    require("fs").writeFileSync("dbg.json", document.body.innerHTML, "utf8");
    await gotoSection("内容理解");

    expect(await screen.findByText(/已识别片段/)).toBeInTheDocument();
    // 汇总给出「识别了多少」，是判断结果能否使用的前提
    // （「2 条」在汇总与识别全文的标题计数里各出现一次，这里只要求在汇总里能看到）
    const summary = screen.getByLabelText("识别汇总");
    expect(summary.textContent).toContain("2 条");
    expect(screen.getByText("test-model")).toBeInTheDocument();

    const table = document.querySelector(".clips--understanding") as HTMLTableElement;
    const row = table.querySelector("tbody tr");
    expect(row?.textContent).toContain("已识别");
    expect(row?.textContent).toContain("2");
  });

  it("展开识别全文时拉取并呈现后端拼好的文本", async () => {
    mockApi([
      healthRoute,
      createRoute,
      chunkRoute,
      (url) => (url.endsWith("/complete") ? jsonResponse(200, { object_key: "k", size: 20 }) : undefined),
      (url) => {
        if (url === "/api/tasks/t1") return jsonResponse(200, identifiedTask());
        if (url === "/api/tasks/t1/transcript") return jsonResponse(200, TRANSCRIPT);
        return undefined;
      },
    ]);

    await renderApp();
    selectFile();

    // 识别全文默认直接展示条目：进入内容理解页即拉取，不需要先点「展开」
    await waitFor(() => expect(screen.getByRole("link", { name: /复盘分析/ })).toBeInTheDocument());
    await gotoSection("内容理解");

    expect(await screen.findByText("欢迎来到直播间")).toBeInTheDocument();
    expect(screen.getByText("今天这款到手价 199 元")).toBeInTheDocument();
    expect(screen.getByText(/共 1 片，已识别 1 片/)).toBeInTheDocument();
    // 加工过的条目才是页面上的内容；等宽纯文本全文只作为下载件，不再占版
    expect(screen.getByRole("button", { name: "下载 txt" })).toBeInTheDocument();
    expect(screen.queryByText(/语音识别全文：live.ts/)).not.toBeInTheDocument();
    expect(document.querySelector(".transcript-text")).toBeNull();
  });

  it("识别失败时给出原因，并允许单独重试该片段", async () => {
    const failedClip = clipResponse({
      understanding_status: "failed",
      understanding_segment_count: 0,
      understanding_error: "LLMTimeoutError: 识别请求超时（900s）",
      understanding_attempts: 3,
      understanding_at: "2026-01-01T00:26:00Z",
      understanding_warnings: [],
    });
    const failedTask = {
      ...taskResponse("succeeded", null, METADATA, COVERAGE, [failedClip]),
      understanding: {
        ...UNDERSTANDING_DONE,
        status: "failed",
        clip_count: 0,
        segment_count: 0,
        failed_clip_count: 1,
        error: "1/1 个片段识别失败，首个失败片段 #0：LLMTimeoutError: 识别请求超时（900s）",
      },
    };
    let retried = false;

    mockApi([
      healthRoute,
      createRoute,
      chunkRoute,
      (url) => (url.endsWith("/complete") ? jsonResponse(200, { object_key: "k", size: 20 }) : undefined),
      (url, init) => {
        if (url === "/api/tasks/t1/clips/0/understanding" && init?.method === "POST") {
          retried = true;
          return jsonResponse(200, identifiedTask());
        }
        if (url === "/api/tasks/t1") {
          return jsonResponse(200, retried ? identifiedTask() : failedTask);
        }
        return undefined;
      },
    ]);

    await renderApp();
    selectFile();

    await waitFor(() => expect(screen.getByRole("link", { name: /复盘分析/ })).toBeInTheDocument());
    await gotoSection("内容理解");

    // 失败原因整条可见（任务级汇总与片段级各出现一次），而不是只显示一个「失败」标签
    const reasons = await screen.findAllByText(/识别请求超时/);
    expect(reasons.length).toBeGreaterThanOrEqual(2);
    expect(screen.getByText(/#0/)).toBeInTheDocument();

    const retryButton = screen.getByRole("button", { name: "重新识别" });
    fireEvent.click(retryButton);

    // 任务详情会被轮询反复拉取，因此响应用 retried 标记决定内容，而不是按调用次数
    await waitFor(() => expect(retried).toBe(true));

    await waitFor(() => expect(screen.queryByText(/#0/)).not.toBeInTheDocument());
    // 「已识别」在汇总与明细里各出现一次，这里断言明细行本身确实变成了已识别
    const table = document.querySelector(".clips--understanding") as HTMLTableElement;
    await waitFor(() => expect(table.querySelector("tbody tr")?.textContent).toContain("已识别"));
  });

  it("尚未识别时不展示全文入口，只给出启动按钮", async () => {
    const pendingTask = {
      ...taskResponse("succeeded", null, METADATA, COVERAGE, [clipResponse()]),
      understanding: {
        status: "pending",
        progress: 0,
        clip_count: 0,
        segment_count: 0,
        failed_clip_count: 0,
        error: null,
        started_at: null,
        finished_at: null,
        model_name: "test-model",
      },
    };

    mockApi([
      healthRoute,
      createRoute,
      chunkRoute,
      (url) => (url.endsWith("/complete") ? jsonResponse(200, { object_key: "k", size: 20 }) : undefined),
      (url) => (url === "/api/tasks/t1" ? jsonResponse(200, pendingTask) : undefined),
    ]);

    await renderApp();
    selectFile();

    await waitFor(() => expect(screen.getByRole("link", { name: /复盘分析/ })).toBeInTheDocument());
    await gotoSection("内容理解");

    expect(await screen.findByRole("button", { name: "开始识别语音" })).toBeInTheDocument();
    // 「还没识别」与「识别出来是空的」必须区分：没有结果时不提供全文与下载入口
    expect(screen.queryByRole("button", { name: "下载 txt" })).not.toBeInTheDocument();
    expect(screen.queryByText(/识别全文/)).not.toBeInTheDocument();
  });

  it("切片尚未完成时内容理解页尚未开放并说明原因", async () => {
    mockApi([
      healthRoute,
      createRoute,
      chunkRoute,
      (url) => (url.endsWith("/complete") ? jsonResponse(200, { object_key: "k", size: 20 }) : undefined),
      (url) => {
        if (url === "/api/tasks/t1") return jsonResponse(200, taskResponse("processing"));
        return undefined;
      },
    ]);

    await renderApp();
    selectFile();

    // 处理还没结束：内容理解整页不可进，进门处就写明原因，而不是让人进去点一个灰按钮
    const link = await screen.findByRole("link", { name: /内容理解/ });
    expect(link).toHaveAttribute("aria-disabled", "true");
    expect(link).toHaveTextContent("视频处理完成后开放");
    expect(screen.queryByRole("button", { name: "开始识别语音" })).not.toBeInTheDocument();
  });
});

describe("复盘结论（V0.3）", () => {
  const originalFetch = globalThis.fetch;

  beforeEach(() => {
    globalThis.fetch = vi.fn();
    window.location.hash = "";
    setPollIntervalMs(10);
  });

  afterEach(() => {
    globalThis.fetch = originalFetch;
    resetPollIntervalMs();
    vi.restoreAllMocks();
  });

  const REVIEW_RESULT = {
    one_line: "本场讲清了越野场景，但报价前的价值翻译不足。",
    analysis_level: "部分",
    level_reason: "1/2 片识别失败，相关时段内容缺失",
    batch_count: 1,
    clip_count: 2,
    succeeded_clip_count: 1,
    failed_clip_count: 1,
    key_events: [
      { start_seconds: 12, end_seconds: 40, topic: "A 产品讲解", summary: "开场介绍三电" },
    ],
    findings: [
      {
        dimension: "产品讲解",
        judgement: "参数直给偏多，缺少场景翻译",
        evidence_level: "事实",
        evidence: "电机功率 400 千瓦",
        start_seconds: 120,
        suggestion: "先讲越野场景里的通过性",
      },
    ],
    top_issues: [
      {
        problem: "权益公布前没有先建立价值",
        evidence: "现在下订直接减两万",
        impact: "留资前的信任建立环节",
        root_cause: "话术方法",
        action: "权益前先用 30 秒讲清三项核心价值",
      },
    ],
    next_actions: [
      { goal: "报价前完成价值翻译", how: "报价前 3 分钟按场景讲三项配置", observe: "是否在报价前完成翻译" },
    ],
    missing_info: ["本场没有分钟级数据，无法判断停留与转化效果"],
  };

  /** 已完成识别、尚未复盘的响应：复盘块显示入口按钮。 */
  function reviewedTask(review: unknown) {
    const clips = [
      clipResponse({
        understanding_status: "succeeded",
        understanding_segment_count: 2,
        understanding_attempts: 1,
        understanding_at: "2026-01-01T00:26:00Z",
        understanding_warnings: [],
      }),
    ];
    return {
      ...taskResponse("succeeded", null, METADATA, COVERAGE, clips),
      understanding: {
        status: "succeeded",
        progress: 100,
        clip_count: 1,
        segment_count: 2,
        failed_clip_count: 0,
        error: null,
        started_at: "2026-01-01T00:20:00Z",
        finished_at: "2026-01-01T00:26:00Z",
        model_name: "test-model",
      },
      review,
    };
  }

  it("识别完成后可启动复盘，结论按「等级、事件、发现、动作」呈现", async () => {
    const done = {
      status: "succeeded",
      progress: 100,
      clip_count: 1,
      segment_count: 2,
      batch_count: 1,
      error: null,
      started_at: "2026-01-01T00:30:00Z",
      finished_at: "2026-01-01T00:32:00Z",
      model_name: "test-model",
      warnings: [],
      result: REVIEW_RESULT,
    };

    mockApi([
      healthRoute,
      createRoute,
      chunkRoute,
      (url) => (url.endsWith("/complete") ? jsonResponse(200, { object_key: "k", size: 20 }) : undefined),
      (url) => {
        if (url === "/api/tasks/t1") return jsonResponse(200, reviewedTask(done));
        return undefined;
      },
    ]);

    await renderApp();
    selectFile();

    // 这条任务已完成识别，打开后按进度自动落在最靠后的可进子页（复盘分析）
    await waitFor(() =>
      expect(screen.getByRole("link", { name: "复盘分析" })).toHaveAttribute("aria-current", "page")
    );
    // 结论最先讲清边界：分析等级与定级依据排在结论之前
    // （「定级依据：」外面套着 <strong>，因此按子串匹配而不按整段文本匹配）
    expect(await screen.findByText(/定级依据：/)).toBeInTheDocument();
    expect(screen.getByText(REVIEW_RESULT.one_line)).toBeInTheDocument();
    expect(screen.getByText("A 产品讲解")).toBeInTheDocument();
    // 每条判断带证据等级，这是准则里「区分事实与推断」的界面落实
    expect(screen.getByText("事实")).toBeInTheDocument();
    expect(screen.getByText(REVIEW_RESULT.top_issues[0].problem)).toBeInTheDocument();
    expect(screen.getByText(REVIEW_RESULT.next_actions[0].goal)).toBeInTheDocument();
    // 缺口单独成块，不藏在结论里
    expect(screen.getByText("本场不足以判断")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "下载 Markdown 报告" })).toBeInTheDocument();
  });

  it("点击开始复盘会调用接口并把返回的状态写回界面", async () => {
    const pending = {
      status: "pending",
      progress: 0,
      clip_count: 0,
      segment_count: 0,
      batch_count: 0,
      error: null,
      started_at: null,
      finished_at: null,
      model_name: null,
      warnings: [],
      result: null,
    };
    const running = {
      ...pending,
      status: "running",
      progress: 10,
      batch_count: 1,
      started_at: "2026-01-01T00:30:00Z",
      model_name: "test-model",
    };

    let started = false;
    mockApi([
      healthRoute,
      createRoute,
      chunkRoute,
      (url, init) => {
        if (url.endsWith("/complete")) return jsonResponse(200, { object_key: "k", size: 20 });
        if (url === "/api/tasks/t1/review" && init?.method === "POST") {
          started = true;
          return jsonResponse(202, reviewedTask(running));
        }
        if (url === "/api/tasks/t1") {
          return jsonResponse(200, reviewedTask(started ? running : pending));
        }
        return undefined;
      },
    ]);

    await renderApp();
    selectFile();

    const button = await screen.findByRole("button", { name: "开始复盘分析" });
    fireEvent.click(button);

    await waitFor(() => expect(started).toBe(true));
    expect(await screen.findByRole("button", { name: "复盘中…" })).toBeDisabled();
  });

  it("识别未完成时复盘分析页尚未开放并说明原因", async () => {
    const noUnderstanding = {
      ...taskResponse("succeeded", null, METADATA, COVERAGE, [clipResponse()]),
      understanding: null,
      review: null,
    };

    mockApi([
      healthRoute,
      createRoute,
      chunkRoute,
      (url) => (url.endsWith("/complete") ? jsonResponse(200, { object_key: "k", size: 20 }) : undefined),
      (url) => (url === "/api/tasks/t1" ? jsonResponse(200, noUnderstanding) : undefined),
    ]);

    await renderApp();
    selectFile();

    const link = await screen.findByRole("link", { name: /复盘分析/ });
    expect(link).toHaveAttribute("aria-disabled", "true");
    expect(link).toHaveTextContent("内容识别完成后开放");
    expect(screen.queryByRole("button", { name: "开始复盘分析" })).not.toBeInTheDocument();
  });

  it("复盘失败时显示原因，且不展示可下载报告", async () => {
    const failed = {
      status: "failed",
      progress: 0,
      clip_count: 1,
      segment_count: 2,
      batch_count: 1,
      error: "LLMTimeoutError: 模型请求超时（300s）",
      started_at: "2026-01-01T00:30:00Z",
      finished_at: "2026-01-01T00:35:00Z",
      model_name: "test-model",
      warnings: [],
      result: null,
    };

    mockApi([
      healthRoute,
      createRoute,
      chunkRoute,
      (url) => (url.endsWith("/complete") ? jsonResponse(200, { object_key: "k", size: 20 }) : undefined),
      (url) => (url === "/api/tasks/t1" ? jsonResponse(200, reviewedTask(failed)) : undefined),
    ]);

    await renderApp();
    selectFile();

    await screen.findByRole("link", { name: /复盘分析/ });
    // 复盘失败的任务同样已开过复盘：打开即落在复盘分析页，失败原因整条可见
    expect(await screen.findByText(/LLMTimeoutError/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "下载 Markdown 报告" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "开始复盘分析" })).toBeInTheDocument();
  });
});

describe("三子页信息架构（V0.4.1）", () => {
  const originalFetch = globalThis.fetch;

  beforeEach(() => {
    globalThis.fetch = vi.fn();
    window.location.hash = "";
    window.localStorage.clear();
    setPollIntervalMs(10);
  });

  afterEach(() => {
    globalThis.fetch = originalFetch;
    resetPollIntervalMs();
    window.localStorage.clear();
    vi.restoreAllMocks();
  });

  /** 三条任务：只切分完、识别完、复盘完。分别对应三个子页的开放条件。 */
  const splitOnly = () =>
    taskResponse("succeeded", null, METADATA, COVERAGE, [clipResponse({ index: 0 })]);
  const understandingDone = () => ({
    ...taskResponse("succeeded", null, METADATA, COVERAGE, [
      clipResponse({ index: 0, understanding_status: "succeeded", understanding_segment_count: 2 }),
    ]),
    understanding: {
      status: "succeeded",
      progress: 100,
      clip_count: 1,
      segment_count: 2,
      failed_clip_count: 0,
      error: null,
      started_at: "2026-01-01T00:20:00Z",
      finished_at: "2026-01-01T00:26:00Z",
      model_name: "test-model",
    },
  });
  const reviewDone = () => ({
    ...understandingDone(),
    review: {
      status: "succeeded",
      progress: 100,
      clip_count: 1,
      segment_count: 2,
      batch_count: 1,
      error: null,
      started_at: "2026-01-01T00:30:00Z",
      finished_at: "2026-01-01T00:32:00Z",
      model_name: "test-model",
      warnings: [],
      result: {
        one_line: "本场讲清了产品，但报价前的价值翻译不足。",
        analysis_level: "完整",
        level_reason: "全部片段识别成功",
        batch_count: 1,
        clip_count: 1,
        succeeded_clip_count: 1,
        failed_clip_count: 0,
        key_events: [],
        findings: [],
        top_issues: [],
        next_actions: [],
        missing_info: [],
      },
    },
  });

  /** 从侧栏最近任务打开这条任务：打开历史任务与「本次上传」是两条不同的入口。 */
  async function openRecent(task: unknown) {
    mockApi([healthRoute, (url) => (url === "/api/tasks/t1" ? jsonResponse(200, task) : undefined)], [task]);
    await renderApp();
    fireEvent.click(await screen.findByRole("button", { name: /live\.ts/ }));
  }

  it("三个子页并列呈现：未开放的置灰并写明什么时候开放", async () => {
    await openRecent(splitOnly());

    // 只切分完：内容理解与复盘分析都还没到，但用户看得到后面还有两步
    const understanding = await screen.findByRole("link", { name: /内容理解/ });
    const review = screen.getByRole("link", { name: /复盘分析/ });
    expect(understanding).not.toHaveAttribute("aria-disabled", "true");
    expect(review).toHaveAttribute("aria-disabled", "true");
    expect(review).toHaveTextContent("内容识别完成后开放");
    expect(screen.getByRole("link", { name: /视频信息/ })).toHaveAttribute("aria-current", "page");
  });

  it("打开已复盘的任务直接落在复盘分析，打开仅识别的任务落在内容理解", async () => {
    await openRecent(reviewDone());
    await waitFor(() =>
      expect(screen.getByRole("link", { name: "复盘分析" })).toHaveAttribute("aria-current", "page")
    );
    expect(screen.getByText("本场讲清了产品，但报价前的价值翻译不足。")).toBeInTheDocument();
  });

  it("打开仅完成识别的任务落在内容理解", async () => {
    await openRecent(understandingDone());
    await waitFor(() =>
      expect(screen.getByRole("link", { name: "内容理解" })).toHaveAttribute("aria-current", "page")
    );
    expect(screen.getByText(/已识别片段/)).toBeInTheDocument();
  });

  it("子页写进地址：直接打开带子页的链接就停在那一页", async () => {
    window.location.hash = "#/tasks/t1/review";
    const task = reviewDone();
    mockApi([healthRoute, (url) => (url === "/api/tasks/t1" ? jsonResponse(200, task) : undefined)], [task]);

    await renderApp();

    // 刷新后停在原处是路由的基本承诺，子页也不例外
    await waitFor(() =>
      expect(screen.getByRole("link", { name: "复盘分析" })).toHaveAttribute("aria-current", "page")
    );
  });

  it("点名了未开放的子页时退回可进的那一页并说明", async () => {
    window.location.hash = "#/tasks/t1/review";
    const task = splitOnly();
    mockApi([healthRoute, (url) => (url === "/api/tasks/t1" ? jsonResponse(200, task) : undefined)], [task]);

    await renderApp();

    // 旧链接可能指向一个还没开放的页：退回可进的那一页，而不是给一个空屏
    await waitFor(() =>
      expect(screen.getByRole("link", { name: "视频信息" })).toHaveAttribute("aria-current", "page")
    );
  });

  it("切片按组分页，每页不超过十条", async () => {
    const clips = Array.from({ length: 23 }, (_, index) =>
      clipResponse({
        index,
        start_seconds: index * 10,
        end_seconds: index * 10 + 10,
        duration_seconds: 10,
      })
    );
    const task = taskResponse("succeeded", null, METADATA, { ...COVERAGE, clip_count: 23 }, clips);
    await openRecent(task);

    await waitFor(() => expect(screen.getByText(/任务：已完成/)).toBeInTheDocument());

    const table = document.querySelector(".clips--split") as HTMLTableElement;
    // 一屏最多十条：几十片一次铺开会把页面拉到无法核对
    expect(table.querySelectorAll("tbody tr")).toHaveLength(10);
    expect(screen.getByText(/第 1–10 片 \/ 共 23 片/)).toBeInTheDocument();
    expect(screen.getByText(/第 1 \/ 3 组/)).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "下一组" }));
    expect(table.querySelectorAll("tbody tr")).toHaveLength(10);
    expect(screen.getByText(/第 11–20 片 \/ 共 23 片/)).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "末组" }));
    expect(table.querySelectorAll("tbody tr")).toHaveLength(3);
    expect(screen.getByText(/第 21–23 片 \/ 共 23 片/)).toBeInTheDocument();
  });

  it("识别全文默认展示条目，纯文本只提供下载", async () => {
    const task = understandingDone();
    const transcript = {
      task_id: "t1",
      filename: "live.ts",
      status: "succeeded",
      clip_count: 1,
      succeeded_clip_count: 1,
      failed_clip_count: 0,
      segment_count: 2,
      model_name: "test-model",
      clips: [],
      segments: [
        {
          index: 0,
          clip_index: 0,
          start_seconds: 0.5,
          end_seconds: 3.2,
          duration_seconds: 2.7,
          content: "欢迎来到直播间",
          tone: "热情",
          clip_start_seconds: 0.5,
          clip_end_seconds: 3.2,
          out_of_range: false,
        },
      ],
      text: [
        "# 语音识别全文：live.ts",
        "[00:00:00.500 - 00:00:03.200] 欢迎来到直播间",
        "",
      ].join(String.fromCharCode(10)),
    };

    mockApi(
      [
        healthRoute,
        (url) => (url === "/api/tasks/t1" ? jsonResponse(200, task) : undefined),
        (url) => (url === "/api/tasks/t1/transcript" ? jsonResponse(200, transcript) : undefined),
      ],
      [task]
    );
    await renderApp();
    fireEvent.click(await screen.findByRole("button", { name: /live\.ts/ }));

    // 进页即拉取并展示条目，不需要先点「展开」；纯文本不再占版，只留下载入口
    expect(await screen.findByText("欢迎来到直播间")).toBeInTheDocument();
    expect(screen.getByText("（热情）")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "下载 txt" })).toBeInTheDocument();
    expect(document.querySelector(".transcript-text")).toBeNull();
  });
});

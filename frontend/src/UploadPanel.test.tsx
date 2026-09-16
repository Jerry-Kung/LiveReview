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

function taskResponse(status: string, error: string | null = null) {
  return {
    id: "t1",
    kind: "ingest",
    status,
    progress: status === "succeeded" ? 100 : 0,
    filename: "live.ts",
    object_key: "liverreview/original/live_1_abcd.ts",
    size: 20,
    error,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
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

  it("切换到服务状态页展示存储状态", async () => {
    mockApi([healthRoute]);
    render(<App />);
    fireEvent.click(screen.getByRole("button", { name: "服务状态" }));
    expect(await screen.findByText(/对象存储：已配置/)).toBeInTheDocument();
  });
});

import { render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App from "./App";

/** 页面只需要任务列表接口；页眉不再读取健康检查。 */
function mockApi() {
  globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.startsWith("/api/tasks?")) {
      return { ok: true, status: 200, json: () => Promise.resolve({ items: [] }) } as Response;
    }
    throw new Error(`未覆盖的请求：${url}`);
  }) as unknown as typeof fetch;
}

describe("App 壳层", () => {
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

  it("页眉只呈现品牌与账号入口，不出现服务或版本信息", () => {
    mockApi();
    render(<App />);

    expect(screen.getByRole("heading", { name: "LiveReview" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "用户登录" })).toBeInTheDocument();
    // 服务状态行已移除：不再有后端、对象存储、版本这些内部字样
    expect(screen.queryByText("后端")).not.toBeInTheDocument();
    expect(screen.queryByText("对象存储")).not.toBeInTheDocument();
    expect(screen.queryByText("版本")).not.toBeInTheDocument();
  });

  it("任务列表读取失败时不影响上传入口", async () => {
    globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.startsWith("/api/tasks?")) {
        return { ok: false, status: 503, json: () => Promise.resolve({ detail: "暂不可用" }) } as Response;
      }
      throw new Error(`未覆盖的请求：${url}`);
    }) as unknown as typeof fetch;

    render(<App />);

    expect(await screen.findByText("暂不可用")).toBeInTheDocument();
    expect(screen.getByLabelText("选择录屏文件")).toBeInTheDocument();
  });
});


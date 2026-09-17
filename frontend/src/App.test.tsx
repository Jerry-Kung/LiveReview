import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App from "./App";

/** 页眉的服务状态：后端连通性与对象存储配置来自健康检查。 */
function mockHealth(body: unknown, ok = true) {
  globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.startsWith("/health")) {
      return { ok, status: ok ? 200 : 503, json: () => Promise.resolve(body) } as Response;
    }
    if (url.startsWith("/api/tasks?")) {
      return { ok: true, status: 200, json: () => Promise.resolve({ items: [] }) } as Response;
    }
    throw new Error(`未覆盖的请求：${url}`);
  }) as unknown as typeof fetch;
}

describe("App 页眉状态", () => {
  const originalFetch = globalThis.fetch;

  beforeEach(() => {
    globalThis.fetch = vi.fn();
  });

  afterEach(() => {
    globalThis.fetch = originalFetch;
    vi.restoreAllMocks();
  });

  it("健康检查未连通时显示未连接", async () => {
    mockHealth({}, false);
    render(<App />);
    expect(screen.getByText("未连接")).toBeInTheDocument();
    // 版本与存储都退化为占位符
    const runtime = screen.getByLabelText("服务状态");
    expect(runtime.querySelectorAll("dd")[1].textContent).toBe("—");
    expect(runtime.querySelectorAll("dd")[2].textContent).toBe("—");
  });

  it("后端健康且存储已配置时显示正常状态", async () => {
    mockHealth({
      status: "ok",
      service: "LiveReview",
      version: "0.1.6",
      environment: "development",
      database: "ok",
      storage: "configured",
      storage_missing: [],
    });
    render(<App />);
    await waitFor(() => expect(screen.getByText("已连接")).toBeInTheDocument());
    expect(screen.getByText("已配置")).toBeInTheDocument();
    expect(screen.getByText("0.1.6")).toBeInTheDocument();
  });

  it("缺少凭据时列出缺失项", async () => {
    mockHealth({
      status: "ok",
      service: "LiveReview",
      version: "0.1.6",
      environment: "development",
      database: "ok",
      storage: "not_configured",
      storage_missing: ["TOS_ACCESS_KEY", "TOS_SECRET_KEY"],
    });
    render(<App />);
    expect(await screen.findByText(/TOS_ACCESS_KEY/)).toBeInTheDocument();
  });

  it("存储不可用时仍渲染页面其他部分", async () => {
    mockHealth({
      status: "degraded",
      service: "LiveReview",
      version: "0.1.6",
      environment: "development",
      database: "ok",
      storage: "unavailable",
      storage_missing: [],
    });
    render(<App />);
    expect(await screen.findByText("不可用")).toBeInTheDocument();
    expect(screen.getByLabelText("选择录屏文件")).toBeInTheDocument();
  });

  it("健康检查不返回 storage 字段时显示未知", async () => {
    mockHealth({
      status: "ok",
      service: "LiveReview",
      version: "0.1.6",
      environment: "development",
      database: "ok",
    });
    render(<App />);
    expect(await screen.findByText("未知")).toBeInTheDocument();
  });
});

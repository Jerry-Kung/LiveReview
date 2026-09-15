import { render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App from "./App";

describe("App", () => {
  const originalFetch = globalThis.fetch;

  beforeEach(() => {
    globalThis.fetch = vi.fn();
  });

  afterEach(() => {
    globalThis.fetch = originalFetch;
    vi.restoreAllMocks();
  });

  it("显示服务名与未连接状态", async () => {
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockResolvedValue({
      ok: false,
      json: () => Promise.resolve({}),
    });
    render(<App />);
    expect(screen.getByText("LiveReview")).toBeInTheDocument();
    expect(await screen.findByText(/未连接/)).toBeInTheDocument();
  });

  it("后端健康时显示已连接状态", async () => {
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockResolvedValue({
      ok: true,
      json: () =>
        Promise.resolve({
          status: "ok",
          service: "LiveReview",
          version: "0.1.2",
          environment: "development",
          database: "ok",
        }),
    });
    render(<App />);
    expect(await screen.findByText(/已连接/)).toBeInTheDocument();
  });

  it("存储已配置时显示已配置", async () => {
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockResolvedValue({
      ok: true,
      json: () =>
        Promise.resolve({
          status: "ok",
          service: "LiveReview",
          version: "0.1.2",
          environment: "development",
          database: "ok",
          storage: "configured",
          storage_missing: [],
        }),
    });
    render(<App />);
    expect(await screen.findByText(/对象存储：已配置/)).toBeInTheDocument();
  });

  it("缺少凭据时提示未配置并列出缺失项", async () => {
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockResolvedValue({
      ok: true,
      json: () =>
        Promise.resolve({
          status: "ok",
          service: "LiveReview",
          version: "0.1.2",
          environment: "development",
          database: "ok",
          storage: "not_configured",
          storage_missing: ["TOS_ACCESS_KEY", "TOS_SECRET_KEY"],
        }),
    });
    render(<App />);
    expect(await screen.findByText(/对象存储：未配置/)).toBeInTheDocument();
    expect(screen.getByText(/TOS_ACCESS_KEY/)).toBeInTheDocument();
  });

  it("存储不可用时不使页面报错", async () => {
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockResolvedValue({
      ok: true,
      json: () =>
        Promise.resolve({
          status: "degraded",
          service: "LiveReview",
          version: "0.1.2",
          environment: "development",
          database: "ok",
          storage: "unavailable",
          storage_missing: [],
        }),
    });
    render(<App />);
    expect(await screen.findByText(/对象存储：不可用/)).toBeInTheDocument();
  });

  it("后端不返回 storage 字段时页面仍可渲染", async () => {
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockResolvedValue({
      ok: true,
      json: () =>
        Promise.resolve({
          status: "ok",
          service: "LiveReview",
          version: "0.1.2",
          environment: "development",
          database: "ok",
        }),
    });
    render(<App />);
    expect(await screen.findByText(/对象存储：未知/)).toBeInTheDocument();
  });
});

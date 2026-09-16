import { render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App from "./App";

describe("App", () => {
  const originalFetch = globalThis.fetch;

  beforeEach(() => {
    globalThis.fetch = vi.fn();
    // 状态页现在是第二个标签屏，直接以 hash 进入
    window.location.hash = "status";
  });

  afterEach(() => {
    globalThis.fetch = originalFetch;
    window.location.hash = "";
    vi.restoreAllMocks();
  });

  it("显示服务名与未连接状态", async () => {
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockResolvedValue({
      ok: false,
      json: () => Promise.resolve({}),
    });
    render(<App />);
    expect(screen.getByText("LiveReview")).toBeInTheDocument();
    const line = await screen.findByText(/未连接/);
    expect(line).toBeInTheDocument();
    expect(line).toHaveAttribute("data-state", "attention");
  });

  it("后端健康时显示已连接状态", async () => {
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockResolvedValue({
      ok: true,
      json: () =>
        Promise.resolve({
          status: "ok",
          service: "LiveReview",
          version: "0.1.3",
          environment: "development",
          database: "ok",
        }),
    });
    render(<App />);
    const line = await screen.findByText(/已连接/);
    expect(line).toBeInTheDocument();
    expect(line).toHaveAttribute("data-state", "ok");
  });

  it("存储已配置时显示已配置，并带有健康态语义标记", async () => {
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockResolvedValue({
      ok: true,
      json: () =>
        Promise.resolve({
          status: "ok",
          service: "LiveReview",
          version: "0.1.3",
          environment: "development",
          database: "ok",
          storage: "configured",
          storage_missing: [],
        }),
    });
    render(<App />);
    const line = await screen.findByText(/对象存储：已配置/);
    expect(line).toBeInTheDocument();
    expect(line).toHaveAttribute("data-state", "ok");
  });

  it("缺少凭据时提示未配置并列出缺失项，并带有待留意语义标记", async () => {
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockResolvedValue({
      ok: true,
      json: () =>
        Promise.resolve({
          status: "ok",
          service: "LiveReview",
          version: "0.1.3",
          environment: "development",
          database: "ok",
          storage: "not_configured",
          storage_missing: ["TOS_ACCESS_KEY", "TOS_SECRET_KEY"],
        }),
    });
    render(<App />);
    const line = await screen.findByText(/对象存储：未配置/);
    expect(line).toBeInTheDocument();
    expect(line).toHaveAttribute("data-state", "attention");
    expect(screen.getByText(/TOS_ACCESS_KEY/)).toBeInTheDocument();
  });

  it("存储不可用时不使页面报错，并带有待留意语义标记", async () => {
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockResolvedValue({
      ok: true,
      json: () =>
        Promise.resolve({
          status: "degraded",
          service: "LiveReview",
          version: "0.1.3",
          environment: "development",
          database: "ok",
          storage: "unavailable",
          storage_missing: [],
        }),
    });
    render(<App />);
    const line = await screen.findByText(/对象存储：不可用/);
    expect(line).toBeInTheDocument();
    expect(line).toHaveAttribute("data-state", "attention");
  });

  it("后端不返回 storage 字段时页面仍可渲染，并带有待留意语义标记", async () => {
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockResolvedValue({
      ok: true,
      json: () =>
        Promise.resolve({
          status: "ok",
          service: "LiveReview",
          version: "0.1.3",
          environment: "development",
          database: "ok",
        }),
    });
    render(<App />);
    const line = await screen.findByText(/对象存储：未知/);
    expect(line).toBeInTheDocument();
    expect(line).toHaveAttribute("data-state", "attention");
  });

  it("健康检查未返回数据时，对象存储行显示占位符“—”", async () => {
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockResolvedValue({
      ok: false,
      json: () => Promise.resolve({}),
    });
    render(<App />);
    const line = await screen.findByText(/对象存储：—/);
    expect(line).toBeInTheDocument();
    expect(line).toHaveAttribute("data-state", "attention");
  });
});

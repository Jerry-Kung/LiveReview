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
          version: "0.1.1",
          environment: "development",
          database: "ok",
        }),
    });
    render(<App />);
    expect(await screen.findByText(/已连接/)).toBeInTheDocument();
  });
});

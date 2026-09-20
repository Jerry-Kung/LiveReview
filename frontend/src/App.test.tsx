/**
 * 壳层的登录门槛：未登录只给登录页，登录后进工作台，会话失效退回登录页。
 *
 * 这些断言钉住的是 V0.5 的核心承诺——「外部人员访问项目首先落在登录页」。真实的安全性
 * 由后端接口保证（见后端 `tests/auth/`），这里验证的是界面不会把工作台露给未登录的人。
 */

import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App from "./App";
import { SESSION } from "./test/fixtures";

function jsonResponse(status: number, body: unknown): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: () => Promise.resolve(body),
  } as Response;
}

/** 只覆盖登录门槛相关的请求：会话查询、登录、任务列表。 */
function mockApi(options: { session?: Response } = {}) {
  const calls: string[] = [];
  globalThis.fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    calls.push(`${init?.method ?? "GET"} ${url}`);
    if (url === "/api/auth/session") return options.session ?? jsonResponse(200, SESSION);
    if (url === "/api/auth/login") return jsonResponse(200, SESSION);
    if (url === "/api/auth/logout") return jsonResponse(204, null);
    if (url.startsWith("/api/tasks?")) return jsonResponse(200, { items: [] });
    throw new Error(`未覆盖的请求：${url}`);
  }) as unknown as typeof fetch;
  return calls;
}

describe("登录门槛", () => {
  const originalFetch = globalThis.fetch;

  beforeEach(() => {
    globalThis.fetch = vi.fn();
    window.location.hash = "";
    window.localStorage.clear();
  });

  afterEach(() => {
    globalThis.fetch = originalFetch;
    window.localStorage.clear();
    vi.restoreAllMocks();
  });

  it("未登录时只显示登录页，不渲染工作台", async () => {
    mockApi({ session: jsonResponse(401, { detail: "登录状态已失效，请重新登录" }) });
    render(<App />);

    expect(await screen.findByRole("heading", { name: "LiveReview" })).toBeInTheDocument();
    // 登录页而不是工作台：上传入口、侧栏、页眉的账号区都不该出现
    expect(screen.getByLabelText("账号")).toBeInTheDocument();
    expect(screen.getByLabelText("密码")).toBeInTheDocument();
    expect(screen.queryByLabelText("选择录屏文件")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("工作区")).not.toBeInTheDocument();
  });

  it("未登录时不请求任务列表", async () => {
    // 未登录时这个请求必然 401，发出去只会白白触发一次「会话失效」提示
    const calls = mockApi({ session: jsonResponse(401, { detail: "未登录" }) });
    render(<App />);

    await screen.findByLabelText("账号");
    expect(calls.some((call) => call.includes("/api/tasks?"))).toBe(false);
  });

  it("登录后进入工作台，并说明本版不提供注册与用户隔离", async () => {
    mockApi({ session: jsonResponse(401, { detail: "未登录" }) });
    render(<App />);

    fireEvent.change(await screen.findByLabelText("账号"), { target: { value: "tester" } });
    fireEvent.change(screen.getByLabelText("密码"), { target: { value: "test-password-1" } });
    fireEvent.click(screen.getByRole("button", { name: "登录" }));

    expect(await screen.findByLabelText("选择录屏文件")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /测试用户/ })).toBeInTheDocument();
    // 登录页上如实说明本版的边界，不让使用者以为可以自助注册或彼此隔离
    expect(screen.queryByText(/暂不提供自助注册/)).not.toBeInTheDocument();
  });

  it("登录失败时原样呈现后端给出的原因", async () => {
    globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url === "/api/auth/session") return jsonResponse(401, { detail: "未登录" });
      if (url === "/api/auth/login") return jsonResponse(401, { detail: "账号或密码不正确" });
      throw new Error(`未覆盖的请求：${url}`);
    }) as unknown as typeof fetch;

    render(<App />);
    fireEvent.change(await screen.findByLabelText("账号"), { target: { value: "tester" } });
    fireEvent.change(screen.getByLabelText("密码"), { target: { value: "wrong" } });
    fireEvent.click(screen.getByRole("button", { name: "登录" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("账号或密码不正确");
    // 失败后仍停在登录页
    expect(screen.queryByLabelText("选择录屏文件")).not.toBeInTheDocument();
  });

  it("账号或密码为空时不发请求", async () => {
    const calls = mockApi({ session: jsonResponse(401, { detail: "未登录" }) });
    render(<App />);

    fireEvent.click(await screen.findByRole("button", { name: "登录" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("请填写账号与密码。");
    expect(calls.some((call) => call.includes("/api/auth/login"))).toBe(false);
  });

  it("会话过期时退回登录页并说明原因，而不是显示一个没有解释的登录框", async () => {
    const { unmount } = render(<App />);
    // 先以已登录状态进入工作台
    mockApi({ session: jsonResponse(401, { detail: "未登录" }) });
    await screen.findByLabelText("账号");
    unmount();

    // 再以「Cookie 还在但服务端已判失效」的状态进入：任务列表会返回 401
    globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url === "/api/auth/session") return jsonResponse(200, SESSION);
      if (url.startsWith("/api/tasks?")) return jsonResponse(401, { detail: "登录状态已失效" });
      throw new Error(`未覆盖的请求：${url}`);
    }) as unknown as typeof fetch;

    render(<App />);

    expect(
      await screen.findByText("登录状态已失效，请重新登录。"),
    ).toBeInTheDocument();
    expect(screen.queryByLabelText("选择录屏文件")).not.toBeInTheDocument();
  });

  it("首次进入的载入态不显示登录页，避免闪一下再跳进工作台", async () => {
    // 把会话请求挂起，观察「还没问完」这一态；TS 的控制流分析看不到赋值发生在回调里，
    // 因此显式声明为可变引用
    const pending: { release?: (value: Response) => void } = {};
    globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url === "/api/auth/session") {
        return new Promise<Response>((resolve) => {
          pending.release = resolve;
        });
      }
      throw new Error(`未覆盖的请求：${url}`);
    }) as unknown as typeof fetch;

    render(<App />);
    expect(screen.getByRole("status")).toHaveTextContent("正在载入…");
    expect(screen.queryByLabelText("账号")).not.toBeInTheDocument();

    pending.release?.(jsonResponse(200, SESSION));
    expect(await screen.findByLabelText("选择录屏文件")).toBeInTheDocument();
  });
});

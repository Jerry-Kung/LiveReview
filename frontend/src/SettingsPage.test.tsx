/**
 * 设置页的用例（V0.7.0）。
 *
 * 只钉三件界面承诺，都是「说错了会误导用户」的地方：开关的当前状态如实显示；
 * 非管理员看得到状态但点不动；模型未配置时给出提示而不是让人开完开关等一个不动的任务。
 * 不测样式类名——开关的外观怎么改都不该让用例变红。
 */

import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import SettingsPage from "./SettingsPage";
import type { Settings } from "./api";

/**
 * 样例设置。类型标成 `Settings`（而不是让 TS 从字面量推断）：
 * `model_name` 允许为 null，推断出的字面量类型会把它收窄成 string，
 * 于是「模型未配置」那条用例传 null 就编译不过。
 */
const SAMPLE: Settings = {
  auto_pipeline: false,
  model_name: "test-model",
  llm_configured: true,
  split_max_duration_seconds: 3600,
  split_max_clip_bytes: 1024 * 1024 * 1024,
  video_ttl_seconds: 72 * 3600,
};

function jsonResponse(status: number, body: unknown): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: () => Promise.resolve(body),
  } as Response;
}

let fetchMock: ReturnType<typeof vi.fn>;

/** 按 URL 与方法分派的 fetch 桩；`patches` 收集每次 PATCH 的请求体，供断言调用次数。 */
function stubFetch(
  initial: Settings,
  options: { onPatch?: (body: unknown) => unknown } = {},
) {
  const patches: unknown[] = [];
  fetchMock = vi.fn((url: string, init?: RequestInit) => {
    if (String(url) === "/api/settings") {
      if (init?.method === "PATCH") {
        const body = JSON.parse(String(init.body));
        patches.push(body);
        const next = options.onPatch
          ? options.onPatch(body)
          : { ...initial, auto_pipeline: body.auto_pipeline };
        return Promise.resolve(jsonResponse(200, next));
      }
      return Promise.resolve(jsonResponse(200, initial));
    }
    return Promise.resolve(jsonResponse(404, { detail: "not found" }));
  });
  vi.stubGlobal("fetch", fetchMock);
  return patches;
}

beforeEach(() => {
  vi.restoreAllMocks();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("设置页：全自动流程模式（V0.7.0）", () => {
  it("显示开关的当前状态与只读运行参数", async () => {
    stubFetch(SAMPLE);
    render(<SettingsPage isAdmin />);

    const toggle = await screen.findByRole("checkbox");
    expect(toggle).not.toBeChecked();
    expect(screen.getByText("已关闭")).toBeInTheDocument();

    // 只读参数里最能影响费用的一项是模型名，必须看得见
    const facts = screen.getByLabelText("运行参数");
    expect(within(facts).getByText("test-model")).toBeInTheDocument();
  });

  it("管理员勾选开关会提交 PATCH，并以服务端返回的取值为准", async () => {
    const patches = stubFetch(SAMPLE);
    render(<SettingsPage isAdmin />);

    const toggle = await screen.findByRole("checkbox");
    fireEvent.click(toggle);

    await waitFor(() => expect(patches).toHaveLength(1));
    expect(patches[0]).toEqual({ auto_pipeline: true });
    // 界面显示的是服务端落库后的取值，不是本地乐观置位
    await waitFor(() => expect(screen.getByText("已开启")).toBeInTheDocument());
  });

  it("非管理员看得到状态，但开关不可点，并说明去哪里改", async () => {
    stubFetch({ ...SAMPLE, auto_pipeline: true });
    render(<SettingsPage isAdmin={false} />);

    const toggle = await screen.findByRole("checkbox");
    expect(toggle).toBeDisabled();
    // 状态仍然如实显示：普通账号也需要知道自己上传后会不会被自动跑完
    expect(screen.getByText("已开启")).toBeInTheDocument();
    expect(screen.getByText(/只对管理员开放/)).toBeInTheDocument();
    // 只有那一次 GET，没有发出 PATCH：真正的写入防线上在后端（非管理员一律 403），
    // 界面这层只是不给入口
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("模型未配置且开关已打开时提示「开了也不会自动识别」", async () => {
    stubFetch({ ...SAMPLE, auto_pipeline: true, model_name: null, llm_configured: false });
    render(<SettingsPage isAdmin />);

    expect(await screen.findByText(/模型未配置，此时即使开启全自动也不会自动识别/)).toBeInTheDocument();
  });

  it("读取失败时给出原因，不渲染一个假开关", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.resolve(jsonResponse(503, { detail: "设置读取失败" }))),
    );
    render(<SettingsPage isAdmin />);

    expect(await screen.findByRole("alert")).toHaveTextContent("设置读取失败");
    expect(screen.queryByRole("checkbox")).not.toBeInTheDocument();
  });
});

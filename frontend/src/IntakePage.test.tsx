/**
 * 新建分析任务页右栏「使用说明」的用例（V0.6.3）。
 *
 * 这里单独成一个文件而不是并进 `Workbench.test.tsx`：那个文件必须 mock 一整套 fetch 才能
 * 把上传链路跑起来，而本模块是零 props、零副作用的纯渲染，直接挂载即可。混进去只会让每条
 * 断言都拖着一堆与它无关的桩。
 *
 * 断言只碰角色与文本，不碰类名——这一栏是静态文案，样式怎么改都不该让用例变红。
 */

import { createRef } from "react";
import { render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import IntakePage from "./IntakePage";

/** 空态下的最小 props：本组用例只看右栏，上传区保持空闲即可。 */
function renderIntake() {
  return render(
    <IntakePage
      phase="idle"
      fileName={null}
      totalBytes={0}
      sentBytes={0}
      percent={0}
      error={null}
      missingChunks={[]}
      dragOver={false}
      inputId="clip-file"
      inputRef={createRef<HTMLInputElement>()}
      onPick={vi.fn()}
      onFile={vi.fn()}
      onDragOver={vi.fn()}
      onDragLeave={vi.fn()}
      onDrop={vi.fn()}
      onCancel={vi.fn()}
    />
  );
}

describe("新建任务页的使用说明（V0.6.3）", () => {
  it("给出从上传到拿到报告的五步操作清单", () => {
    renderIntake();

    // 区域名与标题一致：aria-label 与 h2 分头写死时最容易在这里失配
    const guide = screen.getByRole("complementary", { name: "使用说明" });
    expect(within(guide).getByRole("heading", { name: "使用说明" })).toBeInTheDocument();

    // 五步，且是有序列表：顺序本身就是这条链路的信息
    const steps = within(guide).getAllByRole("listitem");
    expect(steps).toHaveLength(5);
    const titles = ["上传录屏", "等待视频处理", "开始内容识别", "开始复盘分析", "查看与下载结论"];
    expect(steps.map((step) => step.querySelector(".step-title")?.textContent)).toEqual(titles);

    // 收尾提示保留两个用户会撞上、界面却不主动告知的约束
    expect(within(guide).getByText(/识别与复盘都要点一下按钮才会开始/)).toBeInTheDocument();
    expect(within(guide).getByText(/视频保留 72 小时/)).toBeInTheDocument();
  });

  it("不再讲后台处理环节，只讲用户自己要做的操作", () => {
    renderIntake();

    const guide = screen.getByRole("complementary", { name: "使用说明" });
    const text = guide.textContent ?? "";
    // 这四个是 V0.6.3 之前摆在这里的后台步骤，属于用户既看不到也影响不了的内部实现
    for (const internal of ["媒体解析", "格式处理", "视频切片", "完整性校验"]) {
      expect(text).not.toContain(internal);
    }
    expect(screen.queryByRole("heading", { name: "处理说明" })).not.toBeInTheDocument();
  });
});

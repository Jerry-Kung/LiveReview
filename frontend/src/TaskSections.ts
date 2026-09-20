/**
 * 任务结果页的三个子页与它们的开放条件。
 *
 * 拆子页的动机是「一屏装不下」：处理流程、媒体信息、切片表、识别汇总、识别全文、
 * 复盘结论原本堆在同一条竖轴上，页面被拉到几屏长，用户要滚动很久才能确认一件事。
 * 拆开之后每一页只回答一个问题：
 *
 * 1. **视频信息**——这条录屏处理得怎么样、切出了什么（上传后自动进入）。
 * 2. **内容理解**——讲的是什么（处理完成后开放，用户点击按钮触发识别）。
 * 3. **复盘分析**——讲得怎么样、下一场改什么（识别完成后开放）。
 *
 * 三条开放条件是「上一步的产物已存在」，不是「上一步已点过按钮」：处理失败的任务没有切片，
 * 但内容理解页仍然开放——失败原因与单片重试入口在那里才看得见。
 */

import type { Task } from "./api";
import type { TaskSection } from "./routing";

/** 子页在导航里的顺序，也是链路的先后顺序。 */
export const SECTION_ORDER: TaskSection[] = ["media", "understanding", "review"];

const SECTION_TITLES: Record<TaskSection, string> = {
  media: "视频信息",
  understanding: "内容理解",
  review: "复盘分析",
};

export function sectionTitle(section: TaskSection): string {
  return SECTION_TITLES[section];
}

/** 任务是否已经处理结束（成功或失败都算结束，停在处理中的不算）。 */
export function processingFinished(task: Task): boolean {
  return task.status === "succeeded" || task.status === "failed";
}

/** 是否已有可用的识别产物：有识别条目，或已经开过复盘（复盘输入就是识别结果）。 */
export function hasUnderstanding(task: Task): boolean {
  return (task.understanding?.clip_count ?? 0) > 0 || task.review != null;
}

/**
 * 子页的开放条件。返回 null 表示可以进，否则返回「为什么还不能进」，直接做界面文案。
 */
export function sectionBlockedReason(task: Task, section: TaskSection): string | null {
  if (section === "understanding") {
    return processingFinished(task) ? null : "视频处理完成后开放";
  }
  if (section === "review") {
    // 已开过复盘的任务不受识别条件约束：识别产物在库里，历史结论应该能看
    if (task.review != null) return null;
    if (!processingFinished(task)) return "视频处理完成后开放";
    if (!hasUnderstanding(task)) return "内容识别完成后开放";
    return null;
  }
  return null;
}

/**
 * 当前处理进度下最靠后的可进子页：打开一条任务时默认落在这一页。
 *
 * 由「产物存在与否」直接判定，不逐项试探开放条件——产物决定开放条件，两处各写一遍
 * 迟早会不一致（子页显示可用、默认跳转却选了别的页）。这也是用户最想先看到的东西：
 * 有结论就看结论，没结论但有识别就看讲了什么，什么都没有就看处理情况。
 */
export function deepestOpenSection(task: Task): TaskSection {
  if (!processingFinished(task)) return "media";
  if (task.review != null) return "review";
  if (hasUnderstanding(task)) return "understanding";
  return "media";
}

/**
 * 轻量 hash 路由。
 *
 * 只需要表达「当前在看哪一屏」，因此用 `location.hash` 而不是引入路由库：
 * 新增的依赖要能摊平它的维护成本，而这里只有两种形态——新建任务页与某条任务的结果页。
 *
 * | hash                        | 视图            |
 * | --------------------------- | --------------- |
 * | （空）/ #/new               | 新建任务        |
 * | #/tasks/:id                 | 任务结果 · 默认子页 |
 * | #/tasks/:id/media           | 任务结果 · 视频信息 |
 * | #/tasks/:id/understanding   | 任务结果 · 内容理解 |
 * | #/tasks/:id/review          | 任务结果 · 复盘分析 |
 * | #/accounts                   | 账号管理（仅管理员可见） |
 *
 * V0.4.1 把任务结果页拆成三个子页，子页也写进 hash：刷新与前进/后退要在子页之间也成立，
 * 否则「打开时落在复盘结论、刷新后又回到顶部」这种跳变会一直存在。
 *
 * 路由同时承担两件事：刷新后停在原处，以及让浏览器前进/后退可用。
 */

import { useCallback, useEffect, useState } from "react";

/**
 * 侧栏主导航；数据分析与设置为预留入口，本版没有对应后端能力。
 *
 * `accounts`（V0.5.2）已接入，但只对管理员渲染——它到底显不显示由 `App` 按会话里的
 * 角色决定，路由这一层不掺和权限判断，只负责「这个 hash 对应哪一屏」。
 */
export type NavKey = "tasks" | "analytics" | "settings" | "accounts";

/** 任务结果页的三个子页。取值即 hash 里的那一段，两者保持一致便于对照。 */
export type TaskSection = "media" | "understanding" | "review";

/** 打开一条任务时未指定子页用的占位值：由详情页按处理进度决定落在哪一页。 */
export const DEFAULT_SECTION: TaskSection = "media";

export type Route = {
  /** 主区当前显示什么 */
  view: "new" | "task" | NavKey;
  /** 任务结果视图对应的任务 id，其余视图为 null */
  taskId: string | null;
  /**
   * 任务结果视图的子页，省略即默认子页。
   *
   * 这里只表达「hash 上写了什么」：`#/tasks/:id` 不带子页时是默认值，具体落在哪一页由
   * 详情页按处理进度决定并按需改写到 hash 上（见 `DEFAULT_SECTION` 的说明）。
   */
  section?: TaskSection;
};

const TASK_PREFIX = "#/tasks/";
const SECTIONS: TaskSection[] = ["media", "understanding", "review"];

function isSection(value: string): value is TaskSection {
  return (SECTIONS as string[]).includes(value);
}

function parse(hash: string): Route {
  if (hash.startsWith(TASK_PREFIX)) {
    const rest = hash.slice(TASK_PREFIX.length);
    // id 与子页以 / 分隔；id 自身经 encodeURIComponent 处理，不含裸斜杠
    const slash = rest.indexOf("/");
    const rawId = slash === -1 ? rest : rest.slice(0, slash);
    const rawSection = slash === -1 ? "" : rest.slice(slash + 1);
    const id = decodeURIComponent(rawId).trim();
    if (id === "") return { view: "new", taskId: null };

    // 未写子页或写了一个不认识的子页，都交给详情页按处理进度决定
    return {
      view: "task",
      taskId: id,
      section: isSection(rawSection) ? rawSection : DEFAULT_SECTION,
    };
  }
  if (hash === "#/analytics") return { view: "analytics", taskId: null };
  if (hash === "#/settings") return { view: "settings", taskId: null };
  if (hash === "#/accounts") return { view: "accounts", taskId: null };
  return { view: "new", taskId: null };
}

function hrefOf(route: Route): string {
  if (route.view === "task" && route.taskId) {
    const base = `${TASK_PREFIX}${encodeURIComponent(route.taskId)}`;
    // 默认子页不必写进 hash：保持 #/tasks/:id 与上一版一致，旧链接照常可用
    const section = route.section ?? DEFAULT_SECTION;
    return section === DEFAULT_SECTION ? base : `${base}/${section}`;
  }
  if (route.view === "analytics") return "#/analytics";
  if (route.view === "settings") return "#/settings";
  if (route.view === "accounts") return "#/accounts";
  return "#/new";
}

/** 任务的默认子页链接：由详情页在挂载时改写成按进度选中的那一页。 */
export function taskHref(taskId: string): string {
  return `${TASK_PREFIX}${encodeURIComponent(taskId)}`;
}

/** 某个子页的链接，供子导航渲染成可复制的地址。 */
export function taskSectionHref(taskId: string, section: TaskSection): string {
  return hrefOf({ view: "task", taskId, section });
}

export function useRoute(): {
  route: Route;
  navigate: (route: Route) => void;
} {
  // 每次渲染都直接读 location.hash，而不是挂在 state 上等 hashchange 事件：
  // 「赋值 hash」与「事件派发」之间是一个真实的窗口，跨实例、跨 effect 时序都可能错过，
  // 一旦错过，路由就停在旧值上，界面与地址栏说的不是同一件事。本地 hash 是同步可读的，
  // 直接读它就是最可靠的单一事实来源；state 只用于在 hash 变化时触发一次重渲染。
  const [, forceRender] = useState(0);
  const route = parse(window.location.hash);

  useEffect(() => {
    const sync = () => forceRender((n) => n + 1);
    window.addEventListener("hashchange", sync);
    return () => window.removeEventListener("hashchange", sync);
  }, []);

  const navigate = useCallback((next: Route) => {
    const href = hrefOf(next);
    if (window.location.hash === href) {
      // 地址没变但路由对象变了（例如两次改写落在同一个 hash 上）：仍需重渲染一次
      forceRender((n) => n + 1);
      return;
    }
    window.location.hash = href;
  }, []);

  return { route, navigate };
}

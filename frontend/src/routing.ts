/**
 * 轻量 hash 路由。
 *
 * 只需要表达「当前在看哪一屏」，因此用 `location.hash` 而不是引入路由库：
 * 新增的依赖要能摊平它的维护成本，而这里只有两种形态——新建任务页与某条任务的结果页。
 *
 * | hash         | 视图    |
 * | ------------ | ------- |
 * | （空）/ #/new | 新建任务 |
 * | #/tasks/:id   | 任务结果 |
 *
 * 路由同时承担两件事：刷新后停在原处，以及让浏览器前进/后退可用。
 */

import { useEffect, useState } from "react";

/** 侧栏主导航的三项；数据分析与设置为预留入口，本版没有对应后端能力。 */
export type NavKey = "tasks" | "analytics" | "settings";

export type Route = {
  /** 主区当前显示什么 */
  view: "new" | "task" | NavKey;
  /** 任务结果视图对应的任务 id，其余视图为 null */
  taskId: string | null;
};

const TASK_PREFIX = "#/tasks/";

function parse(hash: string): Route {
  if (hash.startsWith(TASK_PREFIX)) {
    const id = decodeURIComponent(hash.slice(TASK_PREFIX.length)).trim();
    return id === "" ? { view: "new", taskId: null } : { view: "task", taskId: id };
  }
  if (hash === "#/analytics") return { view: "analytics", taskId: null };
  if (hash === "#/settings") return { view: "settings", taskId: null };
  return { view: "new", taskId: null };
}

function hrefOf(route: Route): string {
  if (route.view === "task" && route.taskId) return `${TASK_PREFIX}${encodeURIComponent(route.taskId)}`;
  if (route.view === "analytics") return "#/analytics";
  if (route.view === "settings") return "#/settings";
  return "#/new";
}

export function taskHref(taskId: string): string {
  return `${TASK_PREFIX}${encodeURIComponent(taskId)}`;
}

export function useRoute(): {
  route: Route;
  navigate: (route: Route) => void;
} {
  const [route, setRoute] = useState<Route>(() => parse(window.location.hash));

  useEffect(() => {
    const onHashChange = () => setRoute(parse(window.location.hash));
    window.addEventListener("hashchange", onHashChange);
    return () => window.removeEventListener("hashchange", onHashChange);
  }, []);

  const navigate = (next: Route) => {
    const href = hrefOf(next);
    // 同一 hash 再赋值不会触发 hashchange，因此直接写 state 兜底
    if (window.location.hash === href) {
      setRoute(next);
      return;
    }
    window.location.hash = href;
  };

  return { route, navigate };
}

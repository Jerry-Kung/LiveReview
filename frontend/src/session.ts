/**
 * 登录态：当前登录者的唯一来源。
 *
 * 与路由一样刻意保持为一个模块级的小store，而不是引入状态管理库：这里要记住的只有
 * 「当前是谁」，读写它的也只有壳层、页眉与登录页三处。
 *
 * 首次进入时先向后端问一次（`GET /api/auth/session`）——登录态存在 HttpOnly Cookie
 * 里，前端读不到，只能问服务端。因此壳层有三种状态：还没问完（loading）、没登录
 * （anonymous）、已登录。把 loading 与 anonymous 分开是必要的：混在一起会让刷新页面
 * 时先闪一下登录页再跳进工作台。
 */

import { useCallback, useEffect, useState } from "react";

export type SessionUser = {
  /** 登录账号，鉴权用它 */
  username: string;
  /** 界面上的称呼 */
  display_name: string;
  /**
   * 账号角色（V0.5.2）：`admin` 或 `member`。
   *
   * 界面拿它决定要不要显示「账号管理」入口。这只是可见性，不是安全边界——后端对每个
   * 管理接口都单独判角色，绕过界面直接敲 hash 或调接口一样会被拦。
   */
  role: string;
};

export type SessionState =
  | { status: "loading"; user: null; reason: null }
  | { status: "anonymous"; user: null; reason: AnonymousReason }
  | { status: "authenticated"; user: SessionUser; reason: null };

/**
 * 为什么停在登录页。
 *
 * - `initial`：还没登录过（首次访问）。
 * - `expired`：登录过，但会话被服务端判为失效（过期、被踢、服务换了签名密钥）。
 * - `signed-out`：使用者自己点了退出登录。
 *
 * 区分三者的意义只在文案：`expired` 要说清「是会话过期，不是你的操作有误」，
 * 否则用户会怀疑自己刚才哪一步做错了。
 */
export type AnonymousReason = "initial" | "expired" | "signed-out";

/** 会话失效（任一接口返回 401）时通知壳层退回登录页。 */
type UnauthorizedListener = () => void;

let listener: UnauthorizedListener | null = null;

/** 由壳层注册：接口层不直接依赖 React 组件，只在这里挂一个回调。 */
export function onUnauthorized(fn: UnauthorizedListener | null): void {
  listener = fn;
}

/**
 * 会话失效通知。由 `api.ts` 在收到 401 时调用。
 *
 * 之所以放在这里而不是让每个调用点自己处理：任务轮询、分片上传、报告下载都会撞上
 * 会话过期，逐个判断既容易漏，又会在同一时刻弹出多个登录页。
 */
export function notifyUnauthorized(): void {
  listener?.();
}

/** 查询当前登录态。401 是「还没登录」，不是错误。 */
export async function fetchSession(): Promise<SessionUser | null> {
  const response = await fetch("/api/auth/session", { credentials: "same-origin" });
  if (response.status === 401) return null;
  if (!response.ok) {
    throw new Error(`读取登录状态失败（HTTP ${response.status}）`);
  }
  const data = (await response.json()) as { user: SessionUser };
  return data.user;
}

export async function login(username: string, password: string): Promise<SessionUser> {
  const response = await fetch("/api/auth/login", {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username, password }),
  });
  if (!response.ok) {
    let message = `登录失败（HTTP ${response.status}）`;
    try {
      const body = await response.json();
      if (typeof body?.detail === "string") message = body.detail;
    } catch {
      // 保留默认文案
    }
    throw new Error(message);
  }
  const data = (await response.json()) as { user: SessionUser };
  return data.user;
}

export async function logout(): Promise<void> {
  // 退出失败也让本地回到未登录：服务端那次请求的成败不该把用户卡在工作台里
  try {
    await fetch("/api/auth/logout", { method: "POST", credentials: "same-origin" });
  } catch {
    // 网络异常：Cookie 仍在，但用户意图是退出，本地不再显示数据
  }
}

export function useSession(): {
  session: SessionState;
  setUser: (user: SessionUser) => void;
  signOut: () => void;
  reload: () => void;
} {
  const [session, setSession] = useState<SessionState>({
    status: "loading",
    user: null,
    reason: null,
  });

  const reload = useCallback(() => {
    setSession({ status: "loading", user: null, reason: null });
    void fetchSession()
      .then((user) =>
        setSession(
          user
            ? { status: "authenticated", user, reason: null }
            : { status: "anonymous", user: null, reason: "initial" },
        ),
      )
      .catch(() => {
        // 读取失败（后端不可达等）时按未登录处理：登录页会显示后端给出的失败原因
        setSession({ status: "anonymous", user: null, reason: "initial" });
      });
  }, []);

  useEffect(() => {
    reload();
  }, [reload]);

  // 任一接口返回 401 即回到登录页。这里只在**已经登录过**的情况下报「会话失效」：
  // 未登录时的 401 是正常的，不该被解释成出了问题。
  useEffect(() => {
    onUnauthorized(() =>
      setSession((current) =>
        current.status === "authenticated"
          ? { status: "anonymous", user: null, reason: "expired" }
          : current,
      ),
    );
    return () => onUnauthorized(null);
  }, []);

  const setUser = useCallback((user: SessionUser) => {
    setSession({ status: "authenticated", user, reason: null });
  }, []);

  const signOut = useCallback(() => {
    setSession({ status: "anonymous", user: null, reason: "signed-out" });
    void logout();
  }, []);

  return { session, setUser, signOut, reload };
}

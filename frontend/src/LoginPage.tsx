/**
 * 登录页：未登录时占据整个视口，登录成功后才进入工作台。
 *
 * 这是唯一一处「后端还没给身份」的界面，因此它自带页脚说明——工作台里的所有数据接口
 * 都要求登录，外部访问者拿到的是一个空壳页面。
 *
 * 与旧版的差别：旧版是一个弹层，把「账号 + 角色」提交给前端内存，不请求后端、不校验密码，
 * 界面上也如实写着「预留功能区」。本版改成真实登录，凭据送后端比对，失败原因原样呈现。
 * 角色下拉一并去掉：本版没有角色机制，让它留在界面上等于暗示存在权限差异。
 */

import { useEffect, useRef, useState } from "react";
import type { AnonymousReason } from "./session";

/** 停在登录页的原因对应的提示语；首次访问不需要任何解释。 */
const REASON_NOTICE: Record<AnonymousReason, string | null> = {
  initial: null,
  expired: "登录状态已失效，请重新登录。",
  "signed-out": "你已退出登录。",
};

export default function LoginPage({
  onSignIn,
  reason = "initial",
}: {
  /** 提交凭据：成功时由调用方切换会话，失败时抛出可展示的错误。 */
  onSignIn: (username: string, password: string) => Promise<void>;
  /** 停在登录页的原因，决定是否需要一句解释。 */
  reason?: AnonymousReason;
}) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState(false);
  const usernameRef = useRef<HTMLInputElement>(null);
  const notice = REASON_NOTICE[reason];

  // 进入页面即聚焦账号框，少一次点击
  useEffect(() => {
    usernameRef.current?.focus();
  }, []);

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (pending) return;
    const account = username.trim();
    if (account === "" || password === "") {
      setError("请填写账号与密码。");
      return;
    }
    setPending(true);
    setError(null);
    try {
      await onSignIn(account, password);
    } catch (err) {
      // 保留已填的账号，只清掉密码：失败后重新输入时，账号通常是对的
      setPassword("");
      setError(err instanceof Error ? err.message : "登录失败，请重试");
    } finally {
      setPending(false);
    }
  };

  return (
    <div className="login-screen">
      <main className="login-card" aria-labelledby="login-title">
        <div className="login-brand">
          <span className="brand-mark" aria-hidden="true">
            <svg viewBox="0 0 24 24" width={14} height={14} aria-hidden="true">
              <path d="M9 6.5l8.5 5.5L9 17.5z" fill="currentColor" />
            </svg>
          </span>
          <h1 className="brand-name" id="login-title">
            LiveReview
          </h1>
          <span className="brand-scope">直播复盘工作台</span>
        </div>

        {notice && <p className="login-notice">{notice}</p>}

        <form className="login-form" onSubmit={submit}>
          <label className="field">
            <span className="field-label">账号</span>
            <input
              ref={usernameRef}
              className="field-input"
              type="text"
              name="username"
              autoComplete="username"
              value={username}
              onChange={(event) => setUsername(event.target.value)}
            />
          </label>

          <label className="field">
            <span className="field-label">密码</span>
            <input
              className="field-input"
              type="password"
              name="password"
              autoComplete="current-password"
              value={password}
              onChange={(event) => setPassword(event.target.value)}
            />
          </label>

          {error && (
            <p className="field-notice" role="alert">
              {error}
            </p>
          )}

          <button type="submit" className="btn btn--primary login-submit" disabled={pending}>
            {pending ? "正在登录…" : "登录"}
          </button>
        </form>

        <p className="login-foot">
          本系统暂不提供自助注册，账号由管理员配置。登录后所有使用者共享同一套任务数据。
        </p>
      </main>
    </div>
  );
}

/**
 * 页眉：项目名称与服务/用户区。
 *
 * 用户登录是预留功能区，只承载界面状态，后端尚未提供账号体系，
 * 因此界面明确标注「未接入」，不做假的登录成功反馈。
 */

import type { Health } from "./api";

export type SessionUser = { name: string; role: string } | null;

const STORAGE_LABELS: Record<string, string> = {
  configured: "已配置",
  not_configured: "未配置",
  unavailable: "不可用",
};

function storageText(health: Health | null): string {
  if (health === null) return "—";
  const label = STORAGE_LABELS[health.storage ?? ""] ?? "未知";
  const missing = health.storage_missing ?? [];
  return missing.length > 0 ? `${label}（缺少 ${missing.join("、")}）` : label;
}

function storageState(health: Health | null): "ok" | "attention" {
  if (health === null) return "attention";
  return health.storage === "configured" ? "ok" : "attention";
}

export default function Header({
  health,
  user,
  onSignIn,
  onSignOut,
}: {
  health: Health | null;
  user: SessionUser;
  onSignIn: () => void;
  onSignOut: () => void;
}) {
  const connected = health !== null;

  return (
    <header className="masthead">
      <div className="masthead-row">
        <div className="masthead-brand">
          <span className="mark" aria-hidden="true" />
          <h1 className="masthead-name">直播视频复盘分析工作台</h1>
        </div>

        <div className="masthead-side">
          {user ? (
            <div className="account">
              <span className="avatar" aria-hidden="true">
                {user.name.slice(0, 1)}
              </span>
              <span className="account-text">
                <strong>{user.name}</strong>
                <span>{user.role}</span>
              </span>
              <button type="button" className="btn btn--quiet" onClick={onSignOut}>
                退出
              </button>
            </div>
          ) : (
            <button type="button" className="btn btn--quiet" onClick={onSignIn}>
              用户登录
            </button>
          )}
        </div>
      </div>

      <dl className="runtime" aria-label="服务状态">
        <div className="runtime-item">
          <dt>后端</dt>
          <dd>
            <span className="dot" data-state={connected ? "ok" : "attention"} aria-hidden="true" />
            {connected ? "已连接" : "未连接"}
          </dd>
        </div>
        <div className="runtime-item">
          <dt>对象存储</dt>
          <dd>
            <span className="dot" data-state={storageState(health)} aria-hidden="true" />
            {storageText(health)}
          </dd>
        </div>
        <div className="runtime-item">
          <dt>版本</dt>
          <dd>{health?.version ?? "—"}</dd>
        </div>
      </dl>
    </header>
  );
}

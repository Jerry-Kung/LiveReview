import { useEffect, useState } from "react";
import UploadPanel from "./UploadPanel";

type Health = {
  status: string;
  service: string;
  version: string;
  environment: string;
  database: string;
  storage?: string;
  storage_missing?: string[];
};

const STORAGE_LABELS: Record<string, string> = {
  configured: "已配置",
  not_configured: "未配置",
  unavailable: "不可用",
};

type StatusState = "ok" | "attention";

function storageText(health: Health | null): string {
  if (health === null) return "—";
  const label = STORAGE_LABELS[health.storage ?? ""] ?? "未知";
  const missing = health.storage_missing ?? [];
  return missing.length > 0 ? `${label}（缺少 ${missing.join("、")}）` : label;
}

function storageState(health: Health | null): StatusState {
  if (health === null) return "attention";
  return health.storage === "configured" ? "ok" : "attention";
}

/** 极简 hash 路由：只区分「上传」与「服务状态」两屏，不引入路由依赖。 */
function useView(): [string, (view: string) => void] {
  const [view, setView] = useState(() => window.location.hash.replace("#", "") || "upload");

  useEffect(() => {
    const onChange = () => setView(window.location.hash.replace("#", "") || "upload");
    window.addEventListener("hashchange", onChange);
    return () => window.removeEventListener("hashchange", onChange);
  }, []);

  return [view, (next: string) => { window.location.hash = next; }];
}

export default function App() {
  const [health, setHealth] = useState<Health | null>(null);
  const [view, goTo] = useView();

  useEffect(() => {
    let cancelled = false;
    fetch("/health")
      .then((res) => (res.ok ? res.json() : null))
      .then((data: Health | null) => {
        if (!cancelled) setHealth(data);
      })
      .catch(() => {
        if (!cancelled) setHealth(null);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const connected = health !== null;

  return (
    <main className="shell">
      <h1 className="title">LiveReview</h1>
      <p className="meta">
        版本 {health?.version ?? "—"} · 环境 {health?.environment ?? "—"}
      </p>

      <nav className="tabs" aria-label="页面切换">
        <button
          type="button"
          data-active={view === "upload"}
          onClick={() => goTo("upload")}
        >
          上传录屏
        </button>
        <button
          type="button"
          data-active={view !== "upload"}
          onClick={() => goTo("status")}
        >
          服务状态
        </button>
      </nav>

      {view === "upload" ? (
        <UploadPanel />
      ) : (
        <div className="status-group">
          <p className="status-line" data-state={connected ? "ok" : "attention"}>
            后端：{connected ? "已连接" : "未连接"}
          </p>
          <p className="status-line" data-state={storageState(health)}>
            对象存储：{storageText(health)}
          </p>
        </div>
      )}
    </main>
  );
}

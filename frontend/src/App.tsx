import { useEffect, useState } from "react";

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

export default function App() {
  const [health, setHealth] = useState<Health | null>(null);

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
      <div className="status-group">
        <p className="status-line" data-state={connected ? "ok" : "attention"}>
          后端：{connected ? "已连接" : "未连接"}
        </p>
        <p className="status-line" data-state={storageState(health)}>
          对象存储：{storageText(health)}
        </p>
      </div>
    </main>
  );
}

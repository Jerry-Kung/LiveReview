import { useEffect, useState } from "react";

type Health = {
  status: string;
  service: string;
  version: string;
  environment: string;
  database: string;
};

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
    <main>
      <h1>LiveReview</h1>
      <p>版本 {health?.version ?? "—"} · 环境 {health?.environment ?? "—"}</p>
      <p>后端：{connected ? "已连接" : "未连接"}</p>
    </main>
  );
}

/**
 * 侧栏：发起新任务与历史记录。
 *
 * 历史记录读的是既有的任务列表接口（`GET /api/tasks`），用于回到之前上传的录屏并查看结果；
 * 登录体系与多用户任务归属尚未实现，因此列表列出的是本服务上的全部任务，不区分账号，
 * 界面上不为此加说明性标注。
 */

import type { Task } from "./api";
import { PLACEHOLDER, formatDuration, taskStatusLabel } from "./format";

export default function Sidebar({
  tasks,
  loading,
  error,
  activeId,
  onNewTask,
  onOpenTask,
}: {
  tasks: Task[];
  loading: boolean;
  error: string | null;
  activeId: string | null;
  onNewTask: () => void;
  onOpenTask: (taskId: string) => void;
}) {
  return (
    <aside className="rail" aria-label="工作区导航">
      <button type="button" className="rail-new" onClick={onNewTask}>
        <span className="rail-new-mark" aria-hidden="true" />
        新建上传任务
      </button>

      <section className="rail-section">
        <h2 className="rail-title">历史记录</h2>
        <p className="rail-note">按上传时间倒序，点击查看结果。</p>

        {loading && <p className="rail-empty">正在读取…</p>}
        {!loading && error !== null && <p className="rail-empty rail-empty--warn">{error}</p>}
        {!loading && error === null && tasks.length === 0 && (
          <p className="rail-empty">还没有任务。上传一场录屏后，记录会出现在这里。</p>
        )}

        {!loading && error === null && tasks.length > 0 && (
          <ul className="history">
            {tasks.map((task) => (
              <li key={task.id}>
                <button
                  type="button"
                  className="history-item"
                  data-active={task.id === activeId}
                  onClick={() => onOpenTask(task.id)}
                >
                  <span className="history-name">{task.filename ?? "未命名录屏"}</span>
                  <span className="history-meta">
                    <span className="history-status" data-state={task.status === "succeeded" ? "ok" : "attention"}>
                      {taskStatusLabel(task.status)}
                    </span>
                    <span className="history-time">
                      {task.metadata
                        ? formatDuration(task.metadata.duration_seconds)
                        : PLACEHOLDER}
                    </span>
                  </span>
                  <span className="history-stamp">{formatStamp(task.created_at)}</span>
                </button>
              </li>
            ))}
          </ul>
        )}
      </section>
    </aside>
  );
}

/** 上传时间：后端存 ISO 时间串，按本地时区展示到分钟。 */
function formatStamp(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return PLACEHOLDER;
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

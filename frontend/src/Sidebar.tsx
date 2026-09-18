/**
 * 侧栏：主导航 + 最近任务。
 *
 * 「最近任务」读的是既有的任务列表接口（`GET /api/tasks`），用于回到之前上传的录屏并查看
 * 结果；登录体系与多用户任务归属尚未实现，因此列表列出的是本服务上的全部任务，不区分账号，
 * 界面上不为此加说明性标注。
 *
 * 「数据分析」「设置」是导航结构上的预留入口，本版没有对应后端能力，点击不跳转，并用
 * 「未接入」明确说明——不做空白页伪装成功能，也不把它们藏起来让导航只剩一项。
 *
 * 任务状态用小圆点 + 文字表达（见 `.status`），不用彩色 Badge：状态是辅助信息，
 * 不该抢页面的主视觉。
 */

import type { Task } from "./api";
import { PLACEHOLDER, taskStatusLabel, taskStatusTone } from "./format";
import { IconChart, IconList, IconSettings } from "./icons";
import type { NavKey } from "./routing";

/** 侧栏筛选：文件名是用户手里唯一认得出的标识，因此只按文件名匹配。 */
export function filterTasks(tasks: Task[], query: string): Task[] {
  const keyword = query.trim().toLowerCase();
  if (keyword === "") return tasks;
  return tasks.filter((task) => (task.filename ?? "").toLowerCase().includes(keyword));
}

export default function Sidebar({
  tasks,
  loading,
  error,
  activeTaskId,
  activeNav,
  query,
  onOpenTask,
  onSelectNav,
}: {
  tasks: Task[];
  loading: boolean;
  error: string | null;
  /** 当前打开的任务 id，用于高亮最近任务列表 */
  activeTaskId: string | null;
  /** 当前主导航项 */
  activeNav: NavKey;
  /** 页眉搜索框的筛选词 */
  query: string;
  onOpenTask: (taskId: string) => void;
  onSelectNav: (key: NavKey) => void;
}) {
  const visible = filterTasks(tasks, query);

  return (
    <aside className="rail" aria-label="工作区导航">
      <nav className="rail-nav" aria-label="主导航">
        <button
          type="button"
          className="rail-item"
          aria-current={activeNav === "tasks" ? "page" : undefined}
          onClick={() => onSelectNav("tasks")}
        >
          <IconList />
          任务列表
        </button>
        <button
          type="button"
          className="rail-item"
          aria-current={activeNav === "analytics" ? "page" : undefined}
          onClick={() => onSelectNav("analytics")}
        >
          <IconChart />
          数据分析
          <span className="rail-item-tag">未接入</span>
        </button>
        <button
          type="button"
          className="rail-item"
          aria-current={activeNav === "settings" ? "page" : undefined}
          onClick={() => onSelectNav("settings")}
        >
          <IconSettings />
          设置
          <span className="rail-item-tag">未接入</span>
        </button>
      </nav>

      <section aria-label="最近任务">
        <h2 className="rail-section-title">
          <span>最近任务</span>
          {tasks.length > 0 && <span className="num">{visible.length}</span>}
        </h2>

        {loading && <p className="rail-empty">正在读取…</p>}
        {!loading && error !== null && <p className="rail-empty rail-empty--warn">{error}</p>}

        {!loading && error === null && tasks.length === 0 && (
          <p className="rail-empty">还没有任务。上传一场录屏后，记录会出现在这里。</p>
        )}

        {!loading && error === null && tasks.length > 0 && visible.length === 0 && (
          <p className="rail-empty">没有匹配「{query.trim()}」的任务。</p>
        )}

        {!loading && error === null && visible.length > 0 && (
          <ul className="rail-list">
            {visible.map((task) => (
              <li key={task.id}>
                <button
                  type="button"
                  className="recent-item"
                  aria-current={task.id === activeTaskId ? "true" : undefined}
                  onClick={() => onOpenTask(task.id)}
                >
                  <span className="recent-name">{task.filename ?? "未命名录屏"}</span>
                  <span className="recent-meta">
                    <span className="status" data-tone={taskStatusTone(task.status)}>
                      {taskStatusLabel(task.status)}
                    </span>
                    <span className="recent-stamp num">{formatStamp(task.created_at)}</span>
                  </span>
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

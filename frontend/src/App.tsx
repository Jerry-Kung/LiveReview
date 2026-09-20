/**
 * 应用壳层：页眉 + 侧栏（主导航 / 最近任务）+ 主工作区。
 *
 * 主区按 hash 路由显示三种内容：新建任务页、从最近任务里打开的一条既有任务、以及两个
 * 预留功能页（数据分析 / 设置，本版没有后端能力，只给出说明页）。本业务链路始终是
 * 「上传 → 处理 → 结果」，路由只负责决定当前看哪一屏。
 */

import { useCallback, useEffect, useState } from "react";
import Header, { type SessionUser } from "./Header";
import LoginDialog from "./LoginDialog";
import Sidebar from "./Sidebar";
import Workbench from "./Workbench";
import { IconChart, IconSettings } from "./icons";
import { DEFAULT_SECTION, useRoute, type TaskSection } from "./routing";
import { fetchTasks, type Task } from "./api";

/** 预留功能页：说明「这里会有什么、现在为什么没有」，不假装功能已存在。 */
function PlaceholderPage({
  title,
  note,
  icon,
}: {
  title: string;
  note: string;
  icon: React.ReactNode;
}) {
  return (
    <div className="page">
      <h2 className="page-title">{title}</h2>
      <div className="panel">
        <div className="placeholder">
          {icon}
          <p className="placeholder-title">该功能尚未接入</p>
          <p className="placeholder-note">{note}</p>
        </div>
      </div>
    </div>
  );
}

export default function App() {
  const [tasks, setTasks] = useState<Task[]>([]);
  const [historyLoading, setHistoryLoading] = useState(true);
  const [historyError, setHistoryError] = useState<string | null>(null);
  const [loginOpen, setLoginOpen] = useState(false);
  const [user, setUser] = useState<SessionUser>(null);
  const [query, setQuery] = useState("");
  // 递增计数：新建上传任务时让 Workbench 重挂载，丢掉上一次任务的界面状态
  const [intakeKey, setIntakeKey] = useState(0);
  const { route, navigate } = useRoute();

  const loadTasks = useCallback(async () => {
    try {
      const data = await fetchTasks();
      setTasks(data.items);
      setHistoryError(null);
    } catch (err) {
      // 最近任务读不到不影响主区的上传与处理，只在侧栏说明原因
      setHistoryError(err instanceof Error ? err.message : "任务列表读取失败");
    } finally {
      setHistoryLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadTasks();
  }, [loadTasks]);

  const handleNewTask = () => {
    setIntakeKey((value) => value + 1);
    navigate({ view: "new", taskId: null });
  };

  const openTask = (taskId: string) => {
    setIntakeKey((value) => value + 1);
    navigate({ view: "task", taskId });
  };

  /**
   * 切换任务详情的子页。
   *
   * 任务 id 从**当前 hash** 上取，而不是从本次渲染的 `route.taskId`：上传刚完成时
   * 地址栏还是 `#/new`（任务详情还没被路由认领），此时若按 `route.taskId` 拼地址，
   * 会算出 `#/new` 这个「什么都不改」的地址，子页跳转就静默失效了。
   */
  const handleSection = (section: TaskSection) => {
    const current = window.location.hash;
    const taskId = current.startsWith("#/tasks/")
      ? decodeURIComponent(current.slice("#/tasks/".length).split("/")[0]).trim()
      : route.taskId;
    if (!taskId) return;
    navigate({ view: "task", taskId, section });
  };

  return (
    <div className="app">
      <Header
        user={user}
        query={query}
        onQuery={setQuery}
        onNewTask={handleNewTask}
        onSignIn={() => setLoginOpen(true)}
        onSignOut={() => setUser(null)}
      />

      <div className="layout">
        <Sidebar
          tasks={tasks}
          loading={historyLoading}
          error={historyError}
          activeTaskId={route.view === "task" ? route.taskId : null}
          activeNav={route.view === "analytics" || route.view === "settings" ? route.view : "tasks"}
          query={query}
          onOpenTask={openTask}
          onSelectNav={(key) => {
            if (key === "tasks") {
              handleNewTask();
              return;
            }
            navigate({ view: key, taskId: null });
          }}
        />

        <main className="stage" aria-label="工作区">
          {route.view === "analytics" && (
            <PlaceholderPage
              title="数据分析"
              note="按店铺、时段、主播汇总多场直播的复盘结论。需要先有多场已复盘的任务，且后端提供聚合接口。"
              icon={<IconChart size={20} />}
            />
          )}

          {route.view === "settings" && (
            <PlaceholderPage
              title="设置"
              note="账号与权限、切片时长、模型选择等运行参数。当前这些取值由后端配置文件决定，界面暂不提供修改入口。"
              icon={<IconSettings size={20} />}
            />
          )}

          {(route.view === "new" || route.view === "task") && (
            <Workbench
              key={`${intakeKey}-${route.taskId ?? "new"}`}
              taskId={route.taskId}
              section={route.section ?? DEFAULT_SECTION}
              onSection={handleSection}
              onTaskChange={loadTasks}
              onBackToList={handleNewTask}
            />
          )}
        </main>
      </div>

      <LoginDialog
        open={loginOpen}
        onClose={() => setLoginOpen(false)}
        onSignIn={(next) => {
          setUser(next);
          setLoginOpen(false);
        }}
      />
    </div>
  );
}

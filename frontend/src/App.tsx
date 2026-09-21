/**
 * 应用壳层：登录门槛 + 页眉 + 侧栏（主导航 / 最近任务）+ 主工作区。
 *
 * 登录态是这一层的第一道判断：未登录时**不渲染工作台**，只给登录页。这不只是界面上的
 * 遮掩——工作台里的每个请求都要带会话 Cookie，后端会把未登录的请求一并拒掉，因此
 * 这里省掉的是一串注定失败的请求，而不是一次安全检查。真正的边界在后端。
 *
 * 主区按 hash 路由显示：新建任务页、从最近任务里打开的一条既有任务、账号管理页，以及
 * 两个预留功能页（数据分析 / 设置，本版没有后端能力，只给出说明页）。本业务链路始终是
 * 「上传 → 处理 → 结果」，路由只负责决定当前看哪一屏。
 *
 * 账号管理（V0.5.2）是唯一一处按角色决定可见性的地方。这里只是**界面收敛**：非管理员
 * 既看不到侧栏入口，手敲 `#/accounts` 也只会看到说明页；真正的边界在后端，那一组接口对
 * 非管理员一律 403，绕过界面直接调接口拿不到任何东西。
 */

import { useCallback, useEffect, useState } from "react";
import AccountsPage from "./AccountsPage";
import Header from "./Header";
import LoginPage from "./LoginPage";
import Sidebar from "./Sidebar";
import Workbench from "./Workbench";
import { IconChart, IconSettings, IconUsers } from "./icons";
import { DEFAULT_SECTION, useRoute, type TaskSection } from "./routing";
import { login } from "./session";
import { useSession } from "./session";
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
  const [query, setQuery] = useState("");
  // 递增计数：新建上传任务时让 Workbench 重挂载，丢掉上一次任务的界面状态
  const [intakeKey, setIntakeKey] = useState(0);
  const { route, navigate } = useRoute();
  const { session, setUser, signOut } = useSession();

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

  const authenticated = session.status === "authenticated";
  // 角色只在已登录时才有值，未登录的会话对象上没有 user
  const isAdmin = session.status === "authenticated" && session.user.role === "admin";

  useEffect(() => {
    // 只在已登录后取任务列表：未登录时这个请求必然 401，还会把「会话失效」的提示
    // 挂在刚打开页面的用户脸上——他本来就没登录，不需要被通知会话过期
    if (authenticated) void loadTasks();
  }, [authenticated, loadTasks]);

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

  const handleSignIn = async (username: string, password: string) => {
    const user = await login(username, password);
    // 重新拉一次最近任务：登录前那份列表可能来自上一次会话，不该直接沿用
    setHistoryLoading(true);
    setUser(user);
  };

  const handleSignOut = () => {
    signOut();
    // 退出后清掉任务与筛选词：下次登录时不应先看到上一位使用者的列表
    setTasks([]);
    setQuery("");
    setHistoryLoading(true);
  };

  if (session.status === "loading") {
    // 会话在 HttpOnly Cookie 里，前端读不到，必须先问一次后端。
    // 这一屏只在首次进入时出现一瞬，作用是不闪一下登录页再跳进工作台。
    return (
      <div className="boot" role="status" aria-live="polite">
        正在载入…
      </div>
    );
  }

  if (session.status === "anonymous") {
    return <LoginPage onSignIn={handleSignIn} reason={session.reason} />;
  }

  return (
    <div className="app">
      <Header
        user={session.user}
        query={query}
        onQuery={setQuery}
        onNewTask={handleNewTask}
        onSignOut={handleSignOut}
      />

      <div className="layout">
        <Sidebar
          tasks={tasks}
          loading={historyLoading}
          error={historyError}
          activeTaskId={route.view === "task" ? route.taskId : null}
          activeNav={
            route.view === "analytics" || route.view === "settings" || route.view === "accounts"
              ? route.view
              : "tasks"
          }
          query={query}
          isAdmin={isAdmin}
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

          {route.view === "accounts" &&
            (isAdmin ? (
              <AccountsPage />
            ) : (
              <PlaceholderPage
                title="账号管理"
                note="这个模块只对管理员账号开放。你的账号可以使用上传、处理与复盘等全部功能，账号的增删请联系管理员。"
                icon={<IconUsers size={20} />}
              />
            ))}

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
    </div>
  );
}

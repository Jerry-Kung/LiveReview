/**
 * 应用壳层：页眉 + 侧栏（新建任务 / 历史记录）+ 主工作区。
 *
 * 页面只做一件事——把一场录屏从上传带到切片结果。主区在三种内容之间切换：
 * 上传入口、上传与处理过程、从历史记录里打开的一个既有任务。
 */

import { useCallback, useEffect, useState } from "react";
import Header, { type SessionUser } from "./Header";
import LoginDialog from "./LoginDialog";
import Sidebar from "./Sidebar";
import Workbench from "./Workbench";
import { fetchTasks, type Task } from "./api";

export default function App() {
  const [tasks, setTasks] = useState<Task[]>([]);
  const [historyLoading, setHistoryLoading] = useState(true);
  const [historyError, setHistoryError] = useState<string | null>(null);
  const [activeTaskId, setActiveTaskId] = useState<string | null>(null);
  const [loginOpen, setLoginOpen] = useState(false);
  const [user, setUser] = useState<SessionUser>(null);
  // 递增计数：新建上传任务时让 Workbench 重挂载，丢掉上一次任务的界面状态
  const [intakeKey, setIntakeKey] = useState(0);

  const loadTasks = useCallback(async () => {
    try {
      const data = await fetchTasks();
      setTasks(data.items);
      setHistoryError(null);
    } catch (err) {
      // 历史记录读不到不影响主区的上传与处理，只在侧栏说明原因
      setHistoryError(err instanceof Error ? err.message : "历史记录读取失败");
    } finally {
      setHistoryLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadTasks();
  }, [loadTasks]);

  const handleNewTask = () => {
    setActiveTaskId(null);
    setIntakeKey((value) => value + 1);
  };

  const openTask = (taskId: string) => {
    setActiveTaskId(taskId);
    setIntakeKey((value) => value + 1);
  };

  return (
    <div className="app">
      <Header
        user={user}
        onSignIn={() => setLoginOpen(true)}
        onSignOut={() => setUser(null)}
      />

      <div className="layout">
        <Sidebar
          tasks={tasks}
          loading={historyLoading}
          error={historyError}
          activeId={activeTaskId}
          onNewTask={handleNewTask}
          onOpenTask={openTask}
        />

        <main className="stage" aria-label="工作区">
          <Workbench key={intakeKey} taskId={activeTaskId} onTaskChange={loadTasks} />
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

/**
 * 工作区：承接两类内容。
 *
 * 一类是当前这次上传——选文件、分片上传、轮询任务直到出结果；
 * 另一类是用户从最近任务里打开的一个既有任务，只读呈现它的处理结果。
 * 两者共用同一套结果展示（`TaskDetailPage`），区别只在于数据从哪来、能给哪些操作。
 *
 * V0.4 只重组了这里的呈现层：上传状态机、轮询与恢复逻辑与上一版完全一致，因此
 * 「关掉页面不影响后台处理」等行为不受界面重构影响。
 */

import { useCallback, useEffect, useRef, useState } from "react";
import type { ChangeEvent, DragEvent } from "react";
import { pollIntervalMs } from "./polling";
import IntakePage from "./IntakePage";
import TaskDetailPage from "./TaskDetailPage";
import type { UnderstandingActions } from "./Understanding";
import type { ReviewActions } from "./Review";
import type { TaskSection } from "./routing";
import {
  ApiError,
  completeUpload,
  createUpload,
  deleteTask,
  fetchTask,
  fetchUpload,
  retryClipUnderstanding,
  retryTask,
  reviewDownloadUrl,
  sliceFile,
  startReview,
  startUnderstanding,
  uploadChunk,
  type Task,
} from "./api";
import { clearPendingUpload, loadPendingUpload, savePendingUpload } from "./pendingUpload";
import { formatBytes } from "./format";

const CONCURRENCY = 4;

/**
 * 界面阶段。
 *
 * `loading` 是重开页面后的过渡态：这次上传的凭据还在浏览器里，但服务端已经跑到哪一步
 * 要问过才知道（可能仍在收分片、可能已合并入库并在处理），问清楚之前不显示上传入口，
 * 免得用户以为「上次的没了」又传一遍 1～2GB 的文件。
 *
 * `unfinished` 是分片没收齐时的等待态：缺的分片只在用户磁盘上，得等用户重新选一次同一个
 * 文件才能补传。
 */
type Phase =
  | "idle"
  | "loading"
  | "unfinished"
  | "uploading"
  | "assembling"
  | "watching"
  | "done"
  | "error";

/** 上传会话已完成，分片阶段结束：进入「入库中」的等待。 */
const UPLOAD_COMPLETED = "completed";

/**
 * 轮询触发器：taskId 指明看哪个任务，nonce 每次递增。
 *
 * 用单调计数器而不是时间戳，是因为同一毫秒内连续两次触发（如失败后立刻重试）会得到
 * 相同的 `Date.now()`，effect 依赖不变则轮询不会重启，界面会停在中途状态。
 */
let pollNonce = 0;
const nextWatch = (taskId: string) => ({ taskId, nonce: ++pollNonce });

type UploadState = {
  phase: Phase;
  taskId: string | null;
  sentBytes: number;
  totalBytes: number;
  fileName: string | null;
  error: string | null;
  missingChunks: number[];
};

/** 没有未完成的上传时的稳定状态：上传入口。 */
const IDLE: UploadState = {
  phase: "idle",
  taskId: null,
  sentBytes: 0,
  totalBytes: 0,
  fileName: null,
  error: null,
  missingChunks: [],
};

/**
 * 挂载时的起点：先问一次「有没有没传完的上传」。
 *
 * 用 loading 而不是 idle 起步，是因为「还没问」与「问过了、什么都没有」是两件事：
 * 前者显示上传入口会诱导用户把同一个文件再传一遍。
 */
const INITIAL: UploadState = { ...IDLE, phase: "loading" };

function describeError(err: unknown): { message: string; missing: number[] } {
  if (err instanceof ApiError) {
    return { message: err.message, missing: err.missingChunks?.missing_chunks ?? [] };
  }
  return { message: err instanceof Error ? err.message : "上传失败，原因未知", missing: [] };
}

/**
 * 识别操作与结果刷新，供「本次上传」与「历史任务」两条路径共用。
 *
 * `refreshKey` 每次识别状态变化后递增，全文面板据此重新拉取——识别的推进不需要
 * 重取任务详情之外的额外接口，这里是唯一让正文与列表保持同步的地方。
 */
function useUnderstanding(
  task: Task | null,
  setTask: (task: Task) => void,
  setError: (message: string | null) => void
): UnderstandingActions & { refreshKey: number } {
  const [starting, setStarting] = useState(false);
  const [retryingIndex, setRetryingIndex] = useState<number | null>(null);
  const [refreshKey, setRefreshKey] = useState(0);
  const taskId = task?.id ?? null;
  const running = task?.understanding?.status === "running";

  // 识别在后台推进：处于识别中的任务按轮询节奏刷新，直到有终态结果
  useEffect(() => {
    if (!running || taskId === null) return;
    let cancelled = false;
    const timer = window.setInterval(() => {
      void fetchTask(taskId)
        .then((latest) => {
          if (cancelled) return;
          setTask(latest);
          setRefreshKey((prev) => prev + 1);
        })
        // 轮询失败不打断：任务仍在后台跑，下个周期继续
        .catch(() => undefined);
    }, pollIntervalMs());
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [running, taskId, setTask]);

  const onStart = useCallback(async () => {
    if (taskId === null) return;
    setStarting(true);
    setError(null);
    try {
      setTask(await startUnderstanding(taskId));
      setRefreshKey((prev) => prev + 1);
    } catch (err) {
      setError(describeError(err).message);
    } finally {
      setStarting(false);
    }
  }, [taskId, setTask, setError]);

  const onRetryClip = useCallback(
    async (index: number) => {
      if (taskId === null) return;
      setRetryingIndex(index);
      setError(null);
      try {
        setTask(await retryClipUnderstanding(taskId, index));
        setRefreshKey((prev) => prev + 1);
      } catch (err) {
        setError(describeError(err).message);
      } finally {
        setRetryingIndex(null);
      }
    },
    [taskId, setTask, setError]
  );

  return {
    onStart: () => void onStart(),
    onRetryClip: (index: number) => void onRetryClip(index),
    starting,
    retryingIndex,
    refreshKey,
  };
}

/**
 * 复盘操作：与识别分开成两个 hook，因为两者的前置条件、耗时与轮询行为都不同——
 * 识别的主循环在 `useUnderstanding` 里，复盘由 `ReviewBlock` 自己按状态轮询。
 */
function useReview(
  task: Task | null,
  setTask: (task: Task) => void,
  setError: (message: string | null) => void
): ReviewActions {
  const [reviewing, setReviewing] = useState(false);
  const taskId = task?.id ?? null;

  const onReview = useCallback(async () => {
    if (taskId === null) return;
    setReviewing(true);
    setError(null);
    try {
      setTask(await startReview(taskId));
    } catch (err) {
      setError(describeError(err).message);
    } finally {
      setReviewing(false);
    }
  }, [taskId, setTask, setError]);

  return { onReview: () => void onReview(), reviewing };
}

/** 从最近任务打开的一条记录：只读取并展示，不在这里发起处理。 */
function OpenedTask({
  taskId,
  section,
  onSection,
  onTaskChange,
  onBackToList,
}: {
  taskId: string;
  section: TaskSection;
  onSection: (section: TaskSection) => void;
  onTaskChange: () => void;
  onBackToList: () => void;
}) {
  const [task, setTask] = useState<Task | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [deleting, setDeleting] = useState(false);
  const understanding = useUnderstanding(task, setTask, setError);
  const review = useReview(task, setTask, setError);

  const reload = useCallback(async () => {
    try {
      setTask(await fetchTask(taskId));
      setError(null);
    } catch (err) {
      setError(describeError(err).message);
    }
  }, [taskId]);

  useEffect(() => {
    setTask(null);
    setError(null);
    void reload();
  }, [reload]);

  const retry = async () => {
    try {
      setTask(await retryTask(taskId));
      setError(null);
      onTaskChange();
    } catch (err) {
      setError(describeError(err).message);
    }
  };

  const remove = async () => {
    const confirmed = window.confirm(
      "删除后对象存储中的原始视频、本地分片与任务记录都会被清除，且无法恢复。确认删除？"
    );
    if (!confirmed) return;
    setDeleting(true);
    try {
      await deleteTask(taskId);
      onTaskChange();
      onBackToList();
    } catch (err) {
      setError(describeError(err).message);
    } finally {
      setDeleting(false);
    }
  };

  if (error !== null && task === null) {
    return (
      <div className="page">
        <div className="panel">
          <div className="panel-head">
            <h2 className="panel-title">无法打开这条任务记录</h2>
          </div>
          <p className="detail-error">{error}</p>
          <div className="actions mt-lg">
            <button type="button" className="btn btn--primary" onClick={() => void reload()}>
              重新读取
            </button>
            <button type="button" className="btn" onClick={onBackToList}>
              返回任务列表
            </button>
          </div>
        </div>
      </div>
    );
  }

  if (task === null) return <p className="hint">正在读取任务记录…</p>;

  const hasReport = Boolean(task.review?.result);

  return (
    <TaskDetailPage
      task={task}
      section={section}
      onSection={onSection}
      taskLoaded
      actions={{
        onRetry: () => void retry(),
        onDelete: () => void remove(),
        onReset: onBackToList,
        deleting,
        hasReport,
        onDownloadReport: () => window.open(reviewDownloadUrl(task.id), "_blank"),
      }}
      understanding={understanding}
      review={review}
      refreshKey={understanding.refreshKey}
      onTask={setTask}
    />
  );
}

export default function Workbench({
  taskId,
  section,
  onSection,
  onTaskChange,
  onBackToList,
}: {
  /** 非空时展示这条历史任务，否则展示本次上传流程。 */
  taskId: string | null;
  /** 当前子页：上传完成后自动落到「视频信息」，历史任务由 hash 决定。 */
  section: TaskSection;
  /** 切换子页：交给外层的 hash 路由，刷新与前进/后退在子页之间也成立。 */
  onSection: (section: TaskSection) => void;
  /** 任务创建、重试或删除后通知外层刷新最近任务。 */
  onTaskChange?: () => void;
  /** 回到新建任务页（详情页的「返回任务列表」） */
  onBackToList: () => void;
}) {
  const [state, setState] = useState<UploadState>(INITIAL);
  const [task, setTask] = useState<Task | null>(null);
  // 轮询目标：{ taskId, nonce } 作为 effect 依赖。
  // 用 nonce 而不是 taskId 本身，使「重试同一个任务」也能重新启动轮询循环。
  const [watching, setWatching] = useState<{ taskId: string; nonce: number } | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [dragOver, setDragOver] = useState(false);
  /**
   * 本次上传流程的子页。
   *
   * 地址还停在 `#/new` 时（上传刚提交、还没进任务列表）不能让外层路由接管子页：
   * 一旦认领成 `#/tasks/:id`，本组件的上传状态机就会随重挂载一起丢掉，界面会弹回上传入口。
   * 因此这一段用本地状态顶着，等用户从侧栏再打开这条任务时才走 hash 路由。
   */
  const [localSection, setLocalSection] = useState<TaskSection>("media");
  const detailSection = taskId === null ? localSection : section;
  const fileRef = useRef<HTMLInputElement>(null);
  // 识别操作与结果刷新：切分完成后即可在本页直接启动识别，不必走历史记录
  const understanding = useUnderstanding(task, setTask, (message) =>
    setState((prev) => ({ ...prev, error: message }))
  );
  // 复盘操作：与识别共用同一条错误通道
  const review = useReview(task, setTask, (message) =>
    setState((prev) => ({ ...prev, error: message }))
  );

  // 轮询任务状态，直到进入终态；组件卸载或换文件时停止。
  useEffect(() => {
    if (watching === null) return;
    const taskId = watching.taskId;
    let cancelled = false;
    let timer: number | undefined;

    const tick = async () => {
      try {
        const latest = await fetchTask(taskId);
        if (cancelled) return;
        setTask(latest);
        if (latest.status === "succeeded" || latest.status === "failed") {
          setState((prev) => ({
            ...prev,
            phase: latest.status === "failed" ? "error" : "done",
            error: latest.error,
          }));
          onTaskChange?.();
          return;
        }
      } catch (err) {
        if (cancelled) return;
        // 轮询失败不终止流程：任务仍在后台推进，下个周期继续尝试
        setState((prev) => ({ ...prev, error: describeError(err).message }));
      }
      // 清理后不再排下一次，避免卸载或换任务后仍有悬挂的定时器
      if (!cancelled) timer = window.setTimeout(tick, pollIntervalMs());
    };

    void tick();
    return () => {
      cancelled = true;
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [watching, onTaskChange]);

  const resumeAttempted = useRef(false);

  /** 进入「看处理进度」阶段：上传这一半已经结束，剩下的是后端的事。 */
  const watchProcessing = useCallback(
    (taskId: string) => {
      clearPendingUpload();
      setState((prev) => ({ ...prev, phase: "watching", error: null, missingChunks: [] }));
      setWatching(nextWatch(taskId));
      // 地址仍停在新建页（上传流程不认领地址），子页先在本地顶着：
      // 这一页本来就是「看着进度走完」，认领地址会连带把上传状态机一起丢掉
      setLocalSection("media");
      onTaskChange?.();
    },
    [onTaskChange]
  );

  const fail = useCallback((err: unknown) => {
    const { message, missing } = describeError(err);
    // 失败即作废本地凭据：否则下一次挂载还会去捡这次失败的上传
    clearPendingUpload();
    setState((prev) => ({ ...prev, phase: "error", error: message, missingChunks: missing }));
  }, []);

  /** 分片收齐 → 合并入库 → 交给后端处理。 */
  const finishSession = useCallback(
    async (uploadId: string, taskId: string) => {
      setState((prev) => ({ ...prev, phase: "assembling" }));
      try {
        await completeUpload(uploadId);
      } catch (err) {
        fail(err);
        return;
      }
      watchProcessing(taskId);
    },
    [fail, watchProcessing]
  );

  /** 补传缺失分片：已收分片计入起始进度，续传时进度不会从 0 开始。 */
  const uploadChunks = useCallback(
    async (uploadId: string, chunkSize: number, file: File, received: number[]) => {
      const alreadySent = new Set(received);
      const pending = sliceFile(file, chunkSize)
        .map((body, index) => ({ body, index }))
        .filter((item) => !alreadySent.has(item.index));

      let sent = Math.min(received.length * chunkSize, file.size);
      setState((prev) => ({ ...prev, phase: "uploading", sentBytes: sent }));

      let cursor = 0;
      const worker = async () => {
        while (cursor < pending.length) {
          const item = pending[cursor++];
          await uploadChunk(uploadId, item.index, item.body);
          sent = Math.min(sent + item.body.size, file.size);
          setState((prev) => ({ ...prev, sentBytes: sent }));
        }
      };
      await Promise.all(Array.from({ length: Math.min(CONCURRENCY, pending.length) }, worker));
    },
    []
  );

  const start = useCallback(
    async (file: File) => {
      setTask(null);
      setState({ ...IDLE, phase: "uploading", totalBytes: file.size, fileName: file.name });

      try {
        const created = await createUpload(file.name, file.size);
        setState((prev) => ({ ...prev, taskId: created.task_id, totalBytes: created.size }));
        savePendingUpload({
          upload_id: created.upload_id,
          task_id: created.task_id,
          filename: created.filename,
          size: created.size,
          phase: "uploading",
        });

        await uploadChunks(created.upload_id, created.chunk_size, file, created.received_chunks);
        setState((prev) => ({ ...prev, sentBytes: file.size }));
        await finishSession(created.upload_id, created.task_id);
      } catch (err) {
        fail(err);
      }
    },
    [fail, finishSession, uploadChunks]
  );

  /**
   * 重开页面后接着上次传：凭据在浏览器里，服务端跑到哪一步要问过才知道。
   *
   * 三种可能都在这里收口——会话不存在（已被删除或过期）、分片还没收齐（等用户点继续）、
   * 已收齐（把完成请求补上）、已入库（直接进入看进度）。
   */
  const restoreSession = useCallback(async (): Promise<boolean> => {
    const record = loadPendingUpload();
    if (record === null) return false;

    setState({
      ...INITIAL,
      taskId: record.task_id,
      fileName: record.filename,
      totalBytes: record.size,
    });

    try {
      const status = await fetchUpload(record.upload_id);
      if (status.status === UPLOAD_COMPLETED) {
        watchProcessing(status.task_id || record.task_id);
        return true;
      }

      // 分片都在服务端了却还没入库（关页面断在合并前）：把完成请求补上即可，
      // 这一步不需要原文件——分片已经全在服务端，合并与入库都在后端完成
      if (status.received_chunks.length >= status.total_chunks) {
        await finishSession(status.upload_id, status.task_id || record.task_id);
        return true;
      }

      // 还没收齐：缺的那些分片只有用户磁盘上有，续传要用户重新选一次同一个文件
      setState((prev) => ({
        ...prev,
        phase: "unfinished",
        error: status.error,
        sentBytes: Math.min(status.received_bytes, status.size),
      }));
      return true;
    } catch (err) {
      if (err instanceof ApiError && err.status === 404) {
        // 会话已不存在（被删除或服务端清过数据）：凭据作废，回到上传入口
        clearPendingUpload();
        setState(IDLE);
        return false;
      }
      fail(err);
      return true;
    }
  }, [fail, finishSession, watchProcessing]);

  /** 用户重新选文件：服务端留着已收分片，补传缺的那些即可。 */
  const continueUpload = async (file: File) => {
    const record = loadPendingUpload();
    if (record === null) return;
    try {
      const status = await fetchUpload(record.upload_id);
      await uploadChunks(status.upload_id, status.chunk_size, file, status.received_chunks);
      setState((prev) => ({ ...prev, sentBytes: file.size }));
      await finishSession(status.upload_id, status.task_id || record.task_id);
    } catch (err) {
      fail(err);
    }
  };

  // 挂载时捡回没传完的上传。用 ref 挡住重复执行：开发模式下 effect 会跑两次，
  // 两次都去补传分片会让同一序号被写两遍。
  useEffect(() => {
    if (resumeAttempted.current) return;
    resumeAttempted.current = true;
    void restoreSession().then((restored) => {
      if (!restored) setState((prev) => (prev.phase === "loading" ? IDLE : prev));
    });
  }, [restoreSession]);

  const handleFile = (event: ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0];
    if (file) void start(file);
  };

  /** 未传完的上传：选同一个文件即可补齐缺的分片，已传部分不重传。
   *
   * 没有本地 `File` 对象就无法补传——重开页面后那份 1～2GB 的内容只在用户磁盘上，
   * 浏览器不会为它保留句柄，因此这里必须由用户重新选一次。 */
  const handleContinueFile = (event: ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0];
    if (file) void continueUpload(file);
  };

  const handleRetryTask = async () => {
    if (!state.taskId) return;
    try {
      const reset = await retryTask(state.taskId);
      setTask(reset);
      setState((prev) => ({ ...prev, phase: "watching", error: null }));
      setWatching(nextWatch(state.taskId));
    } catch (err) {
      setState((prev) => ({ ...prev, error: describeError(err).message }));
    }
  };

  const handleReset = () => {
    setWatching(null);
    setState(IDLE);
    setTask(null);
    // 放弃上传即作废本地凭据：否则下次挂载又会把这次上传捡回来
    clearPendingUpload();
    if (fileRef.current) fileRef.current.value = "";
    onTaskChange?.();
    onBackToList();
  };

  /** 删除已上传视频：由后端清除云端对象、本地分片与记录，成功后回到初始态。 */
  const handleDelete = async () => {
    if (!state.taskId) return;
    const confirmed = window.confirm(
      "删除后对象存储中的原始视频、本地分片与任务记录都会被清除，且无法恢复。确认删除？"
    );
    if (!confirmed) return;

    setDeleting(true);
    setState((prev) => ({ ...prev, error: null }));
    try {
      await deleteTask(state.taskId);
      handleReset();
    } catch (err) {
      setState((prev) => ({ ...prev, error: describeError(err).message }));
    } finally {
      setDeleting(false);
    }
  };

  if (taskId !== null) {
    // key 让切换到另一条记录时丢掉上一条的界面状态
    return (
      <OpenedTask
        key={taskId}
        taskId={taskId}
        section={section}
        onSection={onSection}
        onTaskChange={onTaskChange ?? (() => {})}
        onBackToList={onBackToList}
      />
    );
  }

  const percent = state.totalBytes > 0 ? Math.round((state.sentBytes / state.totalBytes) * 100) : 0;
  const busy = state.phase === "uploading" || state.phase === "assembling";

  /** 拖拽落盘：与点击选择走同一条路径，续传态下同样是「补传缺片」。 */
  const onDrop = (event: DragEvent<HTMLElement>) => {
    event.preventDefault();
    setDragOver(false);
    if (busy) return;
    const file = event.dataTransfer.files?.[0];
    if (!file) return;
    if (state.phase === "unfinished") void continueUpload(file);
    else void start(file);
  };

  const onDragOver = (event: DragEvent<HTMLElement>) => {
    event.preventDefault();
    if (!busy) setDragOver(true);
  };

  const onDragLeave = () => setDragOver(false);

  /** 点「选择文件 / 开始处理」：交给同一个文件输入框，不重复实现选择逻辑。 */
  const onPick = () => fileRef.current?.click();

  // 上传阶段之外（已入库、处理中或已完成）展示任务详情；上传阶段展示上传页
  const showDetail = !busy && task !== null;
  const inputId = state.phase === "unfinished" ? "continue-file" : "clip-file";

  return (
    <div className="workbench">
      {showDetail ? (
        <TaskDetailPage
          task={task}
          section={detailSection}
          onSection={setLocalSection}
          sectionLinks={false}
          taskLoaded={!busy}
          notice={task.error ? null : state.error}
          actions={{
            onRetry: handleRetryTask,
            onDelete: handleDelete,
            onReset: handleReset,
            deleting,
            hasReport: Boolean(task.review?.result),
            onDownloadReport: () => window.open(reviewDownloadUrl(task.id), "_blank"),
          }}
          understanding={understanding}
          review={review}
          refreshKey={understanding.refreshKey}
          onTask={setTask}
        />
      ) : (
        <IntakePage
          phase={state.phase === "watching" || state.phase === "done" ? "idle" : state.phase}
          fileName={state.fileName}
          totalBytes={state.totalBytes}
          sentBytes={state.sentBytes}
          percent={percent}
          error={state.error}
          missingChunks={state.missingChunks}
          dragOver={dragOver}
          inputId={inputId}
          inputRef={fileRef}
          onPick={onPick}
          onFile={state.phase === "unfinished" ? handleContinueFile : handleFile}
          onDragOver={onDragOver}
          onDragLeave={onDragLeave}
          onDrop={onDrop}
          onCancel={handleReset}
        />
      )}

      {state.phase === "done" && task === null && (
        <p className="hint">已提交 {formatBytes(state.totalBytes)}，任务已回到后台队列。</p>
      )}
    </div>
  );
}

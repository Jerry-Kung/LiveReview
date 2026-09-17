/**
 * 工作区：承接两类内容。
 *
 * 一类是当前这次上传——选文件、分片上传、轮询任务直到出结果；
 * 另一类是用户从历史记录里打开的一个既有任务，只读呈现它的处理结果。
 * 两者共用同一套结果展示（TaskSummary），区别只在于数据从哪来、能给哪些操作。
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { pollIntervalMs } from "./polling";
import UploadProgress from "./UploadProgress";
import TaskSummary from "./TaskSummary";
import {
  ApiError,
  completeUpload,
  createUpload,
  deleteTask,
  fetchTask,
  retryTask,
  sliceFile,
  uploadChunk,
  type Task,
} from "./api";
import { formatBytes } from "./format";

const CONCURRENCY = 4;

type Phase = "idle" | "uploading" | "assembling" | "watching" | "done" | "error";

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

const INITIAL: UploadState = {
  phase: "idle",
  taskId: null,
  sentBytes: 0,
  totalBytes: 0,
  fileName: null,
  error: null,
  missingChunks: [],
};

function describeError(err: unknown): { message: string; missing: number[] } {
  if (err instanceof ApiError) {
    return { message: err.message, missing: err.missingChunks?.missing_chunks ?? [] };
  }
  return { message: err instanceof Error ? err.message : "上传失败，原因未知", missing: [] };
}

/** 从历史记录打开的任务：只读取并展示，不在这里发起处理。 */
function OpenedTask({
  taskId,
  onTaskChange,
  onClose,
}: {
  taskId: string;
  onTaskChange: () => void;
  onClose: () => void;
}) {
  const [task, setTask] = useState<Task | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [deleting, setDeleting] = useState(false);

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
      onClose();
    } catch (err) {
      setError(describeError(err).message);
    } finally {
      setDeleting(false);
    }
  };

  if (error !== null && task === null) {
    return (
      <section className="intake intake--failed">
        <h2 className="intake-title">无法打开这条任务记录</h2>
        <p className="detail-error">{error}</p>
        <div className="actions">
          <button type="button" className="btn btn--primary" onClick={() => void reload()}>
            重新读取
          </button>
          <button type="button" className="btn" onClick={onClose}>
            返回上传
          </button>
        </div>
      </section>
    );
  }

  if (task === null) return <p className="hint">正在读取任务记录…</p>;

  return (
    <>
      <p className="opened-note">
        正在查看历史任务的处理结果。需要重新处理新的录屏时，请使用侧栏的
        <strong> 新建上传任务</strong>。
      </p>
      <TaskSummary
        task={task}
        actions={{
          onRetry: () => void retry(),
          onDelete: () => void remove(),
          onReset: onClose,
          deleting,
        }}
      />
      {error !== null && <p className="detail-error">{error}</p>}
    </>
  );
}

export default function Workbench({
  taskId,
  onTaskChange,
}: {
  /** 非空时展示这条历史任务，否则展示本次上传流程。 */
  taskId: string | null;
  /** 任务创建、重试或删除后通知外层刷新历史记录。 */
  onTaskChange?: () => void;
}) {
  const [state, setState] = useState<UploadState>(INITIAL);
  const [task, setTask] = useState<Task | null>(null);
  // 轮询目标：{ taskId, nonce } 作为 effect 依赖。
  // 用 nonce 而不是 taskId 本身，使「重试同一个任务」也能重新启动轮询循环。
  const [watching, setWatching] = useState<{ taskId: string; nonce: number } | null>(null);
  const [deleting, setDeleting] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);

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

  const start = useCallback(
    async (file: File) => {
      setTask(null);
      setState({ ...INITIAL, phase: "uploading", totalBytes: file.size, fileName: file.name });

      try {
        const created = await createUpload(file.name, file.size);
        setState((prev) => ({ ...prev, taskId: created.task_id }));

        const parts = sliceFile(file, created.chunk_size);
        const alreadySent = new Set(created.received_chunks);
        const pending = parts
          .map((body, index) => ({ body, index }))
          .filter((item) => !alreadySent.has(item.index));

        // 已收分片计入起始进度，续传时进度不会从 0 开始
        let sent = Math.min(created.received_chunks.length * created.chunk_size, file.size);
        setState((prev) => ({ ...prev, sentBytes: sent }));

        let cursor = 0;
        const worker = async () => {
          while (cursor < pending.length) {
            const item = pending[cursor++];
            await uploadChunk(created.upload_id, item.index, item.body);
            sent = Math.min(sent + item.body.size, file.size);
            setState((prev) => ({ ...prev, sentBytes: sent }));
          }
        };
        await Promise.all(Array.from({ length: Math.min(CONCURRENCY, pending.length) }, worker));

        setState((prev) => ({ ...prev, phase: "assembling", sentBytes: file.size }));
        await completeUpload(created.upload_id);
        setState((prev) => ({ ...prev, phase: "watching", error: null, missingChunks: [] }));
        setWatching(nextWatch(created.task_id));
        onTaskChange?.();
      } catch (err) {
        const { message, missing } = describeError(err);
        setState((prev) => ({ ...prev, phase: "error", error: message, missingChunks: missing }));
      }
    },
    [onTaskChange]
  );

  const handleFile = (event: React.ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0];
    if (file) void start(file);
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
    setState(INITIAL);
    setTask(null);
    if (fileRef.current) fileRef.current.value = "";
    onTaskChange?.();
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
    // key 让切换到另一条历史记录时丢掉上一条的界面状态
    return <OpenedTask key={taskId} taskId={taskId} onTaskChange={onTaskChange ?? (() => {})} onClose={() => handleReset()} />;
  }

  const percent = state.totalBytes > 0 ? Math.round((state.sentBytes / state.totalBytes) * 100) : 0;
  const busy = state.phase === "uploading" || state.phase === "assembling";
  const failed = state.phase === "error";

  return (
    <div className="workbench">
      <input
        ref={fileRef}
        id="clip-file"
        className="visually-hidden"
        type="file"
        accept="video/*,.ts,.mp4"
        onChange={handleFile}
        disabled={busy}
        aria-label="选择录屏文件"
      />

      {state.phase === "idle" && (
        <section className="intake">
          <h2 className="intake-title">上传一场直播录屏</h2>
          <p className="intake-lead">
            支持 TS 与 MP4，典型体积 1～2GB。上传完成后后台会自动完成媒体探测、格式处理与切片；
            关掉本页不影响处理，回来再看历史记录即可。
          </p>
          {/* label 关联到文件输入：点击整块区域即可选文件，键盘也能聚焦 */}
          <label className="file-picker" htmlFor="clip-file">
            <span className="file-picker-mark" aria-hidden="true" />
            <span className="file-picker-text">
              <strong>选择录屏文件</strong>
              <span>TS / MP4，单文件，建议不超过 2GB</span>
            </span>
          </label>
        </section>
      )}

      {busy && (
        <UploadProgress
          percent={percent}
          sentBytes={state.sentBytes}
          totalBytes={state.totalBytes}
          assembling={state.phase === "assembling"}
        />
      )}

      {!busy && task && (
        <TaskSummary
          task={task}
          notice={task.error ? null : state.error}
          actions={{
            onRetry: handleRetryTask,
            onDelete: handleDelete,
            onReset: handleReset,
            deleting,
          }}
        />
      )}

      {failed && (
        <section className="intake intake--failed">
          <h2 className="intake-title">上传未完成</h2>
          {state.fileName && <p className="hint">文件：{state.fileName}</p>}
          <p className="detail-error">{state.error}</p>
          {state.missingChunks.length > 0 && (
            <p className="hint">
              缺少 {state.missingChunks.length} 个分片。重新选择同一文件可继续补齐，已上传的分片不会重传。
            </p>
          )}
          <div className="actions">
            <button type="button" className="btn btn--primary" onClick={handleReset}>
              重新发起上传
            </button>
          </div>
        </section>
      )}

      {state.phase === "done" && task === null && (
        <p className="hint">已提交 {formatBytes(state.totalBytes)}，任务已回到后台队列。</p>
      )}
    </div>
  );
}

/**
 * 上传面板：选文件 → 分片并发上传 → 合并 → 轮询任务状态。
 *
 * 上传进度与任务进度分开呈现：前者是字节搬运，后者是后端处理，
 * 避免把「上传完成」误读为「处理完成」。
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { pollIntervalMs } from "./polling";
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

const CONCURRENCY = 4;

export const TASK_LABELS: Record<string, string> = {
  uploading: "上传中",
  uploaded: "待处理",
  processing: "处理中",
  succeeded: "已完成",
  failed: "失败",
};

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
  error: string | null;
  missingChunks: number[];
};

const INITIAL: UploadState = {
  phase: "idle",
  taskId: null,
  sentBytes: 0,
  totalBytes: 0,
  error: null,
  missingChunks: [],
};

export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
  return `${(bytes / 1024 / 1024 / 1024).toFixed(2)} GB`;
}

export function describeTask(task: Task): string {
  const label = TASK_LABELS[task.status] ?? task.status;
  return task.progress > 0 && task.status === "processing" ? `${label} ${task.progress}%` : label;
}

/** 任务状态对应的语义标记：只有成功是健康态，其余都需留意。 */
function taskState(task: Task): "ok" | "attention" {
  return task.status === "succeeded" ? "ok" : "attention";
}

function describeError(err: unknown): { message: string; missing: number[] } {
  if (err instanceof ApiError) {
    return { message: err.message, missing: err.missingChunks?.missing_chunks ?? [] };
  }
  return { message: err instanceof Error ? err.message : "上传失败，原因未知", missing: [] };
}

export default function UploadPanel() {
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
  }, [watching]);

  const start = useCallback(async (file: File) => {
    setTask(null);
    setState({ ...INITIAL, phase: "uploading", totalBytes: file.size });

    try {
      const created = await createUpload(file.name, file.size);
      setState((prev) => ({ ...prev, taskId: created.task_id }));

      const parts = sliceFile(file, created.chunk_size);
      const alreadySent = new Set(created.received_chunks);
      const pending = parts
        .map((body, index) => ({ body, index }))
        .filter((item) => !alreadySent.has(item.index));

      // 已收分片计入起始进度，续传时进度不会从 0 开始
      let sent = created.received_chunks.length * created.chunk_size;
      sent = Math.min(sent, file.size);
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
    } catch (err) {
      const { message, missing } = describeError(err);
      setState((prev) => ({ ...prev, phase: "error", error: message, missingChunks: missing }));
    }
  }, []);

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

  const percent = state.totalBytes > 0 ? Math.round((state.sentBytes / state.totalBytes) * 100) : 0;
  const busy = state.phase === "uploading" || state.phase === "assembling";
  const failed = state.phase === "error";

  return (
    <section className="panel" aria-label="上传录屏">
      <input
        ref={fileRef}
        className="file-input"
        type="file"
        accept="video/*,.ts,.mp4"
        onChange={handleFile}
        disabled={busy}
        aria-label="选择录屏文件"
      />

      {state.phase === "idle" && <p className="hint">选择一场直播录屏（TS/MP4，1～2GB），上传后可在后台继续处理。</p>}

      {busy && (
        <div className="progress-block">
          <p className="progress-label">
            上传中 {percent}%（{formatBytes(state.sentBytes)} / {formatBytes(state.totalBytes)}）
          </p>
          <div className="bar" role="progressbar" aria-valuenow={percent} aria-valuemin={0} aria-valuemax={100}>
            <span style={{ width: `${percent}%` }} />
          </div>
          {state.phase === "assembling" && <p className="hint">分片已收齐，正在合并并写入对象存储…</p>}
        </div>
      )}

      {!busy && task && (
        <div className="progress-block">
          <p className="status-line" data-state={taskState(task)}>
            {`任务：${describeTask(task)}`}
          </p>
          {task.object_key && <p className="hint">对象键：{task.object_key}</p>}
          {task.error && <p className="detail-error">失败原因：{task.error}</p>}
          {!task.error && state.error && <p className="detail-error">{state.error}</p>}
          <div className="actions">
            {task.status === "failed" && (
              <button type="button" onClick={handleRetryTask}>
                重新执行任务
              </button>
            )}
            <button
              type="button"
              className="ghost danger"
              onClick={handleDelete}
              disabled={deleting}
            >
              {deleting ? "删除中…" : "删除视频"}
            </button>
            <button type="button" className="ghost" onClick={handleReset} disabled={deleting}>
              换一个文件
            </button>
          </div>
        </div>
      )}

      {failed && (
        <div className="progress-block">
          <p className="status-line" data-state="attention">
            上传未完成
          </p>
          <p className="detail-error">{state.error}</p>
          {state.missingChunks.length > 0 && (
            <p className="hint">
              缺少 {state.missingChunks.length} 个分片，可重新选择同一文件继续上传补齐全部分片。
            </p>
          )}
          <div className="actions">
            <button type="button" onClick={handleReset}>
              重新发起上传
            </button>
          </div>
        </div>
      )}
    </section>
  );
}

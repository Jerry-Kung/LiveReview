/**
 * 任务工作区：单个任务的处理进度、探测结论、切分结果与操作入口。
 *
 * 内容按「现在到哪一步 → 拿到什么结论 → 能做什么」排列，每块的标题用序号标记，
 * 因为处理链路确实是有先后次序的，序号在这里是信息而不是装饰。
 */

import type { Task, TaskMetadata } from "./api";
import ClipTable from "./ClipTable";
import UnderstandingBlock, { type UnderstandingActions } from "./Understanding";
import ReviewBlock, { type ReviewActions } from "./Review";
import {
  PLACEHOLDER,
  describeTask,
  formatBytes,
  formatChannels,
  formatDuration,
  formatResolution,
  taskState,
} from "./format";

/** 处理链路的阶段划分，与后端进度取值一一对应（0 → 20 → 25 → 40~98 → 100）。 */
const STAGES: Array<{ key: string; label: string; atLeast: number }> = [
  { key: "prepare", label: "准备本地副本", atLeast: 0 },
  { key: "probe", label: "探测媒体信息", atLeast: 20 },
  { key: "convert", label: "转封装", atLeast: 25 },
  { key: "split", label: "切分并上传片段", atLeast: 40 },
  { key: "done", label: "覆盖校验完成", atLeast: 100 },
];

function stageState(task: Task, atLeast: number, index: number): "done" | "active" | "todo" {
  if (task.status === "succeeded") return "done";
  if (task.status === "failed") return task.progress >= atLeast ? "done" : "todo";
  const nextAtLeast = STAGES[index + 1]?.atLeast ?? Number.POSITIVE_INFINITY;
  if (task.progress >= nextAtLeast) return "done";
  if (task.progress >= atLeast) return "active";
  return "todo";
}

/** 处理进度：五段刻度轴，与后端的处理链路一一对应。 */
function ProcessingLine({ task }: { task: Task }) {
  const label = task.status === "processing" ? "正在处理" : "处理记录";
  const state = taskState(task);

  return (
    <div className="stage-bar" role="group" aria-label={`${label}：${describeTask(task)}`}>
      <p className="stage-bar-head">
        <span className="stage-bar-label">{label}</span>
        <span className="stage-bar-value" data-state={state}>
          任务：{describeTask(task)}
        </span>
      </p>
      <ol className="stages">
        {STAGES.map((stage, index) => (
          <li key={stage.key} className="tick" data-state={stageState(task, stage.atLeast, index)}>
            <span className="tick-label">{stage.label}</span>
          </li>
        ))}
      </ol>
    </div>
  );
}

/** 探测结论：让用户能核对「拿到的到底是什么媒体」，字段缺失显示占位。 */
function MetadataBlock({ metadata }: { metadata: TaskMetadata }) {
  const technical: Array<[string, string]> = [
    ["分辨率", formatResolution(metadata.width, metadata.height)],
    ["帧率", metadata.frame_rate ?? PLACEHOLDER],
    ["视频编码", metadata.video_codec ?? PLACEHOLDER],
    ["音频编码", metadata.audio_codec ?? PLACEHOLDER],
    ["采样率", metadata.sample_rate !== null ? `${(metadata.sample_rate / 1000).toFixed(1)} kHz` : PLACEHOLDER],
    ["声道", formatChannels(metadata.channels)],
    ["容器", metadata.format_name ?? PLACEHOLDER],
    ["流数", metadata.stream_count !== null ? `${metadata.stream_count}` : PLACEHOLDER],
  ];

  return (
    <dl className="metadata" aria-label="媒体信息">
      <div className="metadata-item metadata-item--duration">
        <dt>时长</dt>
        <dd>{formatDuration(metadata.duration_seconds)}</dd>
      </div>
      {/* 技术参数逐项可核对，与时长分开成组，避免主次混在一排 */}
      <div className="metadata-params">
        {technical.map(([label, value]) => (
          <div className="metadata-item" key={label}>
            <dt>{label}</dt>
            <dd>{value}</dd>
          </div>
        ))}
      </div>
    </dl>
  );
}

export type TaskActions = {
  onRetry: () => void;
  onDelete: () => void;
  onReset: () => void;
  deleting: boolean;
};

export default function TaskSummary({
  task,
  actions,
  understanding,
  review,
  refreshKey = 0,
  notice = null,
  onTask,
}: {
  task: Task;
  actions: TaskActions;
  /** 识别相关操作：切分未完成的任务也能看到识别块，只是按钮不可用。 */
  understanding: UnderstandingActions;
  /** 复盘相关操作：识别未完成时按钮不可用，块本身仍然显示。 */
  review: ReviewActions;
  /** 识别状态变化时递增，用于让全文面板重新拉取。 */
  refreshKey?: number;
  /** 操作层面的失败（如删除未成功）：与任务自身的失败原因分开呈现。 */
  notice?: string | null;
  /** 复盘轮询拿到新状态时回写：复盘结论由任务详情承载。 */
  onTask: (task: Task) => void;
}) {
  const coverage = task.coverage;
  const clips = task.clips ?? [];
  const issues = coverage?.issues ?? [];

  return (
    <div className="task">
      <header className="task-head">
        <h2 className="task-name">{task.filename ?? "未命名录屏"}</h2>
        <p className="task-meta">
          {task.size !== null ? `原始文件 ${formatBytes(task.size)}` : "体积未知"}
          {task.metadata?.duration_seconds != null &&
            ` · 时长 ${formatDuration(task.metadata.duration_seconds)}`}
        </p>
      </header>

      <ProcessingLine task={task} />

      {task.metadata && (
        <section className="block">
          <h3 className="block-title">
            <span className="block-no">1</span>
            媒体信息
          </h3>
          <MetadataBlock metadata={task.metadata} />
        </section>
      )}

      {coverage && (
        <section className="block">
          <h3 className="block-title">
            <span className="block-no">2</span>
            切分结果
          </h3>
          {/* 覆盖校验通过时是一条普通结论，有问题才升级为警示块 */}
          <dl className="metadata metadata--split" aria-label="切分结果">
            <div className="metadata-item">
              <dt>片段数</dt>
              <dd>{coverage.clip_count}</dd>
            </div>
            <div className="metadata-item">
              <dt>总时长</dt>
              <dd>{formatDuration(coverage.source_duration_seconds ?? null)}</dd>
            </div>
            <div className="metadata-item">
              <dt>覆盖校验</dt>
              <dd>{issues.length === 0 ? "通过" : `${issues.length} 处问题`}</dd>
            </div>
          </dl>

          {issues.length > 0 && (
            <ul className="coverage-issues">
              {issues.map((issue) => (
                <li key={`${issue.code}-${issue.message}`}>{issue.message}</li>
              ))}
            </ul>
          )}

          {clips.length > 0 && <ClipTable clips={clips} coverage={coverage} />}
        </section>
      )}

      {/* 识别块在切分未完成时也出现：让用户看到「下一步要做什么」以及为什么还不能做 */}
      <UnderstandingBlock task={task} actions={understanding} refreshKey={refreshKey} />

      {/* 复盘块同理：识别没做完时按钮不可用，但用户能看到链路的下一步是什么 */}
      <ReviewBlock task={task} actions={review} onTask={onTask} />

      {task.error && <p className="detail-error">失败原因：{task.error}</p>}
      {notice && <p className="detail-error">{notice}</p>}

      <div className="actions">
        {task.status === "failed" && (
          <button type="button" className="btn btn--primary" onClick={actions.onRetry}>
            重新执行任务
          </button>
        )}
        <button type="button" className="btn" onClick={actions.onReset} disabled={actions.deleting}>
          开始新任务
        </button>
        <button
          type="button"
          className="btn btn--danger"
          onClick={actions.onDelete}
          disabled={actions.deleting}
        >
          {actions.deleting ? "删除中…" : "删除视频"}
        </button>
      </div>
    </div>
  );
}

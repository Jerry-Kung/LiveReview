/**
 * 任务详情：一条录屏从上传到结论的全部可见信息。
 *
 * 版式按用户的核对顺序排列，从身份到结论：
 *
 * 1. **任务身份**——哪个文件、多大、什么编码，加上返回与操作入口，一眼能确认「我看的是哪一场」。
 * 2. **处理流程**——五个阶段的横向节点，当前步骤在哪一段是这一屏最需要的信息。
 * 3. **媒体信息 + 视频切片**——左栏是探测到的媒体参数（两列数据布局，不用横向分割线），
 *    右栏是切片 Data Table。两者并排是因为它们常被一起核对：切片总时长对不对，看左边就够了。
 * 4. **识别结果 / 复盘结论**——由 `Understanding` 与 `Review` 两块承载。
 *
 * 与处理链路有关的判定逻辑（`STAGES` 与 `stageState`）保持不变，本次只换呈现方式。
 */

import type { Task, TaskMetadata } from "./api";
import ClipTable from "./ClipTable";
import UnderstandingBlock, { type UnderstandingActions } from "./Understanding";
import ReviewBlock, { type ReviewActions } from "./Review";
import {
  IconArrowLeft,
  IconCheck,
  IconCheckCircle,
  IconVideo,
} from "./icons";
import {
  PLACEHOLDER,
  describeTask,
  formatBytes,
  formatChannels,
  formatDuration,
  formatMoment,
  formatResolution,
  taskStatusLabel,
  taskStatusTone,
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

/** 缩略区：后端不提供视频缩略图，用与实际分辨率同比例的占位块表达「这是一条竖屏录屏」。 */
function Thumbnail({ metadata }: { metadata: TaskMetadata | null }) {
  // 无探测结果时按 9:16 兜底——本产品的素材是竖屏直播录屏
  const ratio =
    metadata?.width && metadata?.height && metadata.height > 0
      ? metadata.width / metadata.height
      : 9 / 16;

  return (
    <div className="thumb" aria-hidden="true">
      <span className="thumb-frame" style={{ aspectRatio: `${ratio}` }}>
        <IconVideo size={20} />
      </span>
    </div>
  );
}

/** 处理流程：横向节点 + 连线，已完成绿色，当前步骤品牌蓝。 */
function ProcessingFlow({ task }: { task: Task }) {
  const label = task.status === "processing" ? "正在处理" : "处理记录";

  return (
    <section className="panel" aria-label={`${label}：${describeTask(task)}`}>
      <div className="panel-head">
        <IconVideo size={18} />
        <h3 className="panel-title">处理流程</h3>
        <span className="panel-head-side">
          <span className="status" data-tone={taskStatusTone(task.status)}>
            任务：{describeTask(task)}
          </span>
        </span>
      </div>

      <ol className="flow">
        {STAGES.map((stage, index) => {
          const stepState = stageState(task, stage.atLeast, index);
          return (
            <li className="flow-node" key={stage.key} data-state={stepState}>
              <span className="flow-dot">
                {stepState === "done" && <IconCheck size={16} />}
                {stepState === "active" && <IconCheckCircle size={16} />}
              </span>
              <span className="flow-label">{stage.label}</span>
              <span className="flow-note">
                {stepState === "done"
                  ? "完成"
                  : stepState === "active"
                    ? `进行中 ${task.progress}%`
                    : "等待"}
              </span>
            </li>
          );
        })}
      </ol>
    </section>
  );
}

/** 媒体信息：两列数据布局，标签灰、取值深。 */
function MetadataBlock({ metadata }: { metadata: TaskMetadata }) {
  // 时长排在最前，其余按「视频 → 音频 → 容器」的核对顺序
  const facts: Array<[string, string]> = [
    ["时长", formatDuration(metadata.duration_seconds)],
    ["音频编码", metadata.audio_codec ?? PLACEHOLDER],
    ["分辨率", formatResolution(metadata.width, metadata.height)],
    ["采样率", metadata.sample_rate !== null ? `${(metadata.sample_rate / 1000).toFixed(1)} kHz` : PLACEHOLDER],
    ["视频编码", metadata.video_codec ?? PLACEHOLDER],
    ["声道", formatChannels(metadata.channels)],
    ["帧率", metadata.frame_rate ?? PLACEHOLDER],
    ["流数", metadata.stream_count !== null ? `${metadata.stream_count}` : PLACEHOLDER],
    ["容器", metadata.format_name ?? PLACEHOLDER],
  ];

  return (
    <dl className="facts-grid" aria-label="媒体信息">
      {facts.map(([label, value]) => (
        <div key={label}>
          <dt className="fact-label">{label}</dt>
          <dd className="fact-value">{value}</dd>
        </div>
      ))}
    </dl>
  );
}

export type TaskActions = {
  onRetry: () => void;
  onDelete: () => void;
  onReset: () => void;
  deleting: boolean;
  onDownloadReport: () => void;
  /** 是否有可下载的复盘报告 */
  hasReport: boolean;
};

export default function TaskDetail({
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
  const metadata = task.metadata;

  return (
    <div className="page">
      <button type="button" className="back-link" onClick={actions.onReset}>
        <IconArrowLeft size={16} />
        返回任务列表
      </button>

      <header className="detail-head">
        <Thumbnail metadata={metadata} />

        <div className="detail-head-main">
          <div className="detail-title-row">
            <h2 className="detail-title">{task.filename ?? "未命名录屏"}</h2>
            <span className="status" data-tone={taskStatusTone(task.status)}>
              {taskStatusLabel(task.status)}
            </span>
          </div>

          <p className="detail-sub">营销直播录屏分析</p>

          <div className="detail-facts">
            <span className="detail-fact">
              <span className="detail-fact-value num">{formatMoment(task.created_at)}</span>
            </span>
            {task.size !== null && (
              <span className="detail-fact num">{formatBytes(task.size)}</span>
            )}
            {metadata && (
              <span className="detail-fact num">
                {formatResolution(metadata.width, metadata.height)}
              </span>
            )}
            {metadata && (metadata.video_codec || metadata.audio_codec) && (
              <span className="detail-fact num">
                {[metadata.video_codec, metadata.audio_codec].filter(Boolean).join(" / ")}
              </span>
            )}
          </div>
        </div>

        <div className="detail-actions">
          <button type="button" className="btn" onClick={actions.onRetry}>
            重新执行任务
          </button>
          <button
            type="button"
            className="btn btn--primary"
            onClick={actions.onDownloadReport}
            disabled={!actions.hasReport}
            title={actions.hasReport ? "下载 Markdown 复盘报告" : "本任务还没有复盘结论"}
          >
            查看报告
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
      </header>

      <ProcessingFlow task={task} />

      <div className="detail-grid">
        {metadata && (
          <section className="panel" aria-label="媒体信息面板">
            <div className="panel-head">
              <IconVideo size={18} />
              <h3 className="panel-title">媒体信息</h3>
            </div>
            <MetadataBlock metadata={metadata} />
          </section>
        )}

        {coverage && (
          <section className="panel" aria-label="切分结果">
            <div className="panel-head">
              <IconVideo size={18} />
              <h3 className="panel-title">视频切片</h3>
              <span className="panel-head-side">
                <span>
                  共 <span className="num">{coverage.clip_count}</span> 个片段
                </span>
                <span>
                  总时长{" "}
                  <span className="num">
                    {formatDuration(coverage.source_duration_seconds ?? null)}
                  </span>
                </span>
              </span>
            </div>

            {/* 切分结论的三项取值：片段数 / 总时长 / 覆盖校验。与媒体信息同一套读法 */}
            <dl className="facts-grid" aria-label="切分结果">
              <div>
                <dt className="fact-label">片段数</dt>
                <dd className="fact-value">{coverage.clip_count}</dd>
              </div>
              <div>
                <dt className="fact-label">总时长</dt>
                <dd className="fact-value">
                  {formatDuration(coverage.source_duration_seconds ?? null)}
                </dd>
              </div>
              <div>
                <dt className="fact-label">覆盖校验</dt>
                <dd className="fact-value">
                  {issues.length === 0 ? "通过" : `${issues.length} 处问题`}
                </dd>
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
      </div>

      {/* 识别块在切分未完成时也出现：让用户看到「下一步要做什么」以及为什么还不能做 */}
      <UnderstandingBlock task={task} actions={understanding} refreshKey={refreshKey} />

      {/* 复盘块同理：识别没做完时按钮不可用，但用户能看到链路的下一步是什么 */}
      <ReviewBlock task={task} actions={review} onTask={onTask} />

      {task.error && <p className="detail-error">失败原因：{task.error}</p>}
      {notice && <p className="detail-error">{notice}</p>}
    </div>
  );
}

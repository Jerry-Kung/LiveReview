/**
 * 子页一：视频信息。
 *
 * 回答「这条录屏被处理成了什么」，页面从上到下就是用户核对的顺序：
 *
 * 1. **处理流程**——五个阶段的横向节点，当前停在哪一段是这一页最先要讲清的事。
 * 2. **媒体信息 + 视频切片**——探测到的参数与切片结果，处理完成后才出现。
 *
 * 上传完成后自动进入这一页，用户在这里看着进度走完，不必自己去找「在哪看进度」。
 * 与处理链路有关的判定逻辑（`STAGES` 与 `stageState`）自 V0.4 起未变，只是换了容器。
 */

import type { Task, TaskMetadata } from "./api";
import ClipTable from "./ClipTable";
import {
  PLACEHOLDER,
  describeTask,
  formatChannels,
  formatDuration,
  formatResolution,
  taskStatusTone,
} from "./format";
import { IconCheck, IconCheckCircle, IconVideo } from "./icons";

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

/** 媒体信息：参数网格，标签灰、取值深。
 *
 * 取值分三行排，每行是同一类信息，九项正好三行排满：
 * 第一行「这条录像是什么」，第二行视频轨，第三行音频轨。顺序按核对的习惯走，
 * 不是按后端字段的顺序；行内换列时仍然读得通，因此不依赖固定的列宽。 */
function MetadataBlock({ metadata }: { metadata: TaskMetadata }) {
  const facts: Array<[string, string]> = [
    // 第一行：整条录像
    ["时长", formatDuration(metadata.duration_seconds)],
    ["分辨率", formatResolution(metadata.width, metadata.height)],
    ["容器", metadata.format_name ?? PLACEHOLDER],
    // 第二行：视频轨
    ["视频编码", metadata.video_codec ?? PLACEHOLDER],
    ["帧率", metadata.frame_rate ?? PLACEHOLDER],
    ["流数", metadata.stream_count !== null ? `${metadata.stream_count}` : PLACEHOLDER],
    // 第三行：音频轨
    ["音频编码", metadata.audio_codec ?? PLACEHOLDER],
    ["采样率", metadata.sample_rate !== null ? `${(metadata.sample_rate / 1000).toFixed(1)} kHz` : PLACEHOLDER],
    ["声道", formatChannels(metadata.channels)],
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

export default function VideoInfoPage({ task }: { task: Task }) {
  const coverage = task.coverage;
  const clips = task.clips ?? [];
  const issues = coverage?.issues ?? [];
  const metadata = task.metadata;

  return (
    <>
      <ProcessingFlow task={task} />

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
          <dl className="facts-grid facts-grid--inline" aria-label="切分结论取值">
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

          {/* 切片表自 V0.4.1 起按组分页：几十片一次铺开会把这一页拉长到无法核对 */}
          {clips.length > 0 && <ClipTable clips={clips} coverage={coverage} />}
        </section>
      )}

      {task.status === "processing" && !coverage && (
        <p className="hint">处理完成后，这里会列出媒体信息与切分出的片段。</p>
      )}
    </>
  );
}

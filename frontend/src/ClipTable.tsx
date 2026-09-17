/**
 * 切片结果：结论 + 片段列表。
 *
 * 本版不做切片回看，片段以文字信息呈现：序号、原视频时间范围、时长、体积、状态。
 * 每行附带一段按原视频时间轴定位的比例条——它同时表达两件事：该片段在整场视频中的位置，
 * 以及各片段之间的相对长度。这是「覆盖整场、交界可检查」这一验收要求的直接视觉对应，
 * 而不是装饰。
 */

import type { TaskClip, TaskCoverage } from "./api";
import {
  CLIP_STATUS_LABELS,
  PLACEHOLDER,
  clipNumber,
  formatBytes,
  formatClipRange,
  formatDuration,
} from "./format";

/** 比例条：把片段起止换算成整场时长上的百分比区间。 */
function axisGeometry(clip: TaskClip, totalSeconds: number): { offset: number; width: number } | null {
  if (!Number.isFinite(totalSeconds) || totalSeconds <= 0) return null;
  const offset = (clip.start_seconds / totalSeconds) * 100;
  const width = ((clip.end_seconds - clip.start_seconds) / totalSeconds) * 100;
  return {
    offset: Math.min(Math.max(offset, 0), 100),
    width: Math.min(Math.max(width, 0.6), 100),
  };
}

export default function ClipTable({
  clips,
  coverage,
}: {
  clips: TaskClip[];
  coverage: TaskCoverage;
}) {
  const total = coverage.source_duration_seconds ?? 0;

  return (
    <table className="clips">
      <caption>
        切片列表（按原视频时间顺序），共 {coverage.clip_count} 片
        {coverage.issues.length > 0 ? `，覆盖校验 ${coverage.issues.length} 处问题` : "，覆盖校验通过"}
      </caption>
      <thead>
        <tr>
          <th scope="col" className="col-index">
            序号
          </th>
          <th scope="col" className="col-time">
            原视频时间范围
          </th>
          <th scope="col" className="col-axis">
            在整场中的位置
          </th>
          <th scope="col" className="col-duration">
            时长
          </th>
          <th scope="col" className="col-size">
            体积
          </th>
          <th scope="col" className="col-status">
            处理状态
          </th>
        </tr>
      </thead>
      <tbody>
        {clips.map((clip) => {
          const geometry = axisGeometry(clip, total);
          return (
            <tr key={clip.index} data-state={clip.status === "failed" ? "attention" : undefined}>
              <td className="cell-num">{clipNumber(clip.index)}</td>
              <td className="cell-time">{formatClipRange(clip.start_seconds, clip.end_seconds)}</td>
              <td className="cell-axis">
                {geometry ? (
                  <span className="axis" aria-hidden="true">
                    <span
                      className="axis-span"
                      style={{ left: `${geometry.offset}%`, width: `${geometry.width}%` }}
                    />
                  </span>
                ) : (
                  PLACEHOLDER
                )}
              </td>
              <td className="cell-num">{formatDuration(clip.duration_seconds)}</td>
              <td className="cell-num">
                {clip.size_bytes === null ? PLACEHOLDER : formatBytes(clip.size_bytes)}
              </td>
              <td>
                {CLIP_STATUS_LABELS[clip.status] ?? clip.status}
                {clip.error && <span className="clip-error">{clip.error}</span>}
              </td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

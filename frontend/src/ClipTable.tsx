/**
 * 切片结果：结论 + 片段列表。
 *
 * 本版不做切片回看，片段以文字信息呈现：序号、原视频时间范围、时长、体积、状态。
 * 每行附带一段按原视频时间轴定位的比例条——它同时表达两件事：该片段在整场视频中的位置，
 * 以及各片段之间的相对长度。这是「覆盖整场、交界可检查」这一验收要求的直接视觉对应，
 * 而不是装饰。
 *
 * V0.4.1 起按组分页：一条长直播的切片动辄几十片，整表铺开会把页面拉长到无法核对。
 * 表还是同一张表，只是每次只渲染当前组，序号与位置条仍按整场坐标计算。
 */

import type { TaskClip, TaskCoverage } from "./api";
import GroupPager, { useGroups } from "./GroupedTable";
import {
  CLIP_STATUS_LABELS,
  PLACEHOLDER,
  clipNumber,
  clipStatusTone,
  formatBytes,
  formatClipRange,
  formatDuration,
} from "./format";
import { IconAlert, IconCheckCircle } from "./icons";

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
  const groups = useGroups(clips.length);
  const visible = clips.slice(groups.start, groups.end);

  return (
    <div className="mt-md">
      <GroupPager
        label="切片分组"
        unit="片"
        total={clips.length}
        page={groups.page}
        pageCount={groups.pageCount}
        start={groups.start}
        end={groups.end}
        onPage={groups.setPage}
      />

      <div className="table-wrap">
        <table className="table clips clips--split">
          <caption>
            切片列表（按原视频时间顺序），共 {coverage.clip_count} 片
            {coverage.issues.length > 0
              ? `，覆盖校验 ${coverage.issues.length} 处问题`
              : "，覆盖校验通过"}
          </caption>
          <thead>
            <tr>
              <th scope="col" className="cell-index">
                序号
              </th>
              <th scope="col">时间范围</th>
              <th scope="col" className="cell-axis">
                在整场中的位置
              </th>
              <th scope="col">时长</th>
              <th scope="col">文件大小</th>
              <th scope="col">状态</th>
            </tr>
          </thead>
          <tbody>
            {visible.map((clip) => {
              const geometry = axisGeometry(clip, total);
              const tone = clipStatusTone(clip.status);
              return (
                <tr key={clip.index} data-state={clip.status === "failed" ? "attention" : undefined}>
                  <td className="cell-num cell-index">{clipNumber(clip.index)}</td>
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
                    <span className="cell-status" data-tone={tone}>
                      {tone === "ok" ? <IconCheckCircle size={16} /> : <IconAlert size={16} />}
                      {CLIP_STATUS_LABELS[clip.status] ?? clip.status}
                    </span>
                    {clip.error && <span className="clip-error">{clip.error}</span>}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}

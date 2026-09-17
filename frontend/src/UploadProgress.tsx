/**
 * 上传进度：字节搬运阶段的进度呈现。
 *
 * 与任务处理进度分开显示——「上传完成」不等于「处理完成」，两者混在一条进度里
 * 会让用户误判。
 */

import { formatBytes } from "./format";

export default function UploadProgress({
  percent,
  sentBytes,
  totalBytes,
  assembling,
}: {
  percent: number;
  sentBytes: number;
  totalBytes: number;
  assembling: boolean;
}) {
  return (
    <div className="upload-progress">
      <div className="upload-progress-head">
        <p className="upload-progress-label">上传中</p>
        <p className="upload-progress-value">
          {percent}% · {formatBytes(sentBytes)} / {formatBytes(totalBytes)}
        </p>
      </div>
      <div
        className="bar"
        role="progressbar"
        aria-valuenow={percent}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-label="上传进度"
      >
        <span style={{ width: `${percent}%` }} />
      </div>
      <p className="hint">
        {assembling ? "分片已收齐，正在合并并写入对象存储…" : "可关闭页面，上传需保持在本页完成。"}
      </p>
    </div>
  );
}

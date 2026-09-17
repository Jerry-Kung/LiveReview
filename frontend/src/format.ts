/**
 * 界面文案与数值格式化。
 *
 * 时间码、体积、分辨率、编码等「可被逐项核对的取值」统一在这里格式化，
 * 便于整屏用同一套写法呈现；缺失值一律显示占位符，不臆造取值。
 */

import type { Task } from "./api";

/** 拿不到就是不显示，不臆造取值。 */
export const PLACEHOLDER = "—";

export const TASK_LABELS: Record<string, string> = {
  uploading: "上传中",
  uploaded: "待处理",
  processing: "处理中",
  succeeded: "已完成",
  failed: "失败",
};

export const CLIP_STATUS_LABELS: Record<string, string> = {
  pending: "待处理",
  uploaded: "已上传",
  failed: "失败",
};

export function taskStatusLabel(status: string): string {
  return TASK_LABELS[status] ?? status;
}

export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
  return `${(bytes / 1024 / 1024 / 1024).toFixed(2)} GB`;
}

/** 时间码：小时只在超过一小时时出现，避免长录屏里满是 00: 前缀。 */
export function formatDuration(seconds: number | null): string {
  if (seconds === null || !Number.isFinite(seconds)) return PLACEHOLDER;
  const total = Math.round(seconds);
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const rest = total % 60;
  const pad = (value: number) => String(value).padStart(2, "0");
  return hours > 0 ? `${hours}:${pad(minutes)}:${pad(rest)}` : `${minutes}:${pad(rest)}`;
}

export function formatResolution(width: number | null, height: number | null): string {
  return width !== null && height !== null ? `${width}×${height}` : PLACEHOLDER;
}

export function formatChannels(channels: number | null): string {
  if (channels === 1) return "单声道";
  if (channels === 2) return "立体声";
  return channels ? `${channels} 声道` : PLACEHOLDER;
}

/** 任务状态加上处理进度；只有处理中的任务才需要百分比。 */
export function describeTask(task: Task): string {
  const label = taskStatusLabel(task.status);
  return task.progress > 0 && task.status === "processing" ? `${label} ${task.progress}%` : label;
}

/** 只有成功是健康态，其余都需留意。 */
export function taskState(task: Task): "ok" | "attention" {
  return task.status === "succeeded" ? "ok" : "attention";
}

/** 片段在原视频中的时间范围：核对时间对应关系靠的就是它。 */
export function formatClipRange(start: number, end: number): string {
  return `${formatDuration(start)} ~ ${formatDuration(end)}`;
}

/** 片段序号：补零使列表读起来有明确的先后次序。 */
export function clipNumber(index: number): string {
  return String(index + 1).padStart(2, "0");
}

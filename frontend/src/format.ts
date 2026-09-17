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

/** 片段识别状态：与切片状态分开，因为一个片段可能已上传但还没识别。 */
export const UNDERSTANDING_STATUS_LABELS: Record<string, string> = {
  pending: "未识别",
  running: "识别中",
  succeeded: "已识别",
  failed: "识别失败",
  // 任务级专有：切片尚未全部就绪，本次没有可识别的输入
  skipped: "未开始",
};

export function understandingStatusLabel(status: string | null): string {
  if (status === null) return "未开始";
  return UNDERSTANDING_STATUS_LABELS[status] ?? status;
}

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

/**
 * 语音记录的精确时间码：带毫秒。
 *
 * 与 `formatDuration` 的分工是刻意的——时长用整秒便于扫读，而识别记录的时间戳是
 * 用来回看定位的坐标，毫秒位在核查「这句话到底在几分几秒」时有用。
 */
export function formatTimestamp(seconds: number): string {
  if (!Number.isFinite(seconds)) return PLACEHOLDER;
  const sign = seconds < 0 ? "-" : "";
  const value = Math.abs(seconds);
  const hours = Math.floor(value / 3600);
  const minutes = Math.floor((value % 3600) / 60);
  const whole = Math.floor(value % 60);
  const millis = Math.round((value - Math.floor(value)) * 1000);
  const pad = (input: number, width: number) => String(input).padStart(width, "0");
  const base = `${pad(hours, 2)}:${pad(minutes, 2)}:${pad(whole, 2)}.${pad(millis, 3)}`;
  return `${sign}${base}`;
}

/** 识别结果的完成时刻：只显示到分钟，用来判断「这份结果是不是刚才跑的」。 */
export function formatMoment(value: string | null): string {
  if (!value) return PLACEHOLDER;
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return PLACEHOLDER;
  const pad = (input: number) => String(input).padStart(2, "0");
  return `${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

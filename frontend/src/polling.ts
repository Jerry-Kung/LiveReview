/**
 * 任务轮询间隔。
 *
 * 默认 2 秒：上传进度由分片请求自身驱动，任务状态只需够快地被看到。
 * 测试通过 `setPollIntervalMs` 缩短间隔，避免用例等待真实秒数。
 */

const DEFAULT_INTERVAL_MS = 2000;

let intervalMs = DEFAULT_INTERVAL_MS;

export function pollIntervalMs(): number {
  return intervalMs;
}

export function setPollIntervalMs(value: number): void {
  intervalMs = value;
}

export function resetPollIntervalMs(): void {
  intervalMs = DEFAULT_INTERVAL_MS;
}

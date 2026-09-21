/**
 * 未完成上传的本地凭据：把「服务器上的哪个上传会话」留在浏览器里。
 *
 * 上传由浏览器逐片驱动，关掉页面后服务端能自己把已收齐的分片做完（见后端 `app/uploads.py`），
 * 但**没收齐的部分只有浏览器知道**——没有这份凭据，重开页面就找不到原来那个会话，只能从头再传一遍
 * 1～2GB 的原文件。因此这里只存会话标识与文件元信息，视频内容始终不落浏览器存储。
 *
 * 存储可能不可用（隐私模式、用户清过站点数据），读写一律容错：存不下最多是多传一遍，
 * 不能让上传功能本身因此失败。
 */

export type PendingUpload = {
  upload_id: string;
  task_id: string;
  filename: string;
  size: number;
  /** 中断时停在哪个阶段，决定重开页面后是续传分片还是只等入库 */
  phase: "uploading" | "assembling";
  saved_at: number;
};

const STORAGE_KEY = "livereview.pending-upload";

/**
 * 凭据有效期：超过这个时间就不再自动续传。
 *
 * 服务端只在自己启动时恢复「分片已收齐」的会话，未收齐的会话没有任何计时器会清理它。
 * 留一个上限，避免几个月后的一次访问突然开始往一个早已被遗忘的会话里补分片。
 */
const TTL_MS = 24 * 60 * 60 * 1000;

export function loadPendingUpload(): PendingUpload | null {
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as PendingUpload;
    if (
      typeof parsed?.upload_id !== "string" ||
      typeof parsed?.filename !== "string" ||
      typeof parsed?.size !== "number" ||
      typeof parsed?.saved_at !== "number"
    ) {
      return null;
    }
    if (Date.now() - parsed.saved_at > TTL_MS) {
      clearPendingUpload();
      return null;
    }
    return parsed;
  } catch {
    // 站点数据被禁用或内容损坏：当作没有未完成的上传
    return null;
  }
}

export function savePendingUpload(record: Omit<PendingUpload, "saved_at">): void {
  try {
    const payload: PendingUpload = { ...record, saved_at: Date.now() };
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(payload));
  } catch {
    // 存不下不影响本次上传：只是关页面后无法自动续传
  }
}

export function clearPendingUpload(): void {
  try {
    window.localStorage.removeItem(STORAGE_KEY);
  } catch {
    // 同上：清不掉最多留下一条会被 TTL 拦住的陈旧记录
  }
}

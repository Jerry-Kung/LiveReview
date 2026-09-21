/**
 * 后端接口封装：上传与任务链路。
 *
 * 与后端 `app/schemas.py` 的契约一一对应；后端返回的中文失败原因为用户可见文案。
 * 登录态在 HttpOnly Cookie 里，不在这里读写——当前身份见 `session.ts`。
 */

import { notifyUnauthorized } from "./session";

export type UploadCreateResponse = {
  upload_id: string;
  task_id: string;
  filename: string;
  size: number;
  chunk_size: number;
  total_chunks: number;
  received_chunks: number[];
  status: string;
};

export type UploadStatusResponse = Omit<UploadCreateResponse, "task_id"> & {
  task_id: string;
  received_bytes: number;
  error: string | null;
};

export type TaskMetadata = {
  format_name: string | null;
  duration_seconds: number | null;
  video_codec: string | null;
  width: number | null;
  height: number | null;
  frame_rate: string | null;
  audio_codec: string | null;
  sample_rate: number | null;
  channels: number | null;
  stream_count: number | null;
  bit_rate: number | null;
  content_hash: string | null;
  probed_at: string;
};

export type TaskClip = {
  index: number;
  start_seconds: number;
  end_seconds: number;
  duration_seconds: number | null;
  size_bytes: number | null;
  status: string;
  error: string | null;
  object_key: string | null;
  /** 预签名下载地址：仅已落对象存储的片段有值 */
  download_url: string | null;
  /** 片段的识别状态：pending / running / succeeded / failed */
  understanding_status: string;
  /** 该片段识别出的语音条数；未识别与失败一律为 0 */
  understanding_segment_count: number;
  /** 该片段识别失败的原因，可据它判断是否值得重试 */
  understanding_error: string | null;
  /** 本次识别实际发出的请求次数（含退避重试） */
  understanding_attempts: number;
  /** 识别完成时间；为空表示还没识别过（与「识别出空内容」区分） */
  understanding_at: string | null;
  /** 解析告警：模型返回但被丢弃的条目，缺口要显式可见 */
  understanding_warnings: string[];
};

export type CoverageIssue = {
  code: string;
  message: string;
};

export type TaskCoverage = {
  clip_count: number;
  checked_at: string;
  issues: CoverageIssue[];
  source_format: string | null;
  source_duration_seconds: number | null;
};

export type TaskUnderstanding = {
  status: string;
  progress: number;
  clip_count: number;
  segment_count: number;
  failed_clip_count: number;
  error: string | null;
  started_at: string | null;
  finished_at: string | null;
  model_name: string | null;
};

/** 一条关键事件：时间为原视频绝对秒数，与转写时间戳同一坐标系。 */
export type ReviewEvent = {
  start_seconds: number | null;
  end_seconds: number | null;
  topic: string;
  summary: string;
};

/** 一条分析发现：判断 + 证据等级 + 依据 + 建议。 */
export type ReviewFinding = {
  dimension: string;
  judgement: string;
  /** 事实 / 高置信推断 / 待验证假设 */
  evidence_level: string;
  evidence: string;
  start_seconds: number | null;
  suggestion: string;
};

export type ReviewIssue = {
  problem: string;
  evidence: string;
  impact: string;
  root_cause: string;
  action: string;
};

export type ReviewAction = {
  goal: string;
  how: string;
  observe: string;
};

/** 复盘结论本体：与后端归一后的结构一一对应。 */
export type ReviewResult = {
  one_line: string;
  /** 完整 / 部分 / 受限：由覆盖面与模型判断共同确定 */
  analysis_level: string;
  level_reason: string;
  batch_count: number;
  clip_count: number;
  succeeded_clip_count: number;
  failed_clip_count: number;
  key_events: ReviewEvent[];
  findings: ReviewFinding[];
  top_issues: ReviewIssue[];
  next_actions: ReviewAction[];
  missing_info: string[];
};

/** 整场复盘结论：未复盘时 finished_at 为空，result 也可能为空。 */
export type TaskReview = {
  status: string;
  progress: number;
  clip_count: number;
  segment_count: number;
  batch_count: number;
  error: string | null;
  started_at: string | null;
  finished_at: string | null;
  model_name: string | null;
  /** 归一过程中的告警：如超出准则条数被截断 */
  warnings: string[];
  result: ReviewResult | null;
};

export type TranscriptSegment = {
  index: number;
  clip_index: number;
  start_seconds: number;
  end_seconds: number;
  duration_seconds: number;
  content: string;
  /** 语气与情绪的主观估计（V0.3 起）：可能为空，缺失时界面不显示 */
  tone: string;
  clip_start_seconds: number;
  clip_end_seconds: number;
  /** 时间越出片段范围：只标记不裁切，人工核查时要单独看 */
  out_of_range: boolean;
};

export type TranscriptClip = {
  index: number;
  start_seconds: number;
  end_seconds: number;
  status: string;
  segment_count: number;
  error: string | null;
  warnings: string[];
};

export type Transcript = {
  task_id: string;
  filename: string | null;
  status: string;
  clip_count: number;
  succeeded_clip_count: number;
  failed_clip_count: number;
  segment_count: number;
  model_name: string | null;
  clips: TranscriptClip[];
  segments: TranscriptSegment[];
  /** 后端拼好的全文：页面与导出共用同一份内容 */
  text: string;
};

export type Task = {
  id: string;
  kind: string;
  status: string;
  progress: number;
  filename: string | null;
  object_key: string | null;
  size: number | null;
  error: string | null;
  /** 媒体探测结果：未探测成功时为 null（与「探测出空值」区分开） */
  metadata: TaskMetadata | null;
  /** 切分与覆盖校验结论：未切分时为 null */
  coverage: TaskCoverage | null;
  /** 整场识别结论：未开始识别时为 null */
  understanding: TaskUnderstanding | null;
  /** 整场复盘结论：未开始复盘时为 null */
  review: TaskReview | null;
  /** 切片列表：按序号升序，即原视频时间轴上的顺序 */
  clips: TaskClip[];
  created_at: string;
  updated_at: string;
};

export type MissingChunks = {
  message: string;
  missing_chunks: number[];
  received_chunks: number[];
  total_chunks: number;
};

/** 带后端失败原因的请求错误：调用方直接把 message 呈现给用户。 */
export class ApiError extends Error {
  status: number;
  missingChunks: MissingChunks | null;

  constructor(message: string, status: number, missingChunks: MissingChunks | null = null) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.missingChunks = missingChunks;
  }
}

function isMissingChunks(value: unknown): value is MissingChunks {
  return (
    typeof value === "object" &&
    value !== null &&
    Array.isArray((value as MissingChunks).missing_chunks)
  );
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    // 登录态在 HttpOnly Cookie 里，必须显式带上同源凭据，否则每个请求都会是未登录
    response = await fetch(path, { credentials: "same-origin", ...init });
  } catch {
    throw new ApiError("无法连接后端服务，请确认服务是否已启动", 0);
  }

  if (response.status === 401) {
    // 会话过期：通知壳层退回登录页，再把失败交给调用方。
    // 两件事都要做——只通知的话，正在轮询的代码会继续以为自己在正常工作。
    notifyUnauthorized();
  }

  if (!response.ok) {
    let message = `请求失败（HTTP ${response.status}）`;
    let missing: MissingChunks | null = null;
    try {
      const body = await response.json();
      const detail = body?.detail;
      if (typeof detail === "string") {
        message = detail;
      } else if (isMissingChunks(detail)) {
        message = detail.message;
        missing = detail;
      }
    } catch {
      // 保留默认文案
    }
    throw new ApiError(message, response.status, missing);
  }

  if (response.status === 204) {
    return undefined as T;
  }
  return (await response.json()) as T;
}

export function createUpload(filename: string, size: number): Promise<UploadCreateResponse> {
  return request<UploadCreateResponse>("/api/uploads", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ filename, size }),
  });
}

export function fetchUpload(uploadId: string): Promise<UploadStatusResponse> {
  return request<UploadStatusResponse>(`/api/uploads/${uploadId}`);
}

export function uploadChunk(uploadId: string, index: number, body: Blob): Promise<void> {
  return request<void>(`/api/uploads/${uploadId}/chunks/${index}`, {
    method: "PUT",
    headers: { "Content-Type": "application/octet-stream" },
    body,
  });
}

export function completeUpload(uploadId: string): Promise<{ object_key: string; size: number }> {
  return request(`/api/uploads/${uploadId}/complete`, { method: "POST" });
}

export function fetchTask(taskId: string): Promise<Task> {
  return request<Task>(`/api/tasks/${taskId}`);
}

export function fetchTasks(limit = 20): Promise<{ items: Task[] }> {
  return request<{ items: Task[] }>(`/api/tasks?limit=${limit}`);
}

export function retryTask(taskId: string): Promise<Task> {
  return request<Task>(`/api/tasks/${taskId}/retry`, { method: "POST" });
}

/** 删除已上传视频：后端同步清除对象存储中的原始视频、本地分片与记录，不可撤销。 */
export function deleteTask(taskId: string): Promise<void> {
  return request<void>(`/api/tasks/${taskId}`, { method: "DELETE" });
}

/**
 * 启动整场识别：后台执行，接口只确认入队。
 *
 * 已识别成功的片段不会重发请求，只为未完成与失败的片段补课，因此重复点击不会重复计费。
 */
export function startUnderstanding(taskId: string): Promise<Task> {
  return request<Task>(`/api/tasks/${taskId}/understanding`, { method: "POST" });
}

/** 单片重试：只重发这一片，同步返回该任务的最新状态。 */
export function retryClipUnderstanding(taskId: string, index: number): Promise<Task> {
  return request<Task>(`/api/tasks/${taskId}/clips/${index}/understanding`, { method: "POST" });
}

/** 整场语音记录汇总：片段状态 + 按时间排序的条目 + 后端拼好的全文。 */
export function fetchTranscript(taskId: string): Promise<Transcript> {
  return request<Transcript>(`/api/tasks/${taskId}/transcript`);
}

/** 全文下载地址：交给浏览器直接取，不经过前端内存。 */
export function transcriptDownloadUrl(taskId: string): string {
  return `/api/tasks/${taskId}/transcript.txt`;
}

/**
 * 启动整场复盘：后台执行，接口只确认入队。
 *
 * 前置条件是「已有识别成功的语音记录」；识别有失败片段不阻断，缺口会写进结论的分析等级。
 */
export function startReview(taskId: string): Promise<Task> {
  return request<Task>(`/api/tasks/${taskId}/review`, { method: "POST" });
}

/** 复盘报告下载地址：Markdown，与页面同源。 */
export function reviewDownloadUrl(taskId: string): string {
  return `/api/tasks/${taskId}/review.md`;
}

/** 按后端下发的分片大小切分文件；末片自动不足一整片。 */
/**
 * 账号管理（V0.5.2）：只有管理员账号调得动，普通账号会拿到 403。
 *
 * 这三个走通用的 `request<T>()` 而不是像 `session.ts` 那样自己 fetch：它们都是登录后的
 * 普通数据请求，401 应当照常触发「会话失效」回到登录页，403 则应把后端的中文 `detail`
 * 显示出来。
 */

export type Account = {
  id: number;
  username: string;
  display_name: string;
  /** `admin` 或 `member`；管理员账号不可删除 */
  role: string;
  created_at: string;
  /** 从未登录过即为 null */
  last_login_at: string | null;
};

export function fetchAccounts(): Promise<{ items: Account[] }> {
  return request<{ items: Account[] }>("/api/accounts");
}

export function createAccount(
  username: string,
  password: string,
  displayName: string,
): Promise<Account> {
  return request<Account>("/api/accounts", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    // 不含角色：服务端一律建成普通账号，界面没有提权入口
    body: JSON.stringify({ username, password, display_name: displayName }),
  });
}

export function deleteAccount(accountId: number): Promise<void> {
  return request<void>(`/api/accounts/${accountId}`, { method: "DELETE" });
}

export function sliceFile(file: File, chunkSize: number): Blob[] {
  const parts: Blob[] = [];
  for (let offset = 0; offset < file.size; offset += chunkSize) {
    parts.push(file.slice(offset, Math.min(offset + chunkSize, file.size)));
  }
  return parts;
}

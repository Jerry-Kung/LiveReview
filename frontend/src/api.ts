/**
 * 后端接口封装：上传与任务链路。
 *
 * 与后端 `app/schemas.py` 的契约一一对应；后端返回的中文失败原因为用户可见文案。
 */

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
    response = await fetch(path, init);
  } catch {
    throw new ApiError("无法连接后端服务，请确认服务是否已启动", 0);
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

/** 按后端下发的分片大小切分文件；末片自动不足一整片。 */
export function sliceFile(file: File, chunkSize: number): Blob[] {
  const parts: Blob[] = [];
  for (let offset = 0; offset < file.size; offset += chunkSize) {
    parts.push(file.slice(offset, Math.min(offset + chunkSize, file.size)));
  }
  return parts;
}

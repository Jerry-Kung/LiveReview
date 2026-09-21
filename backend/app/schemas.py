"""接口的请求与响应模型：对外契约集中在此，避免路由层散落字典拼装。"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class UploadCreateRequest(BaseModel):
    filename: str = Field(min_length=1, max_length=255)
    size: int = Field(gt=0)


class UploadCreateResponse(BaseModel):
    upload_id: str
    task_id: str
    filename: str
    size: int
    chunk_size: int
    total_chunks: int
    received_chunks: list[int]
    status: str


class UploadStatusResponse(BaseModel):
    upload_id: str
    task_id: str
    filename: str
    size: int
    chunk_size: int
    total_chunks: int
    received_chunks: list[int]
    received_bytes: int
    status: str
    error: str | None = None


class UploadCompleteResponse(BaseModel):
    upload_id: str
    task_id: str
    object_key: str
    size: int
    status: str


class MissingChunksDetail(BaseModel):
    """缺片响应体：前端据此补齐缺失分片后重新提交完成请求。"""

    message: str
    missing_chunks: list[int]
    received_chunks: list[int]
    total_chunks: int


class TaskMetadataResponse(BaseModel):
    """媒体探测结果（V0.1.4）：任务未探测成功时整个对象为 null。

    字段取值遵循「拿不到就是 null」：探测结果缺字段时不填 0 或空串，避免消费方把未知
    当成已知。`probed_at` 是判断「是否探测过」的唯一依据。
    """

    format_name: str | None = None
    duration_seconds: float | None = None
    video_codec: str | None = None
    width: int | None = None
    height: int | None = None
    frame_rate: str | None = None
    audio_codec: str | None = None
    sample_rate: int | None = None
    channels: int | None = None
    stream_count: int | None = None
    bit_rate: int | None = None
    content_hash: str | None = None
    probed_at: datetime


class TaskClipResponse(BaseModel):
    """一个切片（V0.1.5）：序号即其在原视频时间轴上的顺序。

    `start_seconds` / `end_seconds` 是片段在切分输入（转封装产物或原始 MP4）中的定位，
    也是 V0.1.6 回看定位的依据。`download_url` 只在片段已落对象存储时给出预签名地址。
    """

    index: int
    start_seconds: float
    end_seconds: float
    duration_seconds: float | None = None
    size_bytes: int | None = None
    status: str
    error: str | None = None
    object_key: str | None = None
    download_url: str | None = None
    # 识别结果（V0.2）：`understanding_status` 与 `understanding_at` 区分「识别出空内容」
    # 与「还没识别过」；`understanding_error` 是单片失败原因，供逐片重试的判断依据。
    understanding_status: str = "pending"
    understanding_segment_count: int = 0
    understanding_error: str | None = None
    understanding_attempts: int = 0
    understanding_at: datetime | None = None
    understanding_warnings: list[str] = Field(default_factory=list)


class TaskUnderstandingResponse(BaseModel):
    """整场识别结论（V0.2）：任务级状态、覆盖情况与耗时。

    `status` 为 `skipped` 时表示本次没有可识别的输入（切片尚未全部就绪）；`finished_at`
    非空才说明跑过一轮，与片段级字段的取值方式一致。
    """

    status: str
    progress: int
    clip_count: int
    segment_count: int
    failed_clip_count: int
    error: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    model_name: str | None = None


class TranscriptSegmentResponse(BaseModel):
    """一条语音记录：时间为**原视频时间轴上的绝对秒数**。

    `clip_start_seconds` / `clip_end_seconds` 是片段内的相对时间，保留下来用于对照原始
    模型输出定位偏差。`out_of_range` 为真表示模型给出的时间超出片段本身，本系统只标记、
    不裁切——这类记录需要在人工核查时单独看。`tone` 是模型对语气与情绪的主观估计（V0.3
    起），可能为空。
    """

    index: int
    clip_index: int
    start_seconds: float
    end_seconds: float
    duration_seconds: float
    content: str
    tone: str = ""
    clip_start_seconds: float
    clip_end_seconds: float
    out_of_range: bool = False


class TranscriptClipResponse(BaseModel):
    """一个片段在识别链路里的状态与条目数；失败片段也出现在这里。"""

    index: int
    start_seconds: float
    end_seconds: float
    status: str
    segment_count: int
    error: str | None = None
    warnings: list[str] = Field(default_factory=list)


class TranscriptResponse(BaseModel):
    """整场语音记录：片段级状态 + 按时间顺序拼好的全文。

    `text` 由后端拼装而不是前端逐片拼接：全文是「识别结果汇总」这一对外产物，只在一处
    生成才能保证导出、复制与页面看到的是同一份内容。
    """

    task_id: str
    filename: str | None = None
    status: str
    clip_count: int
    succeeded_clip_count: int
    failed_clip_count: int
    segment_count: int
    model_name: str | None = None
    clips: list[TranscriptClipResponse] = Field(default_factory=list)
    segments: list[TranscriptSegmentResponse] = Field(default_factory=list)
    text: str


class CoverageIssueResponse(BaseModel):
    """一条整场覆盖校验问题：`code` 供程序判断，`message` 供展示。"""

    code: str
    message: str


class ReviewEventResponse(BaseModel):
    """一条关键事件：时间为原视频绝对秒数，与转写时间戳同一坐标系。"""

    start_seconds: float | None = None
    end_seconds: float | None = None
    topic: str = ""
    summary: str = ""


class ReviewFindingResponse(BaseModel):
    """一条分析发现：判断 + 证据等级 + 依据 + 建议。

    `evidence_level` 取值为【事实】/【高置信推断】/【待验证假设】三档，取值口径见复盘准则；
    界面据此提示用户这条结论能信到什么程度。
    """

    dimension: str = ""
    judgement: str = ""
    evidence_level: str = ""
    evidence: str = ""
    start_seconds: float | None = None
    suggestion: str = ""


class ReviewIssueResponse(BaseModel):
    """一条 TOP 问题：问题、证据、影响、归因与下一场动作。"""

    problem: str = ""
    evidence: str = ""
    impact: str = ""
    root_cause: str = ""
    action: str = ""


class ReviewActionResponse(BaseModel):
    """一条下一场实验动作：目标、做法与观察项。"""

    goal: str = ""
    how: str = ""
    observe: str = ""


class ReviewResultResponse(BaseModel):
    """复盘结论本体：与 `app/llm/parsing.py` 的归一结构一一对应。"""

    one_line: str = ""
    analysis_level: str = ""
    level_reason: str = ""
    batch_count: int = 1
    clip_count: int = 0
    succeeded_clip_count: int = 0
    failed_clip_count: int = 0
    key_events: list[ReviewEventResponse] = Field(default_factory=list)
    findings: list[ReviewFindingResponse] = Field(default_factory=list)
    top_issues: list[ReviewIssueResponse] = Field(default_factory=list)
    next_actions: list[ReviewActionResponse] = Field(default_factory=list)
    missing_info: list[str] = Field(default_factory=list)


class TaskReviewResponse(BaseModel):
    """复盘结论（V0.3）：状态、覆盖面与结构化结论。

    `result` 只在真正跑出结论时非空；`finished_at` 为空表示这一轮还没跑过，避免把「未开始」
    渲染成「已复盘」。`error` 是失败原因，`warnings` 是归一过程中的告警（如超出的 TOP 条目
    被截断）。
    """

    status: str
    progress: int = 0
    clip_count: int = 0
    segment_count: int = 0
    batch_count: int = 0
    error: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    model_name: str | None = None
    warnings: list[str] = Field(default_factory=list)
    result: ReviewResultResponse | None = None


class TaskCoverageResponse(BaseModel):
    """切分与覆盖校验结论：`checked_at` 非空表示已校验过。

    未校验时整个对象为 null，避免把「尚未切分」渲染成「校验通过」。
    """

    clip_count: int
    checked_at: datetime
    issues: list[CoverageIssueResponse] = Field(default_factory=list)
    source_format: str | None = None
    source_duration_seconds: float | None = None


class TaskResponse(BaseModel):
    id: str
    kind: str
    status: str
    progress: int
    filename: str | None = None
    object_key: str | None = None
    size: int | None = None
    error: str | None = None
    metadata: TaskMetadataResponse | None = None
    coverage: TaskCoverageResponse | None = None
    understanding: TaskUnderstandingResponse | None = None
    review: TaskReviewResponse | None = None
    clips: list[TaskClipResponse] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class TaskListResponse(BaseModel):
    items: list[TaskResponse]


# ---- 用户鉴权（V0.5）----


class LoginRequest(BaseModel):
    """登录请求。口令长度只做上限约束：下限由配置侧的口令策略决定，不在这里承诺。"""

    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class UserResponse(BaseModel):
    """当前登录者。`display_name` 只用于界面称呼，不参与鉴权。

    `role` 是前端判断「要不要显示账号管理入口」的唯一依据（V0.5.2）。它只决定界面的
    可见性，真正的判定在后端；把角色藏起来只会让前端无从渲染，而不是更安全。
    """

    username: str
    display_name: str
    role: str


class SessionResponse(BaseModel):
    """登录态：登录与「查询当前身份」共用同一份结构。"""

    user: UserResponse


class AccountCreateRequest(BaseModel):
    """新建账号的请求体。

    刻意**不含 `role`**：账号的角色由服务端写死为 `member`（见 `app/routers/accounts.py`）。
    请求体里留一个角色字段等于把提权路径开在接口上——界面只提供建普通账号，接口就不
    该接受更大的权力。要造第二个管理员仍走 `init_admin` 脚本。

    长度下限不在这里约束：pydantic 的 `min_length` 会返回 422 加一段英文提示，而本项目
    的中文 `detail` 是直接展示给使用者的文案。下限改在路由里判定，口径与脚本一致。
    """

    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)
    display_name: str = Field(default="", max_length=64)


class AccountResponse(BaseModel):
    """账号清单里的一行。不含 `password_hash`——哈希不出现在任何响应体里。"""

    id: int
    username: str
    display_name: str
    role: str
    created_at: datetime
    # 从未登录过即为空：界面据此区分「建了没用」与「正在使用」
    last_login_at: datetime | None


class AccountListResponse(BaseModel):
    items: list[AccountResponse]


class StatusResponse(BaseModel):
    """登录后可见的运行配置状态。

    缺失项只给名称，不给取值；`auth_warnings` 是启动时鉴权状态的问题清单（如数据库里
    一个账号都没有），让人在界面上就能看见「服务起来了但没人能登录」这类隐患。
    """

    version: str
    environment: str
    database: str
    storage: str
    storage_missing: list[str] = Field(default_factory=list)
    llm: str
    llm_missing: list[str] = Field(default_factory=list)
    auth_users: int
    auth_warnings: list[str] = Field(default_factory=list)

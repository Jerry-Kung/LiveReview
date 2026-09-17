"""后台任务包：执行器与状态流转。

包含两条任务体：切分链（`runner.py`）与识别链（`understanding.py`）。两者共用执行器，
由 `submit(task_id, phase)` 的 phase 参数区分。
"""

from app.tasks.executor import requeue_pending, run_sync, shutdown, submit
from app.tasks.runner import (
    PROGRESS_CONVERTED,
    PROGRESS_DONE,
    PROGRESS_PROBED,
    PROGRESS_SPLIT_END,
    PROGRESS_SPLIT_START,
    STATUS_FAILED,
    STATUS_PROCESSING,
    STATUS_SUCCEEDED,
    STATUS_UPLOADED,
    STATUS_UPLOADING,
    TERMINAL_STATUSES,
    TASK_KIND_INGEST,
    TASK_KIND_UNDERSTAND,
    claim_task,
    list_tasks,
    reclaim_stale_processing,
    remove_task_records,
    reset_task_for_retry,
    run_task,
)
from app.tasks.understanding import (
    PROGRESS_UNDERSTANDING_DONE,
    PROGRESS_UNDERSTANDING_END,
    PROGRESS_UNDERSTANDING_START,
    list_clips,
    reset_task_understanding,
    retry_clip_understanding,
    run_understanding,
)

__all__ = [
    "PROGRESS_CONVERTED",
    "PROGRESS_DONE",
    "PROGRESS_PROBED",
    "PROGRESS_SPLIT_END",
    "PROGRESS_SPLIT_START",
    "PROGRESS_UNDERSTANDING_DONE",
    "PROGRESS_UNDERSTANDING_END",
    "PROGRESS_UNDERSTANDING_START",
    "STATUS_FAILED",
    "STATUS_PROCESSING",
    "STATUS_SUCCEEDED",
    "STATUS_UPLOADED",
    "STATUS_UPLOADING",
    "TERMINAL_STATUSES",
    "TASK_KIND_INGEST",
    "TASK_KIND_UNDERSTAND",
    "claim_task",
    "list_clips",
    "list_tasks",
    "reclaim_stale_processing",
    "remove_task_records",
    "requeue_pending",
    "reset_task_for_retry",
    "reset_task_understanding",
    "retry_clip_understanding",
    "run_sync",
    "run_task",
    "run_understanding",
    "shutdown",
    "submit",
]

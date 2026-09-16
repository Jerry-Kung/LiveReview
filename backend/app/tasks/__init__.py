"""后台任务包：执行器与状态流转。"""

from app.tasks.executor import requeue_pending, run_sync, shutdown, submit
from app.tasks.runner import (
    STATUS_FAILED,
    STATUS_PROCESSING,
    STATUS_SUCCEEDED,
    STATUS_UPLOADED,
    STATUS_UPLOADING,
    TERMINAL_STATUSES,
    TASK_KIND_INGEST,
    claim_task,
    list_tasks,
    remove_task_records,
    reset_task_for_retry,
    run_task,
)

__all__ = [
    "STATUS_FAILED",
    "STATUS_PROCESSING",
    "STATUS_SUCCEEDED",
    "STATUS_UPLOADED",
    "STATUS_UPLOADING",
    "TERMINAL_STATUSES",
    "TASK_KIND_INGEST",
    "claim_task",
    "list_tasks",
    "remove_task_records",
    "requeue_pending",
    "reset_task_for_retry",
    "run_sync",
    "run_task",
    "shutdown",
    "submit",
]

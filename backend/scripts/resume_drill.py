"""演练：传完分片就关页面 → 重启进程 → 恢复把这次上传做完。

用假 ffprobe/ffmpeg 与本机媒体目录，走的是与真实部署同一条代码路径
（`app.main` 的 lifespan → `tasks.requeue_pending`），只把外部依赖换成可控实现。
"""

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(tempfile.mkdtemp(prefix="liverreview-resume-"))
MEDIA = ROOT / "media"
DB = ROOT / "data" / "liverreview.db"
DB.parent.mkdir(parents=True, exist_ok=True)

HERE = Path(__file__).resolve().parent.parent  # backend/
CHUNK = 1024 * 1024
SIZE = 3 * CHUNK

os.environ.update(
    APP_ENV="development",
    DATABASE_URL=f"sqlite:///{DB.as_posix()}",
    MEDIA_ROOT=str(MEDIA),
    STORAGE_BACKEND="mock",
    UPLOAD_CHUNK_SIZE=str(CHUNK),
    # 用「解释器 + 脚本」形式的 JSON 列表：Windows 路径带反斜杠，必须走 json.dumps 转义
    FFPROBE_PATH=json.dumps([sys.executable, str(HERE / "tests" / "fake_ffprobe.py")]),
    FFMPEG_PATH=json.dumps([sys.executable, str(HERE / "tests" / "fake_ffmpeg.py")]),
    LLM_BASE_URL="",
    LLM_API_KEY="",
    LLM_MODEL_NAME="",
    APP_RELOAD="false",
)

from fastapi.testclient import TestClient  # noqa: E402

from app import chunks as chunk_store  # noqa: E402
from app.main import app  # noqa: E402

# —— 第一次「进程」：建会话、传完所有分片，然后在提交完成请求之前退出 ——
with TestClient(app) as client:
    created = client.post("/api/uploads", json={"filename": "live.ts", "size": SIZE}).json()
    upload_id, task_id = created["upload_id"], created["task_id"]
    for index in range(created["total_chunks"]):
        expected = chunk_store.expected_chunk_size(SIZE, CHUNK, index)
        body = bytes([index]) * expected
        assert client.put(f"/api/uploads/{upload_id}/chunks/{index}", content=body).status_code == 204

    status = client.get(f"/api/uploads/{upload_id}").json()
    print(f"[关页面] 会话 {upload_id} 状态={status['status']} 已收 {len(status['received_chunks'])}/{status['total_chunks']} 片")
    task = client.get(f"/api/tasks/{task_id}").json()
    print(f"[关页面] 任务 {task_id} 状态={task['status']} 进度={task['progress']}")

# —— 第二次「进程」：模拟容器重启，lifespan 里跑启动恢复 ——
# 任务由线程池异步执行，轮询到终态再断言
def wait_terminal(client, task_id, timeout=60.0):
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        payload = client.get(f"/api/tasks/{task_id}").json()
        if payload["status"] in ("succeeded", "failed"):
            return payload
        time.sleep(0.2)
    return payload


with TestClient(app) as client:
    status = client.get(f"/api/uploads/{upload_id}").json()
    print(f"[重启后] 会话状态={status['status']} 错误={status['error']}")
    task = wait_terminal(client, task_id)
    print(f"[重启后] 任务状态={task['status']} 进度={task['progress']} 片段数={len(task['clips'])}")
    print(f"[重启后] 探测时长={task['metadata']['duration_seconds'] if task['metadata'] else None}")
    print(f"[重启后] 覆盖校验={'通过' if task['coverage'] and not task['coverage']['issues'] else task['coverage']}")

    assert status["status"] == "completed", status
    assert chunk_store.received_indices(MEDIA, upload_id) == []
    assert task["status"] == "succeeded", task
    assert task["error"] is None
    assert task["object_key"]
    print(f"[结论] 关页面那次上传已被后端自动做完，产物 {len(task['clips'])} 片")

# —— 识别中断：造一条「跑到一半进程没了」的记录，重启后应退回续跑并重新入队 ——
#
# 这里不调真实模型：只验证启动恢复的状态流转与入队。识别本身的质量与编排由
# `tests/test_understanding.py` 覆盖。
from app.database import SessionLocal  # noqa: E402
from app.models import MediaClip, Task, utcnow  # noqa: E402

with SessionLocal() as db:
    task = db.get(Task, task_id)
    task.understanding_status = "running"
    task.understanding_progress = 50
    task.understanding_started_at = utcnow()
    targets = db.query(MediaClip).filter(MediaClip.task_id == task_id).order_by(MediaClip.index).all()
    # 第 0 片已成功、第 1 片还在途：这是「跑到一半进程被杀」的形态。
    # 素材只切出一片时补一条片段行进来，使「有一个未完成片段」这个前提成立。
    while len(targets) < 2:
        extra = MediaClip(
            task_id=task_id,
            index=len(targets),
            start_seconds=0.0,
            end_seconds=10.0,
            status="uploaded",
            object_key=f"liverreview/clip/clip_{len(targets):03d}_drill_0000.mp4",
        )
        db.add(extra)
        db.commit()
        targets = db.query(MediaClip).filter(MediaClip.task_id == task_id).order_by(MediaClip.index).all()

    targets[0].understanding_status = "succeeded"
    targets[0].understanding_segments_json = "[]"
    targets[0].understanding_finished_at = utcnow()
    targets[1].understanding_status = "running"
    db.commit()
    print(f"[识别中断] 任务置于 running，片段 {[c.understanding_status for c in targets]}")

with TestClient(app) as client:
    payload = wait_terminal(client, task_id)
    state = client.get(f"/api/tasks/{task_id}").json()
    # 模型未配置：恢复会把识别退回待续跑后立刻判失败，但状态流转与入队都发生了
    print(f"[重启后] 识别状态={state['understanding']['status']} 错误={state['understanding']['error']}")

    db = SessionLocal()
    try:
        clips = db.query(MediaClip).filter(MediaClip.task_id == task_id).order_by(MediaClip.index).all()
        print(f"[重启后] 片段识别状态={[c.understanding_status for c in clips]}")
        # 已成功的片段保留结论，不被恢复清掉——这是「重启不重复计费」的依据
        assert clips[0].understanding_status == "succeeded"
        assert clips[0].understanding_finished_at is not None
        if len(clips) > 1:
            assert clips[1].understanding_status != "running"
    finally:
        db.close()

    print("[结论] 被打断的识别已退回续跑（未配置模型时明确失败，而不是永远停在识别中）")

print(f"工作目录：{ROOT}")

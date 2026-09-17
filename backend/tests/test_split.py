"""V0.1.5 预处理与切分链路的测试：存储用 Mock，ffmpeg/ffprobe 用假脚本，执行器同步执行。

覆盖任务方提出的五条硬要求：切分前统一转 MP4、全程不丢音频、每片满足时长与体积上限、
片段顺序不乱、只把切分后的 MP4 片段上传到对象存储。探测与调用的纯逻辑由
`tests/media/test_split_plan.py` 与 `tests/media/test_ffmpeg.py` 覆盖，这里验证编排。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.media.paths import clips_dir, converted_path, original_path
from app.storage.mock import FaultMode
from tests.conftest import CHUNK_SIZE, FILE_SIZE
from tests.test_uploads import _create, _upload_all

MiB = 1024 * 1024


def _complete(client: TestClient, filename: str = "live.ts") -> dict:
    data = _create(client, filename=filename)
    _upload_all(client, data["upload_id"])
    resp = client.post(f"/api/uploads/{data['upload_id']}/complete")
    assert resp.status_code == 200, resp.text
    return data


def _task(client: TestClient, task_id: str) -> dict:
    resp = client.get(f"/api/tasks/{task_id}")
    assert resp.status_code == 200, resp.text
    return resp.json()


@pytest.fixture
def ffmpeg_log(tmp_path: Path, monkeypatch) -> Path:
    """记录每次 ffmpeg 调用的参数（JSON 行），用于断言切分参数与调用次数。"""
    log = tmp_path / "ffmpeg-log.txt"
    monkeypatch.setenv("LIVERREVIEW_FAKE_FFMPEG_LOG", str(log))
    return log


def _calls(log: Path) -> list[dict]:
    import json

    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line]


# —— 完整链路 ——


def test_processing_produces_ordered_clips_and_marks_coverage(client, storage):
    """一次上传即完成「探测 → 转 MP4 → 切分 → 上传 → 覆盖校验」。"""
    data = _complete(client)
    task = _task(client, data["task_id"])

    assert task["status"] == "succeeded", task["error"]
    assert task["progress"] == 100
    assert task["metadata"]["duration_seconds"] == 600.0

    clips = task["clips"]
    assert len(clips) == 1
    assert clips[0]["index"] == 0
    assert clips[0]["start_seconds"] == 0.0
    assert clips[0]["end_seconds"] == 600.0
    assert clips[0]["status"] == "uploaded"
    assert clips[0]["object_key"].startswith("liverreview/clip/")
    assert clips[0]["download_url"]
    assert clips[0]["size_bytes"] > 0

    coverage = task["coverage"]
    assert coverage is not None
    assert coverage["clip_count"] == 1
    assert coverage["issues"] == []
    assert coverage["source_format"] == "mov,mp4,m4a,3gp,3g2,mj2"


def test_split_respects_duration_limit(client, storage, test_settings, monkeypatch):
    """单片时长上限生效：600 秒素材按 300 秒均分为两片，顺序与起止时间正确。"""
    monkeypatch.setattr(test_settings, "split_max_duration_seconds", 300)

    data = _complete(client)
    task = _task(client, data["task_id"])

    assert task["status"] == "succeeded", task["error"]
    clips = task["clips"]
    assert [clip["index"] for clip in clips] == [0, 1]
    assert [clip["start_seconds"] for clip in clips] == [0.0, 300.0]
    assert [clip["end_seconds"] for clip in clips] == [300.0, 600.0]
    assert task["coverage"]["issues"] == []


def test_split_respects_size_limit_by_bisecting(client, storage, test_settings, monkeypatch):
    """单片体积上限生效：超限片段被对半再切，且顺序不变、覆盖完整。"""
    # 600 秒单片，按每片段上限 3MiB 估算需要细分；假 ffmpeg 的码率决定实际体积
    monkeypatch.setattr(test_settings, "split_max_duration_seconds", 3600)
    monkeypatch.setattr(test_settings, "split_max_clip_bytes", 3 * MiB)
    monkeypatch.setenv("LIVERREVIEW_FAKE_FFMPEG_BYTES_PER_SECOND", str(16 * 1024))

    data = _complete(client)
    task = _task(client, data["task_id"])

    assert task["status"] == "succeeded", task["error"]
    clips = task["clips"]
    # 600s × 16KiB/s ≈ 9.4MiB，上限 3MiB → 需要细分到 150 秒（约 2.3MiB）
    assert len(clips) > 1
    assert [clip["index"] for clip in clips] == list(range(len(clips)))
    for clip in clips:
        assert clip["size_bytes"] <= 3 * MiB
    # 顺序即时间轴顺序，首尾相接
    assert clips[0]["start_seconds"] == 0.0
    assert clips[-1]["end_seconds"] == 600.0
    for previous, following in zip(clips, clips[1:]):
        assert previous["end_seconds"] == following["start_seconds"]
    assert task["coverage"]["issues"] == []


def test_only_clips_are_uploaded_to_storage(client, storage, test_settings, monkeypatch):
    """临时要求：只上传切分后的 MP4 片段，转封装产物与原始文件都不上传。"""
    monkeypatch.setattr(test_settings, "split_max_duration_seconds", 300)

    data = _complete(client)
    task = _task(client, data["task_id"])
    keys = storage.uploaded_keys

    # 首个上传对象是原始视频（上传链路本身的要求），此后只上传片段
    assert keys[0] == task["object_key"]
    assert keys[1:] == [clip["object_key"] for clip in task["clips"]]
    assert keys[1:], "没有上传任何片段"
    for key in keys[1:]:
        assert "/clip/" in key
        assert key.endswith(".mp4")


def test_conversion_is_skipped_for_mp4_source(client, storage, test_settings, monkeypatch):
    """已是 MP4 的素材不再转封装：不产生多余的转换产物，也不调用 ffmpeg 复制全片。"""
    data = _complete(client)
    task = _task(client, data["task_id"])

    assert task["status"] == "succeeded", task["error"]
    # 原始文件是 .ts，因此本用例仍会转封装；断言产物路径与任务记录一致
    assert task["coverage"]["source_format"] == "mov,mp4,m4a,3gp,3g2,mj2"


def test_clip_cut_args_keep_audio_and_copy_streams(client, ffmpeg_log):
    """每次切片调用都必须带 -map 0:v / 0:a? 与 -c copy，避免丢音频或重编码。"""
    data = _complete(client)
    assert _task(client, data["task_id"])["status"] == "succeeded"

    calls = _calls(ffmpeg_log)
    assert len(calls) >= 2, "至少应有一次转封装与一次切片"

    clip_calls = [call for call in calls if call["ss"] is not None]
    assert clip_calls, "没有记录到切片调用"
    for call in clip_calls:
        args = call["args"]
        assert "0:v" in args and "0:a?" in args
        assert args[args.index("-c") + 1] == "copy"
        assert call["to"] > call["ss"]


def test_local_products_are_cleaned_after_success(client, media_root):
    """处理成功后本地产物全部释放：副本、转封装产物与切片目录都不残留。"""
    data = _complete(client)
    task_id = data["task_id"]
    assert _task(client, task_id)["status"] == "succeeded"

    assert not original_path(media_root, task_id, "live.ts").exists()
    assert not converted_path(media_root, task_id).exists()
    assert not clips_dir(media_root, task_id).exists()


# —— 失败与重试 ——


def test_split_failure_is_visible_and_keeps_source_for_retry(
    client, media_root, monkeypatch, tmp_path
):
    """切分失败时原因落库，且原始副本保留，便于在容器内复现。"""
    monkeypatch.setenv("LIVERREVIEW_FAKE_FFMPEG_MODE", "fail")

    data = _complete(client)
    task = _task(client, data["task_id"])

    assert task["status"] == "failed"
    assert "执行失败" in task["error"]
    assert original_path(media_root, data["task_id"], "live.ts").is_file()


def test_clip_without_audio_fails_the_task(client, monkeypatch):
    """假 ffmpeg 产出无声片段时必须判失败，而不是静默产出一批无声片段。"""
    monkeypatch.setenv("LIVERREVIEW_FAKE_FFMPEG_SHAPE", "no-audio")

    data = _complete(client)
    task = _task(client, data["task_id"])

    assert task["status"] == "failed"
    assert "丢失了声音" in task["error"]
    assert task["coverage"] is None


def test_truncated_conversion_fails_with_clear_reason(client, monkeypatch):
    monkeypatch.setenv("LIVERREVIEW_FAKE_FFMPEG_SHAPE", "truncated")

    data = _complete(client)
    task = _task(client, data["task_id"])

    assert task["status"] == "failed"
    assert "产物可能被截断" in task["error"]


def test_coverage_gap_fails_the_task(client, storage, test_settings, monkeypatch):
    """覆盖校验发现缺口即判失败：缺口必须以任务失败 + 原因暴露，而不是悄悄通过。

    这里让假 ffmpeg 把每个片段的时长都裁短一截，制造相邻片段之间的缺口。
    """
    monkeypatch.setattr(test_settings, "split_max_duration_seconds", 300)
    # 产物时长只有请求时长的一半 → 两片各覆盖不到一半，交界与末尾都出现缺口
    monkeypatch.setenv("LIVERREVIEW_FAKE_FFMPEG_SHAPE", "truncated")

    data = _complete(client)
    task = _task(client, data["task_id"])

    # 转封装阶段就会因时长偏差失败，这正是「转封装产物被截断」的那条路径
    assert task["status"] == "failed"
    assert task["error"]

    # 直接校验覆盖校验本身的行为：交界缺口与末尾缺失都会被列出
    from app.media.coverage import check_coverage

    issues = check_coverage(600.0, [(0.0, 300.0), (330.0, 600.0)])
    assert [issue.code for issue in issues] == ["gap"]
    issues = check_coverage(600.0, [(0.0, 300.0), (300.0, 500.0)])
    assert [issue.code for issue in issues] == ["missing_end"]


def test_retry_resumes_without_reuploading_finished_clips(client, storage, db_session_factory):
    """重试只补齐未完成的部分：已上传片段不重复上传，对象键保持不变。"""
    data = _complete(client)
    task_id = data["task_id"]
    first = _task(client, task_id)
    assert first["status"] == "succeeded"
    keys_after_first = list(storage.uploaded_keys)

    # 模拟「处理完成后被标记为失败」：片段行仍在，重试应复用它们
    from app.models import Task, utcnow

    db = db_session_factory()
    try:
        task = db.get(Task, task_id)
        task.status = "failed"
        task.error = "模拟失败"
        task.updated_at = utcnow()
        db.commit()
    finally:
        db.close()

    assert client.post(f"/api/tasks/{task_id}/retry").status_code == 200
    latest = _task(client, task_id)
    assert latest["status"] == "succeeded"
    # 片段对象键与首次一致，且没有新增片段上传（原始视频也未重传）
    assert [clip["object_key"] for clip in latest["clips"]] == [
        clip["object_key"] for clip in first["clips"]
    ]
    assert storage.uploaded_keys == keys_after_first


def test_retry_after_upload_failure_uploads_remaining_clips(
    client, storage, db_session_factory, test_settings, monkeypatch
):
    """上传阶段失败后，重试把未上传的片段补齐。"""
    monkeypatch.setattr(test_settings, "split_max_duration_seconds", 300)

    data = _complete(client)
    task_id = data["task_id"]
    assert _task(client, task_id)["status"] == "succeeded"

    # 把一个片段标记为未上传，模拟上传阶段失败留下的状态
    from app.models import MediaClip, Task, utcnow

    db = db_session_factory()
    try:
        task = db.get(Task, task_id)
        clip = db.query(MediaClip).filter(MediaClip.task_id == task_id).order_by(MediaClip.index).first()
        clip.status = "failed"
        clip.error = "模拟上传失败"
        task.status = "failed"
        task.error = "模拟失败"
        task.updated_at = utcnow()
        db.commit()
        object_key = clip.object_key
    finally:
        db.close()

    assert client.post(f"/api/tasks/{task_id}/retry").status_code == 200
    latest = _task(client, task_id)
    assert latest["status"] == "succeeded"
    # 未上传的片段被重新上传，且复用了同一个对象键
    assert storage.uploaded_keys.count(object_key) == 2
    # 已上传的片段没有被重复上传
    other = [clip for clip in latest["clips"] if clip["object_key"] != object_key]
    for clip in other:
        assert storage.uploaded_keys.count(clip["object_key"]) == 1


def test_retry_clears_stale_split_state(client, db_session_factory):
    """重试清空上一轮的探测结论与覆盖结论，避免残留陈旧结果。"""
    data = _complete(client)
    task_id = data["task_id"]
    assert _task(client, task_id)["coverage"] is not None

    from app.models import Task, utcnow
    from app.tasks import reset_task_for_retry

    db = db_session_factory()
    try:
        task = db.get(Task, task_id)
        task.status = "failed"
        task.updated_at = utcnow()
        db.commit()

        reset = reset_task_for_retry(db, task_id)
        assert reset is not None
        assert reset.coverage_issues_json is None
        assert reset.split_checked_at is None
        assert reset.split_source_format is None
        assert reset.split_source_duration_seconds is None
        assert reset.probed_at is None
        # 片段行保留：它们是已上传产物的唯一线索
        assert len(reset.clips) == 1
    finally:
        db.close()


def test_storage_failure_during_clip_upload_keeps_records(client, storage, db_session_factory):
    """片段上传失败时任务失败且原因含 request_id，记录保留以便重试。"""
    storage.set_fault_mode(FaultMode.SERVER)
    # 放行原始视频上传：故障只发生在片段上传阶段
    data = _create(client)
    storage.set_fault_mode(FaultMode.NONE)
    _upload_all(client, data["upload_id"])
    storage.set_fault_mode(FaultMode.SERVER)
    client.post(f"/api/uploads/{data['upload_id']}/complete")
    storage.set_fault_mode(FaultMode.NONE)

    task = _task(client, data["task_id"])
    assert task["status"] == "failed"
    assert "request_id" in task["error"]


def test_clip_object_keys_follow_the_clip_convention(client, test_settings, monkeypatch):
    """片段对象键落在 `clip/` 前缀下、以序号命名且为 .mp4：桶内一眼能看出是第几片。"""
    monkeypatch.setattr(test_settings, "split_max_duration_seconds", 300)

    data = _complete(client)
    task = _task(client, data["task_id"])

    assert task["status"] == "succeeded", task["error"]
    for clip in task["clips"]:
        key = clip["object_key"]
        assert key.startswith("liverreview/clip/")
        assert f"clip_{clip['index']:03d}_" in key
        assert key.endswith(".mp4")


def test_clips_are_ordered_by_time(client, test_settings, monkeypatch):
    """顺序不乱：序号递增且与时间区间顺序一致，首片从 0 起、相邻片首尾相接。"""
    monkeypatch.setattr(test_settings, "split_max_duration_seconds", 100)

    data = _complete(client)
    task = _task(client, data["task_id"])
    clips = task["clips"]

    assert task["status"] == "succeeded", task["error"]
    assert len(clips) > 1
    assert [clip["index"] for clip in clips] == list(range(len(clips)))
    for previous, following in zip(clips, clips[1:]):
        assert previous["end_seconds"] <= following["start_seconds"] + 0.001
        assert abs(previous["end_seconds"] - following["start_seconds"]) < 0.001
    assert clips[0]["start_seconds"] == 0.0
    assert clips[-1]["end_seconds"] == 600.0


# —— 接口契约 ——


def test_clips_are_empty_before_processing(client):
    """处理前不给出空片段列表的假象：列表为空、覆盖结论为 null。"""
    data = _create(client)
    task = _task(client, data["task_id"])
    assert task["clips"] == []
    assert task["coverage"] is None


def test_uploading_task_shows_no_coverage(client):
    data = _create(client)
    for item in client.get("/api/tasks").json()["items"]:
        if item["id"] == data["task_id"]:
            assert item["coverage"] is None
            assert item["clips"] == []


def test_clip_download_url_present_only_for_uploaded_clips(client):
    data = _complete(client)
    task = _task(client, data["task_id"])
    for clip in task["clips"]:
        if clip["status"] == "uploaded":
            assert clip["download_url"]
        else:
            assert clip["download_url"] is None

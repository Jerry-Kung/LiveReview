"""媒体探测命令行工具的测试：在容器内核对探测结果时，输出必须能直接指向原因。"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.config import Settings
from app.media import cli
from tests.conftest import fake_ffprobe_args


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        media_root=str(tmp_path / "media"),
        ffprobe_path=fake_ffprobe_args(),
    )


def test_prints_metadata_of_a_given_file(settings, tmp_path, monkeypatch, capsys):
    source = tmp_path / "sample.ts"
    source.write_bytes(b"\x00" * 1024)
    monkeypatch.setattr("app.config.get_settings", lambda: settings)

    code = cli.main([str(source)])

    output = capsys.readouterr().out
    assert code == 0
    assert "结论：探测成功" in output
    assert "600.0s" in output
    assert "1920x1080" in output


def test_reports_failure_with_reason(settings, tmp_path, monkeypatch, capsys):
    source = tmp_path / "broken.ts"
    source.write_bytes(b"\x00" * 1024)
    monkeypatch.setattr("app.config.get_settings", lambda: settings)
    monkeypatch.setenv("LIVERREVIEW_FAKE_PROBE_MODE", "bad-media")

    code = cli.main([str(source)])

    output = capsys.readouterr().out
    assert code == 1
    assert "探测失败" in output
    assert "Invalid data found" in output


def test_missing_file_is_reported_without_crashing(settings, db_session_factory, tmp_path, monkeypatch, capsys):
    """传入的目标既不是文件也不是任务 id 时，命令要把两种情况都说清楚并正常退出。"""
    monkeypatch.setattr("app.config.get_settings", lambda: settings)
    # 任务分支会查库；不隔离会让测试打到本地开发库，污染也依赖其表结构是否最新
    monkeypatch.setattr("app.database.SessionLocal", db_session_factory)

    code = cli.main([str(tmp_path / "absent.ts")])

    output = capsys.readouterr().out
    assert code == 1
    assert "未找到匹配的任务 id" in output


def test_unknown_task_prefix_is_reported(settings, db_session_factory, monkeypatch, capsys):
    """任务 id 分支要能优雅报错，而不是抛出栈。"""
    monkeypatch.setattr("app.config.get_settings", lambda: settings)
    monkeypatch.setattr("app.database.SessionLocal", db_session_factory)

    code = cli.main(["nosuchtask"])

    assert code == 1
    assert "未找到匹配的任务 id" in capsys.readouterr().out


def test_probes_the_local_copy_of_a_task(
    settings, db_session_factory, media_root, monkeypatch, capsys
):
    """按任务 id 执行时探测的是该任务的本地副本，路径由任务 id 与原始文件名推导。"""
    from app.media.paths import original_path
    from app.models import Task, UploadSession

    upload = UploadSession(
        id="up1", filename="live.ts", declared_size=2048, chunk_size=1024, status="completed"
    )
    task = Task(id="tk1", kind="ingest", status="failed", object_key="liverreview/original/x.ts", upload_id="up1")
    copy = original_path(media_root, "tk1", "live.ts")
    copy.parent.mkdir(parents=True, exist_ok=True)
    copy.write_bytes(b"\x00" * 2048)

    db = db_session_factory()
    try:
        db.add(upload)
        db.add(task)
        db.commit()
    finally:
        db.close()

    monkeypatch.setattr("app.config.get_settings", lambda: settings)
    monkeypatch.setattr("app.database.SessionLocal", db_session_factory)

    code = cli.main(["tk1"])

    output = capsys.readouterr().out
    assert code == 0
    assert "任务 tk1" in output
    assert str(copy) in output
    assert "结论：探测成功" in output


def test_clips_flag_lists_slices_with_time_ranges(
    settings, db_session_factory, media_root, monkeypatch, capsys
):
    """`--clips` 打印切片的时间顺序与覆盖结论，供测试环境抽查切分结果。"""
    from app.models import MediaClip, Task, utcnow

    task = Task(
        id="tk2",
        kind="ingest",
        status="succeeded",
        object_key="liverreview/original/x.ts",
        split_source_format="mov,mp4,m4a,3gp,3g2,mj2",
        split_source_duration_seconds=600.0,
        split_checked_at=utcnow(),
    )
    db = db_session_factory()
    try:
        db.add(task)
        db.add_all(
            [
                MediaClip(
                    task_id="tk2",
                    index=0,
                    start_seconds=0.0,
                    end_seconds=300.0,
                    duration_seconds=300.0,
                    size_bytes=1024,
                    object_key="liverreview/clip/clip_000.mp4",
                    status="uploaded",
                ),
                MediaClip(
                    task_id="tk2",
                    index=1,
                    start_seconds=300.0,
                    end_seconds=600.0,
                    duration_seconds=300.0,
                    size_bytes=1024,
                    object_key="liverreview/clip/clip_001.mp4",
                    status="uploaded",
                ),
            ]
        )
        db.commit()
    finally:
        db.close()

    monkeypatch.setattr("app.config.get_settings", lambda: settings)
    monkeypatch.setattr("app.database.SessionLocal", db_session_factory)

    code = cli.main(["tk2", "--clips"])

    output = capsys.readouterr().out
    assert code == 0
    assert "切片共 2 片" in output
    assert "#0  0.0s ~ 300.0s" in output
    assert "#1  300.0s ~ 600.0s" in output
    assert "覆盖校验：通过" in output


def test_clips_flag_reports_gap(settings, db_session_factory, media_root, monkeypatch, capsys):
    """切片之间存在缺口时 `--clips` 必须以非零退出码暴露问题。"""
    from app.models import MediaClip, Task

    task = Task(
        id="tk3",
        kind="ingest",
        status="failed",
        object_key="liverreview/original/x.ts",
        split_source_duration_seconds=600.0,
        error="SplitError: 覆盖校验发现 1 处问题",
    )
    db = db_session_factory()
    try:
        db.add(task)
        db.add_all(
            [
                MediaClip(task_id="tk3", index=0, start_seconds=0.0, end_seconds=300.0, status="uploaded"),
                MediaClip(task_id="tk3", index=1, start_seconds=330.0, end_seconds=600.0, status="uploaded"),
            ]
        )
        db.commit()
    finally:
        db.close()

    monkeypatch.setattr("app.config.get_settings", lambda: settings)
    monkeypatch.setattr("app.database.SessionLocal", db_session_factory)

    code = cli.main(["tk3", "--clips"])

    output = capsys.readouterr().out
    assert code == 1
    assert "缺失 30.0s" in output
    assert "失败原因：SplitError" in output


def test_clips_flag_without_records(settings, db_session_factory, monkeypatch, capsys):
    from app.models import Task

    db = db_session_factory()
    try:
        db.add(Task(id="tk4", kind="ingest", status="processing", object_key="k"))
        db.commit()
    finally:
        db.close()

    monkeypatch.setattr("app.config.get_settings", lambda: settings)
    monkeypatch.setattr("app.database.SessionLocal", db_session_factory)

    code = cli.main(["tk4", "--clips"])

    assert code == 1
    assert "没有切片记录" in capsys.readouterr().out

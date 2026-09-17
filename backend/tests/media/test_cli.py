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

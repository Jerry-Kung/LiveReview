"""真实 TOS 连通自检命令：上传 → 预签名可访问 → 下载校验 → 删除。

用法（测试环境后端容器内执行）：
    python -m app.storage.verify          # 校验后删除测试对象
    python -m app.storage.verify --keep   # 保留对象以便在控制台人工检查
"""

from __future__ import annotations

import argparse
import hashlib
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Callable, Sequence

from app.storage.base import (
    OBJECT_KIND_ORIGINAL,
    ObjectStorage,
    StorageClientError,
    StorageServerError,
)
from app.storage.mock import InMemoryStorage

StepResult = tuple[str, bool, str]
FetchBytes = Callable[[str], bytes]


def _default_fetch_bytes(url: str, timeout: float = 30.0) -> bytes:
    """用标准库按预签名 URL 拉取内容，避免为自检引入额外 HTTP 依赖。"""
    with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310 —— URL 由 TOS 签发
        return response.read()


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _make_mock_fetch_bytes(storage: InMemoryStorage) -> FetchBytes:
    """构造 Mock 后端专用的「预签名 URL 拉取」实现：不发真实 HTTP 请求。

    Mock 的预签名 URL 形如 `https://mock-tos.local/{bucket}/{object_key}?...`，
    这里按 `/{bucket}/` 前缀从 URL 路径中还原对象键（bucket 为空串时前缀退化为
    `//`，同样成立），再走内存对象读取，以保留「预签名 URL 定位对象」的语义。
    """

    def _fetch(url: str) -> bytes:
        path = urllib.parse.urlparse(url).path
        prefix = f"/{storage.config.bucket}/"
        if not path.startswith(prefix):
            raise StorageClientError(f"无法从 Mock 预签名 URL 中解析对象键：{url}")
        object_key = path[len(prefix) :]
        return storage.get_object_bytes(object_key)

    return _fetch


def run_verification(
    storage: ObjectStorage,
    *,
    fetch_bytes: FetchBytes | None = None,
    workdir: str | Path | None = None,
    keep: bool = False,
) -> list[StepResult]:
    """执行六步连通自检，返回每一步的结果；不抛异常，失败信息写在结果里。"""
    fetch = fetch_bytes or _default_fetch_bytes
    steps: list[StepResult] = []
    temp_dir = Path(workdir) if workdir is not None else Path(tempfile.mkdtemp(prefix="liverreview-verify-"))
    temp_dir.mkdir(parents=True, exist_ok=True)

    # 步骤 1：实现可用性
    try:
        backend_name = type(storage).__name__
        bucket = getattr(storage.config, "bucket", "-")
        steps.append(("配置与实现", True, f"实现={backend_name} bucket={bucket}"))
    except Exception as exc:  # noqa: BLE001 —— 自检需把所有失败转为结果而非崩溃
        steps.append(("配置与实现", False, f"{type(exc).__name__}: {exc}"))
        return steps

    # 步骤 2：生成随机内容的小文件
    payload = f"liverreview-verify-{uuid.uuid4().hex}\n".encode() * 64
    local_source = temp_dir / "verify-source.txt"
    try:
        local_source.write_bytes(payload)
        steps.append(
            ("生成测试文件", True, f"{local_source.name} size={len(payload)} sha256={_sha256(payload)[:12]}")
        )
    except Exception as exc:  # noqa: BLE001
        steps.append(("生成测试文件", False, f"{type(exc).__name__}: {exc}"))
        return steps

    # 步骤 3：上传
    object_key = storage.generate_object_key(OBJECT_KIND_ORIGINAL, local_source.name)
    try:
        stored = storage.upload_file(local_source, object_key)
        steps.append(("上传对象", True, f"key={stored.object_key} size={stored.size}"))
    except StorageServerError as exc:
        steps.append(("上传对象", False, f"服务端错误 code={exc.code} request_id={exc.request_id} message={exc.message}"))
        return steps
    except Exception as exc:  # noqa: BLE001
        steps.append(("上传对象", False, f"{type(exc).__name__}: {exc}"))
        return steps

    # 步骤 4：预签名 URL 可访问且内容一致
    try:
        url = storage.generate_presigned_url(object_key)
        fetched = fetch(url)
        if _sha256(fetched) != _sha256(payload):
            steps.append(
                (
                    "预签名 URL 可访问",
                    False,
                    f"内容不一致：预期 SHA256 {_sha256(payload)[:12]}，实际 {_sha256(fetched)[:12]}",
                )
            )
        else:
            steps.append(("预签名 URL 可访问", True, f"HTTP 拉取成功 size={len(fetched)}"))
    except urllib.error.URLError as exc:
        steps.append(("预签名 URL 可访问", False, f"HTTP 拉取失败：{exc}"))
    except Exception as exc:  # noqa: BLE001
        steps.append(("预签名 URL 可访问", False, f"{type(exc).__name__}: {exc}"))

    # 步骤 5：下载接口取回本地并比对
    local_target = temp_dir / "verify-downloaded.txt"
    try:
        storage.download_to_file(object_key, local_target)
        downloaded = local_target.read_bytes()
        if _sha256(downloaded) != _sha256(payload):
            steps.append(("下载并校验内容", False, f"内容不一致：实际 SHA256 {_sha256(downloaded)[:12]}"))
        else:
            steps.append(("下载并校验内容", True, f"下载文件 {local_target.name} 与源文件一致"))
    except StorageServerError as exc:
        steps.append(("下载并校验内容", False, f"服务端错误 code={exc.code} request_id={exc.request_id}"))
    except Exception as exc:  # noqa: BLE001
        steps.append(("下载并校验内容", False, f"{type(exc).__name__}: {exc}"))

    # 步骤 6：删除（--keep 时跳过，便于人工在控制台检查）
    if keep:
        steps.append(("删除对象", True, f"已跳过（--keep），对象保留：{object_key}"))
        return steps

    try:
        storage.delete_object(object_key)
        try:
            storage.get_object_bytes(object_key)
        except StorageServerError as exc:
            if exc.code == "NoSuchKey" or exc.status_code == 404:
                steps.append(("删除对象", True, f"已删除 key={object_key}"))
            else:
                # 其它服务端错误（如瞬时 5xx）不能确认对象已删除，须视为失败
                steps.append(
                    (
                        "删除对象",
                        False,
                        f"删除确认不确定：服务端错误 code={exc.code} request_id={exc.request_id}",
                    )
                )
        else:
            steps.append(("删除对象", False, f"删除后仍可读取：{object_key}"))
    except StorageServerError as exc:
        steps.append(("删除对象", False, f"服务端错误 code={exc.code} request_id={exc.request_id}"))
    except Exception as exc:  # noqa: BLE001
        steps.append(("删除对象", False, f"{type(exc).__name__}: {exc}"))

    return steps


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="TOS 对象存储连通自检")
    parser.add_argument("--keep", action="store_true", help="保留测试对象，便于在控制台人工检查")
    args = parser.parse_args(argv)

    from app.config import get_settings
    from app.storage import BACKEND_MOCK, build_storage

    settings = get_settings()
    print(f"存储实现选择：STORAGE_BACKEND={settings.storage_backend}")
    # Mock 后端不依赖 TOS 凭据，只有 auto/tos 缺少配置时才提前退出
    if settings.storage_backend != BACKEND_MOCK and not settings.storage_configured:
        print(f"缺少必填配置：{'、'.join(settings.storage_missing_fields)}")
        print("请在环境变量中配置 TOS_ACCESS_KEY / TOS_SECRET_KEY / TOS_ENDPOINT / TOS_REGION / TOS_BUCKET 后重试。")
        return 1

    storage = build_storage(settings)
    fetch_bytes = _make_mock_fetch_bytes(storage) if isinstance(storage, InMemoryStorage) else None
    steps = run_verification(storage, fetch_bytes=fetch_bytes, keep=args.keep)

    print("\n连通自检结果：")
    for name, passed, message in steps:
        print(f"  [{'通过' if passed else '失败'}] {name}：{message}")

    failed = [name for name, passed, _ in steps if not passed]
    if failed:
        print(f"\n结论：失败（{'、'.join(failed)}）")
        return 1
    print("\n结论：全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())

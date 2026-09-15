"""火山引擎 TOS 对象存储对接示例（自包含，可直接运行）。

覆盖：初始化客户端、上传文件、生成预签名 URL、删除对象、异常处理。

用法：
    1. 安装依赖:  pip install "tos>=2.6.0"
    2. 配置环境变量（AK/SK 必填，其余有默认值）:
           TOS_ACCESS_KEY / TOS_SECRET_KEY
           TOS_ENDPOINT / TOS_REGION / TOS_BUCKET / TOS_OBJECT_PREFIX / TOS_PRESIGNED_TTL_SECONDS
    3. 运行:      python tos-integration-example.py <本地文件路径>
"""
import os
import sys
import time
from dataclasses import dataclass

import tos
from tos import HttpMethodType
from tos.exceptions import TosClientError, TosServerError


@dataclass(frozen=True)
class TosConfig:
    endpoint: str
    region: str
    bucket: str
    object_prefix: str
    access_key_id: str
    secret_access_key: str
    presigned_ttl_seconds: int


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def load_config_from_env() -> TosConfig:
    """从环境变量读取配置；AK/SK 无默认，缺失时在调用处报错。"""
    return TosConfig(
        endpoint=os.environ.get("TOS_ENDPOINT", "tos-cn-beijing.volces.com"),
        region=os.environ.get("TOS_REGION", "cn-beijing"),
        bucket=os.environ.get("TOS_BUCKET", "bu-tmp"),
        object_prefix=os.environ.get("TOS_OBJECT_PREFIX", "tmp/"),
        access_key_id=os.environ.get("TOS_ACCESS_KEY", ""),
        secret_access_key=os.environ.get("TOS_SECRET_KEY", ""),
        presigned_ttl_seconds=_env_int("TOS_PRESIGNED_TTL_SECONDS", 3600),
    )


class TosStorage:
    """薄封装：上传 + 预签名合并为一个方法，并支持删除。"""

    def __init__(self, config: TosConfig):
        if not config.access_key_id or not config.secret_access_key:
            raise RuntimeError("TOS_ACCESS_KEY / TOS_SECRET_KEY 未配置")
        self._config = config
        # TosClientV2 线程安全，业务侧可复用同一实例
        self._client = tos.TosClientV2(
            config.access_key_id,
            config.secret_access_key,
            config.endpoint,
            config.region,
        )

    def _build_object_key(self, local_path: str) -> str:
        base = os.path.basename(local_path)
        name, ext = os.path.splitext(base)
        ts_ms = int(time.time() * 1000)
        # 前缀 + 原文件名 + 毫秒时间戳，避免同名覆盖
        return f"{self._config.object_prefix}{name}_{ts_ms}{ext}"

    def upload_and_presign(
        self, local_path: str, object_key: str | None = None
    ) -> tuple[str, str]:
        """上传本地文件并返回 (object_key, signed_url)。"""
        key = object_key or self._build_object_key(local_path)
        try:
            self._client.put_object_from_file(self._config.bucket, key, local_path)
            resp = self._client.pre_signed_url(
                HttpMethodType.Http_Method_Get,
                self._config.bucket,
                key,
                expires=self._config.presigned_ttl_seconds,
            )
        except TosServerError as exc:
            # request_id 可定位具体问题，写入日志但切勿记录 AK/SK
            print(f"上传失败(服务端): code={exc.code} request_id={exc.request_id} "
                  f"message={exc.message}")
            raise
        except TosClientError as exc:
            print(f"上传失败(客户端): message={exc.message}")
            raise
        print(f"上传成功: {key}")
        return key, resp.signed_url

    def delete(self, object_key: str) -> None:
        try:
            self._client.delete_object(self._config.bucket, object_key)
        except TosServerError as exc:
            print(f"删除失败(服务端): code={exc.code} request_id={exc.request_id}")
            raise
        except TosClientError as exc:
            print(f"删除失败(客户端): message={exc.message}")
            raise
        print(f"删除成功: {object_key}")


def main() -> None:
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(1)

    local_path = sys.argv[1]
    if not os.path.isfile(local_path):
        print(f"文件不存在: {local_path}")
        sys.exit(1)

    storage = TosStorage(load_config_from_env())

    # 完整流程：上传 -> 取预签名 URL -> 删除中转对象
    object_key, signed_url = storage.upload_and_presign(local_path)
    print(f"预签名 URL(有效期 {storage._config.presigned_ttl_seconds}s):\n{signed_url}")

    storage.delete(object_key)


if __name__ == "__main__":
    main()

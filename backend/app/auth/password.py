"""口令哈希：只做单向推导与校验，不保存明文。

V0.5 的用户是后端手工配置的，因此这里只提供「生成一份可写进配置的哈希」与
「校验候选口令」两件事，不引入第三方口令库——标准库的 `hashlib.pbkdf2_hmac`
足以覆盖本阶段的需求，少一个依赖就少一处需要跟进的供应链风险。

存储格式（自描述，便于日后换算法而不必迁移数据）：

    pbkdf2_sha256$<迭代次数>$<盐的十六进制>$<推导结果的十六进制>
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

# 迭代次数：取 OWASP 对 PBKDF2-HMAC-SHA256 的当前建议量级。校验在登录时发生，
# 一次约几十毫秒，对交互无感；对离线爆破则是实打实的成本。
DEFAULT_ITERATIONS = 240_000
SALT_BYTES = 16
ALGORITHM = "pbkdf2_sha256"


def hash_password(password: str, *, iterations: int = DEFAULT_ITERATIONS) -> str:
    """生成可写进 `AUTH_USERS` 的哈希串。"""
    if not password:
        raise ValueError("口令不能为空")
    salt = secrets.token_bytes(SALT_BYTES)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"{ALGORITHM}${iterations}${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    """校验候选口令是否匹配哈希串。

    任何格式问题都返回 False 而不是抛异常：配置写坏与口令不对，对调用方是同一件事
    ——这次登录不通过；把异常留给启动时的配置校验去报，登录路径不据此泄露细节。
    """
    try:
        algorithm, raw_iterations, raw_salt, raw_digest = encoded.split("$")
        if algorithm != ALGORITHM:
            return False
        iterations = int(raw_iterations)
        salt = bytes.fromhex(raw_salt)
        expected = bytes.fromhex(raw_digest)
    except (ValueError, AttributeError):
        return False
    if not password or iterations <= 0 or not salt or not expected:
        return False
    candidate = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    # 定长比较：用 compare_digest 而非 ==，避免比较过程本身泄露信息
    return hmac.compare_digest(candidate, expected)

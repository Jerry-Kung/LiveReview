"""会话令牌的载体：Cookie 名、令牌生成与摘要。

V0.5.1 起会话在服务端有记录（`sessions` 表，见 `app.auth.store`），因此这个模块
**不再做签名**：令牌是一串随机字符，安全边界从「验签」变成「查库能否命中」。

签名票据那套（HMAC + `AUTH_SESSION_SECRET`）随本次改造退休，原因见
`specs/v0.5.1-design.md`：它能证明「这张票据是我签发的」，却无法证明「这张票据
现在还有效」——要作废一张已签发的票据，只能换掉密钥把所有票据一起作废。

本模块刻意不导入包内任何东西：`app/auth/__init__.py` 要用这里的 `COOKIE_NAME`，
而包的初始化又会牵出 `store`、`service` 等模块，这里再反向导入就会成环。
"""

from __future__ import annotations

import hashlib
import secrets

COOKIE_NAME = "liverreview_session"

# 令牌的随机字节数：256 位熵，暴力枚举不可行，因此摘要不需要加盐，也不需要慢哈希
TOKEN_BYTES = 32
# 令牌摘要的十六进制长度，与 `sessions.id` 的列宽一致
TOKEN_DIGEST_LENGTH = 64


def new_token() -> str:
    """生成一张新的会话令牌。只在签发时出现一次，之后服务端只留摘要。"""
    return secrets.token_urlsafe(TOKEN_BYTES)


def hash_token(token: str) -> str:
    """令牌摘要。确定性、无盐，因此可以直接当主键用来查会话。

    用的是 SHA-256 而不是口令那套 PBKDF2：令牌是高熵随机串，没有字典可猜，
    慢哈希只会让每个请求多花几十毫秒而换不到实际强度。
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()

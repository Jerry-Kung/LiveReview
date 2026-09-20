"""用户配置：从环境变量读取初始化用户，不在代码里写死账号。

配置形态（`AUTH_USERS`，逗号分隔）：

    <账号>:<口令哈希>[:<显示名>]

- 账号与显示名都不允许包含 `:` 与 `,`，口令本身以哈希形式出现，因此分隔符不会歧义。
- 口令哈希用 `python -m app.auth.passwd` 生成，明文的初始化口令只存在于任务方手中。
- 显示名缺省取账号本身，界面上的称呼不构成鉴权依据。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# 账号与显示名的非法字符：都是配置的分隔符，混进来会切错字段
FORBIDDEN_CHARS = (":", ",")


@dataclass(frozen=True)
class AuthUser:
    """一个可登录的初始化用户。口令只以哈希形式持有。"""

    username: str
    password_hash: str
    display_name: str


class AuthConfigError(ValueError):
    """`AUTH_USERS` 写坏：启动时直接失败，不带着无法登录的配置把服务拉起来。"""


def parse_users(raw: str | None) -> dict[str, AuthUser]:
    """把环境变量解析为账号到用户的映射，失败即抛 `AuthConfigError`。

    这里刻意不宽容：配置写错时静默跳过该条，会让「某个账号怎么也登不上」变成一道
    需要翻日志的谜题，不如启动时就报出来。
    """
    users: dict[str, AuthUser] = {}
    text = (raw or "").strip()
    if not text:
        return users

    for position, chunk in enumerate(text.split(","), start=1):
        entry = chunk.strip()
        if not entry:
            continue
        parts = entry.split(":")
        if len(parts) not in (2, 3):
            raise AuthConfigError(
                f"AUTH_USERS 第 {position} 项格式不正确，应为 账号:口令哈希[:显示名]"
            )
        username = parts[0].strip()
        password_hash = parts[1].strip()
        display_name = parts[2].strip() if len(parts) == 3 else ""
        if not username or not password_hash:
            raise AuthConfigError(f"AUTH_USERS 第 {position} 项的账号或口令哈希为空")
        for value, label in ((username, "账号"), (display_name, "显示名")):
            if any(char in value for char in FORBIDDEN_CHARS):
                raise AuthConfigError(f"AUTH_USERS 第 {position} 项的{label}含冒号或逗号")
        if username in users:
            raise AuthConfigError(f"AUTH_USERS 中账号重复：{username}")
        users[username] = AuthUser(
            username=username,
            password_hash=password_hash,
            display_name=display_name or username,
        )
    return users


def describe_missing(raw: str | None) -> list[str]:
    """列出未配置用户时所缺的项名，供健康检查与启动日志说明原因，不回显任何取值。"""
    return [] if (raw or "").strip() else ["AUTH_USERS"]

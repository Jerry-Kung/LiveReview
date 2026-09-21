"""命令行工具：冷启动阶段创建管理员账号。

用法：

    python -m app.auth.init_admin                    # 交互式创建（或重设）管理员
    python -m app.auth.init_admin --list             # 列出已有账号
    python -m app.auth.init_admin --username admin --password 'xxx'   # 非交互，供自动化使用

交互式创建的流程：

    管理员用户名：admin
    管理员密码：********
    再次输入密码：********
    管理员创建成功

几处刻意如此的设计：

- **口令不回显**（`getpass`），两次输入不一致即中止，不做「先存下再问一次」。
- **账号已存在时不静默覆盖**：那等于给了一条「把别人口令改掉」的顺手路径。此时说明
  情况并单独再问一次，答 y 才重设口令；`--force` 跳过这次确认，供脚本使用。
- **建表在这里也做一次**：冷启动时数据库里可能一张表都没有，先建好再写账号，
  否则脚本会在一个「表不存在」的报错上结束，而使用者并不知道要先跑一次服务。
- 明文口令不落盘、不进日志，只在内存里活到变成哈希为止。
"""

from __future__ import annotations

import argparse
import getpass
import logging
import sys

from sqlalchemy.exc import IntegrityError

from app.auth import store
# 口令长度下限与哈希参数同源：界面上建号走的是同一条策略，两处不能各写一份
from app.auth.password import MIN_PASSWORD_LENGTH
from app.models import ROLE_ADMIN

DEFAULT_USERNAME = "admin"


class PromptAborted(Exception):
    """交互输入不合法导致中止。由 `main` 转成退出码，不在深层直接结束进程。"""


def _prompt_username() -> str:
    answer = input(f"管理员用户名（直接回车使用 {DEFAULT_USERNAME}）：").strip()
    return answer or DEFAULT_USERNAME


def _prompt_password() -> str:
    """读两次口令并比对。长度下限是为拦下「手一滑回车了」这类输入。

    失败时抛 `PromptAborted` 而不是自己 `SystemExit`：让退出码只由 `main` 一处决定，
    调用方（测试、将来可能的调用脚本）也就有办法接住它。
    """
    first = getpass.getpass("管理员密码：")
    if not first:
        raise PromptAborted("密码不能为空。")
    if len(first) < MIN_PASSWORD_LENGTH:
        raise PromptAborted(f"密码过短：至少 {MIN_PASSWORD_LENGTH} 个字符。")
    second = getpass.getpass("再次输入密码：")
    if first != second:
        raise PromptAborted("两次输入的密码不一致。")
    return first


def _confirm(question: str) -> bool:
    return input(f"{question}（y/N）：").strip().lower() in ("y", "yes")


def _print_list() -> int:
    from app.database import SessionLocal, init_db

    init_db()
    db = SessionLocal()
    try:
        users = store.list_users(db)
        if not users:
            print("数据库中还没有任何账号。执行 python -m app.auth.init_admin 创建一个。")
            return 0
        print(f"共 {len(users)} 个账号：")
        for user in users:
            last = user.last_login_at.strftime("%Y-%m-%d %H:%M") if user.last_login_at else "从未登录"
            print(f"  {user.username}  [{user.role}]  {user.display_name}  上次登录：{last}")
    finally:
        db.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="创建或重设管理员账号")
    parser.add_argument("--username", help="账号名；省略则交互式询问")
    parser.add_argument(
        "--password", help="口令；省略则交互式询问（不回显）。命令行传参会被 shell 历史记录，慎用"
    )
    parser.add_argument("--display-name", help="界面上的称呼；省略则取账号名")
    parser.add_argument("--force", action="store_true", help="账号已存在时直接重设口令，不再询问")
    parser.add_argument("--list", action="store_true", help="列出已有账号后退出")
    args = parser.parse_args(argv)

    if args.list:
        return _print_list()

    # 表结构先建好：冷启动时数据库可能是空的，脚本不该要求使用者先启动一次服务
    from app.database import SessionLocal, init_db

    init_db()

    try:
        username = (args.username or "").strip() if args.username else _prompt_username()
        if not username:
            print("账号不能为空。", file=sys.stderr)
            return 2
        password = args.password or _prompt_password()
        if len(password) < MIN_PASSWORD_LENGTH:
            print(f"密码过短：至少 {MIN_PASSWORD_LENGTH} 个字符。", file=sys.stderr)
            return 2
    except PromptAborted as exc:
        print(str(exc), file=sys.stderr)
        return 2

    display_name = (args.display_name or "").strip() or None

    db = SessionLocal()
    try:
        existing = store.get_user_by_username(db, username)
        if existing is not None:
            if not args.force:
                print(f"账号 {username} 已存在（角色 {existing.role}）。")
                if not _confirm(f"要把它的口令重设为刚才输入的值吗？该账号已有会话会一并失效"):
                    print("已取消，未做任何修改。")
                    return 1
            store.set_password(db, existing, password)
            removed = store.delete_user_sessions(db, existing)
            print(f"账号 {username} 的口令已重设。", end="")
            if removed:
                # 说清副作用：口令变了，旧会话不该继续有效
                print(f"该账号原有 {removed} 个登录会话已作废，需要重新登录。")
            else:
                print()
            return 0

        try:
            user = store.create_user(
                db,
                username=username,
                password=password,
                display_name=display_name or username,
                role=ROLE_ADMIN,
            )
        except IntegrityError:
            # 唯一约束兜底：并发执行两个初始化脚本时，先写入的那个赢
            db.rollback()
            print(f"账号 {username} 已被另一个进程创建，请重新执行以确认。", file=sys.stderr)
            return 1

        print(f"管理员 {user.username} 创建成功，界面称呼为「{user.display_name}」。")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    # 脚本在命令行里跑，日志默认不输出；把级别压到 WARNING 以上避免建表等噪声
    logging.basicConfig(level=logging.WARNING)
    raise SystemExit(main())

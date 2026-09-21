"""命令行工具：生成或校验一份口令哈希。

用法：

    python -m app.auth.passwd                 # 交互式输入口令（不回显）
    python -m app.auth.passwd --generate      # 生成一条随机口令并给出哈希
    python -m app.auth.passwd --check <哈希>   # 校验哈希串格式是否可被本模块解析

V0.5.1 起账号存在数据库里，日常建号请用 `python -m app.auth.init_admin`（它直接写库，
不需要手工搬运哈希）。这个工具保留下来是为了两件事：其一，校验一条哈希是否可用；
其二，需要把哈希搬到别处时（例如导出给别的环境）有个不依赖数据库的生成入口。

明文口令只打印到标准输出，不落盘、不进日志。
"""

from __future__ import annotations

import argparse
import getpass
import secrets
import string
import sys

from app.auth.password import DEFAULT_ITERATIONS, hash_password, verify_password

# 随机口令的字符集：去掉容易看错的 0/O/1/l/I，便于手工抄写
PASSWORD_ALPHABET = "".join(
    char for char in string.ascii_letters + string.digits if char not in "0O1lI"
)
GENERATED_PASSWORD_LENGTH = 16


def _generate_password() -> str:
    return "".join(secrets.choice(PASSWORD_ALPHABET) for _ in range(GENERATED_PASSWORD_LENGTH))


def _prompt_password() -> str:
    first = getpass.getpass("请输入口令：")
    if not first:
        print("口令不能为空。", file=sys.stderr)
        raise SystemExit(2)
    second = getpass.getpass("请再次输入以确认：")
    if first != second:
        print("两次输入不一致。", file=sys.stderr)
        raise SystemExit(2)
    return first


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成或校验一份口令哈希")
    parser.add_argument("--generate", action="store_true", help="生成一条随机口令并输出其哈希")
    parser.add_argument("--check", metavar="哈希", help="校验一条口令哈希串能否被解析")
    args = parser.parse_args(argv)

    if args.check:
        encoded = args.check
        # 用一个肯定不匹配的候选口令走一遍完整校验路径：能走到比较环节即说明格式可解析
        parseable = encoded.count("$") == 3 and verify_password("\x00probe", encoded) is False
        print("格式可用。" if parseable else "无法解析：请检查分段与十六进制内容。")
        return 0 if parseable else 1

    password = _generate_password() if args.generate else _prompt_password()
    print(hash_password(password, iterations=DEFAULT_ITERATIONS))
    if args.generate:
        print(f"# 明文口令（仅本次显示，请自行保存）：{password}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

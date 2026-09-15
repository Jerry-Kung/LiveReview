"""从配置读取 host 与 port 的 uvicorn 启动入口。

用法：`uv run python -m app`，监听地址、端口与是否 reload 均由 .env 配置。
"""

import uvicorn

from app.config import get_settings


def main() -> None:
    settings = get_settings()
    uvicorn.run(
        "app.main:app",
        host=settings.app_host,
        port=settings.app_port,
        reload=settings.app_reload,
    )


if __name__ == "__main__":
    main()

"""FastAPI 应用工厂与路由装配。"""

from contextlib import asynccontextmanager

from fastapi import FastAPI

from app import tasks
from app.config import get_settings
from app.database import init_db
from app.routers import health, tasks as tasks_router
from app.routers import review
from app.routers import understanding
from app.routers import uploads


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 建表在启动时完成：V0 阶段表结构仍在演进，不引入迁移工具
    init_db()
    # 进程重启后在途任务已丢失，把待处理任务重新入队
    tasks.requeue_pending()
    yield
    tasks.shutdown()


# 版本号统一取自配置，避免文档与接口各写一份造成漂移
app = FastAPI(title="LiveReview", version=get_settings().app_version, lifespan=lifespan)

app.include_router(health.router)
app.include_router(uploads.router)
app.include_router(tasks_router.router)
# 识别与复盘路由挂在同一 /api/tasks 前缀下；具体路径（transcript.txt、review.md）在模块内
# 先注册，避免被更宽泛的同级路由抢先匹配
app.include_router(understanding.router)
app.include_router(review.router)

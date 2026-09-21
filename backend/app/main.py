"""FastAPI 应用工厂与路由装配。"""

import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI

from app import tasks
from app.auth import require_admin, require_trusted_origin, require_user
from app.config import get_settings
from app.database import init_db
from app.routers import accounts
from app.routers import auth as auth_router
from app.routers import health, tasks as tasks_router
from app.routers import review
from app.routers import understanding
from app.routers import uploads

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 建表在启动时完成：V0 阶段表结构仍在演进，不引入迁移工具
    init_db()
    # 过期会话在每次撞上时会被即时删除，但「登录过一次就再没回来」的那些没人去撞，
    # 启动时顺手清一遍即可，不需要额外的定时任务
    _purge_expired_sessions()
    # 鉴权状态的问题（数据库里一个账号都没有）在启动日志里说清楚，
    # 否则「服务起来了但没人能登录」只能靠翻文档才知道
    for warning in get_settings().auth_warnings:
        logger.warning("鉴权配置：%s", warning)
    # 进程重启后在途任务已丢失，把待处理任务重新入队
    tasks.requeue_pending()
    yield
    tasks.shutdown()


def _purge_expired_sessions() -> None:
    """清掉已过期的会话记录。失败只记日志：这是清理动作，不该拦住服务启动。"""
    from app.auth import store
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        removed = store.purge_expired_sessions(db)
        if removed:
            logger.info("已清理 %d 条过期会话", removed)
    except Exception:  # noqa: BLE001 —— 清理失败不影响服务可用性
        logger.exception("清理过期会话失败")
    finally:
        db.close()


# 版本号统一取自配置，避免文档与接口各写一份造成漂移
app = FastAPI(title="LiveReview", version=get_settings().app_version, lifespan=lifespan)

# 公开路由：健康检查供编排探针调用；鉴权接口必须登录前可达，否则没人能登录。
# `require_trusted_origin` 只管同源，不管登录——退出登录在未登录时也应返回成功，
# 但它确实改变客户端状态，跨站发起的退出请求不该被接受。
app.include_router(health.router)
app.include_router(auth_router.router, dependencies=[Depends(require_trusted_origin)])

# 业务路由整组要求登录：依赖在装配处统一挂上，而不是逐个端点写参数——漏掉一个端点
# 就等于漏掉一条数据出口，集中在装配处只有一个地方需要检查。
# 下载口（transcript.txt / review.md）由浏览器直接发起请求，同样落在这层保护内。
auth_required = [Depends(require_user)]
app.include_router(uploads.router, dependencies=auth_required)
app.include_router(tasks_router.router, dependencies=auth_required)
# 识别与复盘路由挂在同一 /api/tasks 前缀下；具体路径（transcript.txt、review.md）在模块内
# 先注册，避免被更宽泛的同级路由抢先匹配
app.include_router(understanding.router, dependencies=auth_required)
app.include_router(review.router, dependencies=auth_required)

# 账号管理（V0.5.2）整组要求管理员：依赖放在 `require_admin` 里，它自己依赖 `require_user`，
# 因此这里**不再叠加** auth_required——重复挂载不会多查库（依赖按调用者缓存），但会让人
# 误以为这组端点的登录要求与别处不同。
app.include_router(accounts.router, dependencies=[Depends(require_admin)])

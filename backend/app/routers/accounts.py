"""账号管理接口（V0.5.2）：列出、新建、删除账号。

这一组端点只有管理员能用（`require_admin`）。在此之前建号只有一条路径——在服务端跑
`python -m app.auth.init_admin`，每加一个使用者都要运维登进容器执行命令。本版把普通账号
的增删交回界面，管理员账号本身仍然只能由脚本创建。

三条对外约定：

1. **角色由服务端决定，不由请求体决定**。新建的账号一律是 `member`，请求体里没有角色
   字段——让界面能指定角色等于把提权路径开在接口上。
2. **管理员账号不可删除**。判定按角色而不是按用户名或 id：`init_admin --username` 可以
   建出任意名字的管理员账号，按名字硬编码会漏掉它们。
3. **口令长度下限与初始化脚本同源**，取自 `app.auth.password.MIN_PASSWORD_LENGTH`。
   不在 pydantic 里用 `min_length`：那会返回 422 加一段英文，而本项目的失败文案是直接
   展示给使用者的中文 `detail`。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth import CurrentUser, require_admin
from app.auth import store
from app.auth.password import MIN_PASSWORD_LENGTH
from app.database import get_db
from app.models import ROLE_ADMIN, ROLE_MEMBER, User
from app.schemas import (
    AccountCreateRequest,
    AccountListResponse,
    AccountResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/accounts", tags=["accounts"])

# 失败文案：都要说清「做什么能通过」，而不只是「你错了」
DUPLICATE_DETAIL = "该账号已存在，请换一个"
PASSWORD_TOO_SHORT_DETAIL = f"密码过短：至少 {MIN_PASSWORD_LENGTH} 个字符"
DELETE_ADMIN_DETAIL = "管理员账号不能删除"
NOT_FOUND_DETAIL = "账号不存在"


def _to_response(user: User) -> AccountResponse:
    return AccountResponse(
        id=user.id,
        username=user.username,
        display_name=user.display_name,
        role=user.role,
        created_at=user.created_at,
        last_login_at=user.last_login_at,
    )


@router.get("", response_model=AccountListResponse)
def list_accounts(
    current: CurrentUser = Depends(require_admin),
    db: Session = Depends(get_db),
) -> AccountListResponse:
    """全部账号，按账号名排序。"""
    return AccountListResponse(items=[_to_response(user) for user in store.list_users(db)])


@router.post("", response_model=AccountResponse, status_code=status.HTTP_201_CREATED)
def create_account(
    payload: AccountCreateRequest,
    current: CurrentUser = Depends(require_admin),
    db: Session = Depends(get_db),
) -> AccountResponse:
    """新建一个普通账号。

    口令下限在这里判定而不是靠 pydantic 的 `min_length`：后者返回的 422 带一段英文
    校验说明，与其余接口的中文 `detail` 不是同一种东西。
    """
    username = payload.username.strip()
    if not username:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="账号不能为空"
        )
    if len(payload.password) < MIN_PASSWORD_LENGTH:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=PASSWORD_TOO_SHORT_DETAIL
        )

    try:
        user = store.create_user(
            db,
            username=username,
            password=payload.password,
            display_name=payload.display_name.strip() or username,
            # 角色写死：界面只建普通账号，管理员账号由 `init_admin` 创建
            role=ROLE_MEMBER,
        )
    except IntegrityError:
        # 唯一约束兜底，不先查一次：并发下「先查再写」本来就挡不住重复
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=DUPLICATE_DETAIL
        ) from None

    logger.info("管理员 %s 新建账号：%s", current.username, user.username)
    return _to_response(user)


@router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_account(
    user_id: int,
    current: CurrentUser = Depends(require_admin),
    db: Session = Depends(get_db),
) -> None:
    """删除一个普通账号，并作废它手上的全部会话。

    管理员账号一律拒绝，无需再判「是不是删自己」：既然没有任何管理员能被删掉，
    删自己这条路径自然不存在（`current` 本身就是管理员，否则走不到这里）。
    """
    user = store.get_user_by_id(db, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=NOT_FOUND_DETAIL)
    if user.role == ROLE_ADMIN:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=DELETE_ADMIN_DETAIL
        )

    # 会话本会由外键级联一并清掉（`User.sessions` 的 delete-orphan），这里显式再删一次
    # 是为了让「删账号即踢下线」这个意图在代码里可读，而不是依赖一处关系声明
    removed = store.delete_user_sessions(db, user)
    store.delete_user(db, user)
    logger.info(
        "管理员 %s 删除账号：%s（一并作废 %d 个会话）", current.username, user.username, removed
    )

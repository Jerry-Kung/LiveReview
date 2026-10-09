"""系统级可变配置的读写（V0.7.0）。

单行 `app_settings` 表是唯一存放处，主键固定为 `SETTINGS_ROW_ID`。这里只做两件事：读全自动
流程开关、写全自动流程开关。两者都要求调用方传入数据库会话，而不是在模块里自开事务——
取值只在两处被读到（设置页的请求处理、后台串接的每个阶段结束时一次），由调用方决定事务
边界比在这里藏一个隐式事务更清楚。

「读不到行即默认值」是本模块唯一的容错承诺：表刚建出来还没有任何一行时（首次启动），开关
按**开启**读——V0.7.1 起这是产品的期望默认，让开箱即用的链路是「上传完自己跑完」，而不是
要人先找开关。**不在读路径上顺手建行**——写操作应当有明确的来源（谁在设置页点了什么），
由读隐式建行会让「这行是谁建的」变得不可读；相应地，「默认开启」只体现为读回来的假值，
库里始终没有行，第一次有人关掉开关时才落第一行。
"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.models import SETTINGS_ROW_ID, AppSetting, utcnow

logger = logging.getLogger(__name__)


def read_auto_pipeline(db: Session) -> bool:
    """读全自动流程开关；表里没有这一行时按默认值「开启」处理（V0.7.1）。"""
    row = db.get(AppSetting, SETTINGS_ROW_ID)
    return bool(row.auto_pipeline_enabled) if row is not None else True


def write_auto_pipeline(db: Session, *, enabled: bool, updated_by: str | None) -> AppSetting:
    """写全自动流程开关并提交，返回写入后的行；行不存在则建行。

    只改这一个开关：`AppSetting` 上日后若新增其它配置项，它们的写入路径各自成函数，
    不复用这里——一个「按字段名批量赋值」的入口会让「这次到底改了什么」在调用点不可见。
    """
    row = db.get(AppSetting, SETTINGS_ROW_ID)
    if row is None:
        row = AppSetting(id=SETTINGS_ROW_ID)
        db.add(row)
    row.auto_pipeline_enabled = enabled
    row.updated_by = updated_by
    row.updated_at = utcnow()
    db.commit()
    return row

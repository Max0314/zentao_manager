# -*- coding: utf-8 -*-
"""FastAPI 应用入口。

这里负责创建全局对象、注册后台定时任务，并暴露管理接口。
业务同步逻辑放在 SyncService 中，避免入口文件变得臃肿。
"""

import logging
from contextlib import asynccontextmanager
from typing import Annotated

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import Depends, FastAPI, Header, HTTPException, Query, status

from app.bi_client import BiCenterClient
from app.config import load_settings
from app.state_store import StateStore
from app.sync_service import SyncService
from app.zentao_client import ZentaoClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger(__name__)

# 应用启动时读取配置并组装依赖。Docker 中修改 .env 后需要重启容器才能生效。
settings = load_settings()
store = StateStore(settings.sqlite_path, settings.initial_sync_start)
zentao_client = ZentaoClient(settings)
bi_client = BiCenterClient(settings)
sync_service = SyncService(settings, store, zentao_client, bi_client)
scheduler = AsyncIOScheduler(timezone=settings.timezone)


async def require_manager_token(authorization: Annotated[str | None, Header()] = None) -> None:
    """保护同步管理接口。

    未配置 MANAGER_API_TOKEN 时保持内网兼容模式；一旦配置，则要求调用方使用
    Authorization: Bearer <MANAGER_API_TOKEN>。
    """
    if not settings.manager_api_token:
        return

    expected = f"Bearer {settings.manager_api_token}"
    if authorization != expected:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """FastAPI 生命周期钩子。

    服务启动时启动 APScheduler；服务关闭时停止 scheduler，避免后台任务残留。
    """

    # 主同步任务：按 SYNC_INTERVAL_MINUTES 拉取禅道新增 action 并推送 bi_center。
    scheduler.add_job(
        sync_service.run_once,
        "interval",
        minutes=settings.sync_interval_minutes,
        id="zentao_sync",
        kwargs={"run_type": "scheduled"},
        max_instances=1,
        coalesce=True,
    )

    # 失败补发任务：按 FAILED_RETRY_INTERVAL_MINUTES 重试 outbox 中的失败批次。
    scheduler.add_job(
        sync_service.retry_failed,
        "interval",
        minutes=settings.failed_retry_interval_minutes,
        id="retry_failed_batches",
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()
    logger.info("Scheduler started")
    try:
        yield
    finally:
        scheduler.shutdown(wait=False)
        logger.info("Scheduler stopped")


app = FastAPI(title="Zentao Manager", version="1.0.0", lifespan=lifespan)


@app.get("/health")
async def health():
    """健康检查接口。

    这里只确认 Web 服务存活；数据库深度检查可以通过 /sync/run 或探测脚本验证。
    """
    return {"status": "ok"}


@app.get("/sync/status", dependencies=[Depends(require_manager_token)])
async def sync_status():
    """查看本地同步状态、游标和失败队列数量。"""
    return store.get_status()


@app.post("/sync/run", dependencies=[Depends(require_manager_token)])
async def run_sync():
    """手动触发一次同步，适合部署后验证或临时补推。"""
    return await sync_service.run_once(run_type="manual")


@app.post("/sync/backfill", dependencies=[Depends(require_manager_token)])
async def run_backfill(
    initial_sync_start: str = Query(default="2026-03-01 00:00:00"),
    streams: str = Query(default="actions,aiscore_results,tasks,bugs"),
    max_batches_per_stream: int = Query(default=120, ge=1, le=500),
    reset_cursors: bool = Query(default=True),
):
    """手动回填指定数据流。

    典型用法：回补 2026 年 3、4 月禅道 AI 分和任务/Bug 快照。
    """
    stream_names = [stream.strip() for stream in streams.split(",") if stream.strip()]
    return await sync_service.run_backfill(
        streams=stream_names,
        initial_sync_start=initial_sync_start,
        max_batches_per_stream=max_batches_per_stream,
        reset_cursors=reset_cursors,
    )


@app.post("/sync/retry-failed", dependencies=[Depends(require_manager_token)])
async def retry_failed():
    """手动触发失败队列重试。"""
    return await sync_service.retry_failed()

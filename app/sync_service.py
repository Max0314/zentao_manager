# -*- coding: utf-8 -*-
"""同步编排模块。

本模块串起三件事：
1. 从禅道数据库读取多个数据流的增量或快照批次。
2. 组装标准 JSON 批次并推送给 bi_center。
3. 推送失败时写入 outbox，后台定时补发。
"""

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.bi_client import BiCenterClient
from app.config import Settings
from app.state_store import StateStore
from app.zentao_client import ZentaoClient

logger = logging.getLogger(__name__)


class SyncService:
    """核心同步服务。

    FastAPI 手动接口和 APScheduler 定时任务都会调用这个类。
    """

    def __init__(
        self,
        settings: Settings,
        store: StateStore,
        zentao_client: ZentaoClient,
        bi_client: BiCenterClient,
    ) -> None:
        self.settings = settings
        self.store = store
        self.zentao_client = zentao_client
        self.bi_client = bi_client

        # 防止定时任务和手动触发同时运行，导致重复读取同一段游标。
        self._lock = asyncio.Lock()

    async def run_once(self, run_type: str = "scheduled") -> dict[str, Any]:
        """执行一次多数据流同步。

        run_type 用于区分 scheduled/manual，方便从 sync_logs 中排查触发来源。
        """
        async with self._lock:
            stream_results = []
            for stream_name in self.settings.sync_streams:
                for _ in range(self._max_batches_for_stream(stream_name)):
                    result = await self._run_stream_once(stream_name, run_type)
                    stream_results.append(result)
                    if result["status"] != "success" or result["item_count"] < self.settings.batch_size:
                        break

            failed_count = sum(1 for item in stream_results if item["status"] == "failed")
            queued_count = sum(1 for item in stream_results if item["status"] == "queued_failed")
            sent_count = sum(1 for item in stream_results if item["status"] == "success" and item["item_count"] > 0)
            item_count = sum(int(item.get("item_count", 0)) for item in stream_results)

            if failed_count:
                status = "partial_failed"
            elif queued_count:
                status = "queued_failed"
            else:
                status = "success"

            return {
                "status": status,
                "stream_count": len(self.settings.sync_streams),
                "batch_result_count": len(stream_results),
                "sent_batch_count": sent_count,
                "queued_failed_batch_count": queued_count,
                "failed_batch_count": failed_count,
                "item_count": item_count,
                "streams": stream_results,
            }

    def _max_batches_for_stream(self, stream_name: str) -> int:
        try:
            spec = self.zentao_client.get_stream_spec(stream_name)
        except Exception:
            return max(1, int(self.settings.max_batches_per_stream or 1))
        if spec.snapshot:
            return max(1, int(self.settings.snapshot_max_batches_per_stream or 1))
        return max(1, int(self.settings.max_batches_per_stream or 1))

    async def run_backfill(
        self,
        *,
        streams: list[str],
        initial_sync_start: str,
        max_batches_per_stream: int,
        reset_cursors: bool = True,
    ) -> dict[str, Any]:
        """执行一次手动回填。

        事件流按指定 initial_sync_start 重扫；快照流按主键从头分页重扫。
        bi_center 端按主键 upsert，因此重推已有行不会重复计分。
        """
        safe_streams = [stream.strip() for stream in streams if stream.strip()]
        if not safe_streams:
            safe_streams = list(self.settings.sync_streams)
        safe_max_batches = max(1, min(int(max_batches_per_stream or 1), 500))

        async with self._lock:
            stream_results = []
            if reset_cursors:
                for stream_name in safe_streams:
                    self.store.reset_cursor(stream_name)

            for stream_name in safe_streams:
                for _ in range(safe_max_batches):
                    result = await self._run_stream_once(
                        stream_name,
                        "backfill",
                        initial_sync_start=initial_sync_start,
                    )
                    stream_results.append(result)
                    if result["status"] != "success" or result["item_count"] < self.settings.batch_size:
                        break

            failed_count = sum(1 for item in stream_results if item["status"] == "failed")
            queued_count = sum(1 for item in stream_results if item["status"] == "queued_failed")
            sent_count = sum(1 for item in stream_results if item["status"] == "success" and item["item_count"] > 0)
            item_count = sum(int(item.get("item_count", 0)) for item in stream_results)
            if failed_count:
                status = "partial_failed"
            elif queued_count:
                status = "queued_failed"
            else:
                status = "success"
            return {
                "status": status,
                "stream_count": len(safe_streams),
                "batch_result_count": len(stream_results),
                "sent_batch_count": sent_count,
                "queued_failed_batch_count": queued_count,
                "failed_batch_count": failed_count,
                "item_count": item_count,
                "initial_sync_start": initial_sync_start,
                "reset_cursors": reset_cursors,
                "streams": stream_results,
            }

    async def _run_stream_once(
        self,
        stream_name: str,
        run_type: str,
        *,
        initial_sync_start: str | None = None,
    ) -> dict[str, Any]:
        """执行单个数据流的一批同步。"""
        try:
            spec = self.zentao_client.get_stream_spec(stream_name)
        except Exception as exc:
            error = str(exc)
            self.store.add_log(run_type, "failed", message=error, stream_name=stream_name)
            return {"stream": stream_name, "status": "failed", "item_count": 0, "error": error}

        last_id = self.store.get_cursor(stream_name)

        try:
            # PyMySQL 是同步驱动，用 asyncio.to_thread 放到线程里执行，避免阻塞事件循环。
            rows = await asyncio.to_thread(
                self.zentao_client.fetch_stream,
                stream_name,
                last_id,
                initial_sync_start or self.settings.initial_sync_start,
                self.settings.batch_size,
            )
        except Exception as exc:
            error = str(exc)
            logger.exception("Failed to fetch stream %s", stream_name)
            self.store.add_log(run_type, "failed", message=error, stream_name=stream_name)
            return {"stream": stream_name, "status": "failed", "item_count": 0, "error": error}

        if not rows:
            message = "No new rows"
            if spec.snapshot and last_id != 0:
                # 快照流扫到末尾后重置游标，下一轮重新从头快照，捕获原地更新。
                self.store.reset_cursor(stream_name)
                message = "Snapshot cycle completed; cursor reset"
            self.store.add_log(run_type, "success", message=message, stream_name=stream_name)
            return {"stream": stream_name, "status": "success", "item_count": 0, "message": message}

        min_id = int(rows[0][spec.cursor_field])
        max_id = int(rows[-1][spec.cursor_field])
        payload = self._build_payload(stream_name, spec.cursor_field, rows)

        try:
            await self.bi_client.send_batch(payload, spec.endpoint_path)

            # bi_center 已确认收到，推进对应数据流游标。
            self.store.advance_cursor(stream_name, max_id)
            self.store.add_log(
                run_type,
                "success",
                len(rows),
                min_id,
                max_id,
                "Batch sent",
                stream_name=stream_name,
            )
            return {
                "stream": stream_name,
                "status": "success",
                "item_count": len(rows),
                "min_id": min_id,
                "max_id": max_id,
                "endpoint_path": spec.endpoint_path,
            }
        except Exception as exc:
            error = str(exc)
            logger.exception("Failed to send batch %s for stream %s", payload["batch_id"], stream_name)

            # 发送失败时把完整批次写入 outbox，后续由 retry_failed 补发。
            self.store.save_outbox_batch(
                stream_name,
                spec.endpoint_path,
                payload["batch_id"],
                payload,
                min_id,
                max_id,
                len(rows),
                error,
            )

            # 当前批次已经落入 outbox，所以对应游标继续推进，避免后续重复扫描。
            self.store.advance_cursor(stream_name, max_id)
            self.store.add_log(
                run_type,
                "queued_failed",
                len(rows),
                min_id,
                max_id,
                error,
                stream_name=stream_name,
            )
            return {
                "stream": stream_name,
                "status": "queued_failed",
                "item_count": len(rows),
                "min_id": min_id,
                "max_id": max_id,
                "endpoint_path": spec.endpoint_path,
                "error": error,
            }

    async def retry_failed(self) -> dict[str, Any]:
        """补发失败队列中的批次。"""
        async with self._lock:
            sent = 0
            failed = 0
            batches = self.store.list_failed_batches(self.settings.max_failed_retry_batches)

            for batch in batches:
                batch_id = batch["batch_id"]
                payload = json.loads(batch["payload"])
                try:
                    await self.bi_client.send_batch(payload, batch["endpoint_path"])
                    self.store.mark_batch_sent(batch_id)
                    sent += 1
                except Exception as exc:
                    # 单个批次失败不影响后续批次继续重试。
                    logger.exception("Failed to retry batch %s", batch_id)
                    self.store.mark_batch_failed(batch_id, str(exc))
                    failed += 1

            self.store.add_log(
                "retry_failed",
                "success" if failed == 0 else "partial_failed",
                item_count=sent,
                message=f"sent={sent}, failed={failed}",
            )
            return {"status": "success" if failed == 0 else "partial_failed", "sent": sent, "failed": failed}

    def _build_payload(self, stream_name: str, cursor_field: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
        """构造发送给 bi_center 的标准批次格式。"""
        try:
            tz = ZoneInfo(self.settings.timezone)
        except ZoneInfoNotFoundError:
            # Windows 本地或极简容器可能没有系统 tzdata。当前业务默认中国时区，
            # 缺失时用 UTC+8 兜底，避免同步任务因 sent_at 生成失败而中断。
            tz = timezone(timedelta(hours=8)) if self.settings.timezone == "Asia/Shanghai" else timezone.utc
        return {
            "source": "zentao",
            "database": self.settings.zentao_db_name,
            "stream": stream_name,
            "cursor_field": cursor_field,
            "batch_id": str(uuid4()),
            "sent_at": datetime.now(tz).isoformat(),
            "items": rows,
        }

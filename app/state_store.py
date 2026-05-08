# -*- coding: utf-8 -*-
"""本地状态存储模块。

本服务不是最终数据仓库，但仍需要一个可靠的本地小数据库记录同步状态：

1. sync_state：兼容旧版本，记录 actions 流当前已经扫描到的 zt_action 最大 ID。
2. sync_cursors：记录每个数据流自己的游标。
3. outbox_batches：记录发送失败的批次，等待后台重试。
4. sync_logs：记录每次同步/重试结果，方便排查问题。

这里使用 SQLite，是为了让 Docker Compose 部署足够轻量。只要把 /app/data
挂载出来，容器重建后游标和失败队列也不会丢。
"""

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator, Optional


def utc_now_iso() -> str:
    """返回 UTC ISO 时间字符串。

    本地状态统一保存 UTC 时间，避免服务部署机器时区变化导致日志混乱。
    推送给 bi_center 的 sent_at 会在同步服务中使用业务时区。
    """
    return datetime.now(timezone.utc).isoformat()


class StateStore:
    """SQLite 状态仓库。

    这个类封装所有 SQLite 读写，业务层不直接拼 SQL 操作状态表。
    """

    def __init__(self, sqlite_path: str, initial_sync_start: str) -> None:
        self.sqlite_path = sqlite_path
        self.initial_sync_start = initial_sync_start

        # Docker 中通常是 /app/data/xxx.sqlite3；本地目录不存在时自动创建。
        parent = os.path.dirname(sqlite_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self.init_db()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        """打开 SQLite 连接并自动提交。

        每次操作使用短连接，逻辑简单，也能避免长时间持有锁。
        row_factory 让查询结果可以通过字段名访问。
        """
        conn = sqlite3.connect(self.sqlite_path, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def init_db(self) -> None:
        """初始化本地状态表。

        CREATE TABLE IF NOT EXISTS 让服务可以重复启动；INSERT OR IGNORE 确保
        sync_state 只有一行全局状态。
        """
        with self.connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sync_state (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    last_action_id INTEGER NOT NULL DEFAULT 0,
                    initial_sync_start TEXT NOT NULL,
                    last_success_at TEXT,
                    updated_at TEXT NOT NULL
                )
                """
            )

            # 多数据流游标表。actions 使用 zt_action.id，aiscore_results 使用
            # zt_aiscore_result.id，快照表也使用各自 id 分页循环。
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sync_cursors (
                    stream_name TEXT PRIMARY KEY,
                    last_id INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                )
                """
            )

            # 失败队列表保存完整 payload。bi_center 临时不可用时，服务不丢批次。
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS outbox_batches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_id TEXT NOT NULL UNIQUE,
                    stream_name TEXT NOT NULL DEFAULT 'actions',
                    endpoint_path TEXT NOT NULL DEFAULT '/api/zentao/actions/batch',
                    payload TEXT NOT NULL,
                    min_action_id INTEGER NOT NULL,
                    max_action_id INTEGER NOT NULL,
                    item_count INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    retry_count INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            self._ensure_column(
                conn,
                "outbox_batches",
                "stream_name",
                "stream_name TEXT NOT NULL DEFAULT 'actions'",
            )
            self._ensure_column(
                conn,
                "outbox_batches",
                "endpoint_path",
                "endpoint_path TEXT NOT NULL DEFAULT '/api/zentao/actions/batch'",
            )

            # 同步日志只保存摘要，不保存大量明细，避免 SQLite 无限膨胀太快。
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sync_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    stream_name TEXT,
                    run_type TEXT NOT NULL,
                    status TEXT NOT NULL,
                    item_count INTEGER NOT NULL DEFAULT 0,
                    min_action_id INTEGER,
                    max_action_id INTEGER,
                    message TEXT,
                    created_at TEXT NOT NULL
                )
                """
            )
            self._ensure_column(conn, "sync_logs", "stream_name", "stream_name TEXT")
            conn.execute(
                """
                INSERT OR IGNORE INTO sync_state (
                    id, last_action_id, initial_sync_start, updated_at
                ) VALUES (1, 0, ?, ?)
                """,
                (self.initial_sync_start, utc_now_iso()),
            )

            # 从旧版 sync_state 平滑迁移 actions 游标；新安装时默认从 0 开始。
            legacy_state = conn.execute("SELECT last_action_id FROM sync_state WHERE id = 1").fetchone()
            legacy_action_id = int(legacy_state["last_action_id"]) if legacy_state else 0
            conn.execute(
                """
                INSERT OR IGNORE INTO sync_cursors (stream_name, last_id, updated_at)
                VALUES ('actions', ?, ?)
                """,
                (legacy_action_id, utc_now_iso()),
            )

    def _ensure_column(self, conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
        """为旧 SQLite 库补列。

        SQLite 没有通用的 ADD COLUMN IF NOT EXISTS，所以先读 PRAGMA table_info。
        table 和 ddl 均来自代码常量，不接收外部输入。
        """
        columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")

    def get_status(self) -> dict[str, Any]:
        """返回管理接口需要展示的同步状态。"""
        with self.connect() as conn:
            state = conn.execute("SELECT * FROM sync_state WHERE id = 1").fetchone()
            cursors = conn.execute("SELECT * FROM sync_cursors ORDER BY stream_name").fetchall()
            pending = conn.execute(
                "SELECT COUNT(*) AS count FROM outbox_batches WHERE status IN ('pending', 'failed')"
            ).fetchone()
            pending_by_stream = conn.execute(
                """
                SELECT stream_name, COUNT(*) AS count
                FROM outbox_batches
                WHERE status IN ('pending', 'failed')
                GROUP BY stream_name
                ORDER BY stream_name
                """
            ).fetchall()
            last_log = conn.execute(
                "SELECT * FROM sync_logs ORDER BY id DESC LIMIT 1"
            ).fetchone()
            return {
                "last_action_id": state["last_action_id"],
                "initial_sync_start": state["initial_sync_start"],
                "last_success_at": state["last_success_at"],
                "updated_at": state["updated_at"],
                "cursors": [dict(row) for row in cursors],
                "failed_or_pending_batches": pending["count"],
                "failed_or_pending_batches_by_stream": [dict(row) for row in pending_by_stream],
                "last_log": dict(last_log) if last_log else None,
            }

    def get_last_action_id(self) -> int:
        """读取当前同步游标。"""
        return self.get_cursor("actions")

    def get_cursor(self, stream_name: str) -> int:
        """读取指定数据流游标；不存在时自动初始化为 0。"""
        with self.connect() as conn:
            row = conn.execute(
                "SELECT last_id FROM sync_cursors WHERE stream_name = ?",
                (stream_name,),
            ).fetchone()
            if row:
                return int(row["last_id"])

            conn.execute(
                "INSERT INTO sync_cursors (stream_name, last_id, updated_at) VALUES (?, 0, ?)",
                (stream_name, utc_now_iso()),
            )
            return 0

    def advance_cursor(self, stream_name: str, last_id: int) -> None:
        """推进指定数据流游标。

        注意：当前设计在发送失败并落入 outbox 后也会推进游标。这样可以避免
        每次定时任务重复扫描同一批数据，失败数据由 outbox 单独负责重试。
        """
        now = utc_now_iso()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO sync_cursors (stream_name, last_id, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(stream_name) DO UPDATE SET
                    last_id = excluded.last_id,
                    updated_at = excluded.updated_at
                """,
                (stream_name, last_id, now),
            )
            if stream_name == "actions":
                conn.execute(
                    """
                    UPDATE sync_state
                    SET last_action_id = ?, last_success_at = ?, updated_at = ?
                    WHERE id = 1
                    """,
                    (last_id, now, now),
                )

    def reset_cursor(self, stream_name: str) -> None:
        """把快照流游标重置为 0，下一轮从头重新快照。"""
        self.advance_cursor(stream_name, 0)

    def save_outbox_batch(
        self,
        stream_name: str,
        endpoint_path: str,
        batch_id: str,
        payload: dict[str, Any],
        min_action_id: int,
        max_action_id: int,
        item_count: int,
        error: str,
    ) -> None:
        """保存发送失败的批次。

        batch_id 做唯一键；如果同一个批次因为异常流程重复保存，INSERT OR IGNORE
        会避免产生重复失败记录。
        """
        now = utc_now_iso()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO outbox_batches (
                    batch_id, stream_name, endpoint_path, payload,
                    min_action_id, max_action_id, item_count,
                    status, retry_count, last_error, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'failed', 0, ?, ?, ?)
                """,
                (
                    batch_id,
                    stream_name,
                    endpoint_path,
                    json.dumps(payload, ensure_ascii=False),
                    min_action_id,
                    max_action_id,
                    item_count,
                    error,
                    now,
                    now,
                ),
            )

    def list_failed_batches(self, limit: int) -> list[sqlite3.Row]:
        """按创建顺序取出待重试批次。"""
        with self.connect() as conn:
            return list(
                conn.execute(
                    """
                    SELECT * FROM outbox_batches
                    WHERE status IN ('pending', 'failed')
                    ORDER BY id ASC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
            )

    def mark_batch_sent(self, batch_id: str) -> None:
        """标记失败批次已经补发成功。"""
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE outbox_batches
                SET status = 'sent', updated_at = ?
                WHERE batch_id = ?
                """,
                (utc_now_iso(), batch_id),
            )

    def mark_batch_failed(self, batch_id: str, error: str) -> None:
        """记录一次失败重试结果，并累计重试次数。"""
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE outbox_batches
                SET status = 'failed',
                    retry_count = retry_count + 1,
                    last_error = ?,
                    updated_at = ?
                WHERE batch_id = ?
                """,
                (error, utc_now_iso(), batch_id),
            )

    def add_log(
        self,
        run_type: str,
        status: str,
        item_count: int = 0,
        min_action_id: Optional[int] = None,
        max_action_id: Optional[int] = None,
        message: Optional[str] = None,
        stream_name: Optional[str] = None,
    ) -> None:
        """写入同步日志摘要。"""
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO sync_logs (
                    stream_name, run_type, status, item_count,
                    min_action_id, max_action_id, message, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    stream_name,
                    run_type,
                    status,
                    item_count,
                    min_action_id,
                    max_action_id,
                    message,
                    utc_now_iso(),
                ),
            )

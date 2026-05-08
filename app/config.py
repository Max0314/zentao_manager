# -*- coding: utf-8 -*-
"""服务配置模块。

本文件只负责从环境变量中读取配置，并整理成不可变的 Settings 对象。
所有部署差异都应该放到 .env / docker-compose 环境变量中，不要写死在业务代码里。
"""

import os
from dataclasses import dataclass


def _get_int(name: str, default: int) -> int:
    """读取整数类型环境变量。

    环境变量不存在或为空字符串时使用默认值；如果配置了非法数字，会直接抛出
    ValueError，让容器启动失败，避免服务带着错误配置静默运行。
    """
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return int(value)


@dataclass(frozen=True)
class Settings:
    """应用运行所需的全部配置。

    frozen=True 表示配置对象创建后不可修改，避免运行中被误改，保证定时任务、
    手动同步接口、失败重试逻辑使用的是同一套配置。
    """

    # 禅道 MariaDB 连接信息。生产环境请使用只读账号，避免使用 root。
    zentao_db_host: str
    zentao_db_port: int
    zentao_db_name: str
    zentao_db_user: str
    zentao_db_password: str

    # bi_center 接收端地址。BI_CENTER_TOKEN 是可选项，当前可只依赖 IP 白名单。
    bi_center_url: str
    bi_center_token: str
    manager_api_token: str

    # 增量同步控制项。首次启动从 initial_sync_start 开始，之后按 last_action_id 游标推进。
    initial_sync_start: str
    sync_interval_minutes: int
    batch_size: int
    max_batches_per_stream: int
    sync_streams: tuple[str, ...]

    # 失败队列重试控制项，防止 bi_center 临时不可用时丢数据。
    failed_retry_interval_minutes: int
    max_failed_retry_batches: int
    request_timeout_seconds: int

    # SQLite 持久化路径和时区。SQLite 文件需要挂载到 Docker volume 中。
    sqlite_path: str
    timezone: str

    @property
    def bi_batch_url(self) -> str:
        """拼出 bi_center 批量接收接口地址。"""
        return self.bi_center_url.rstrip("/") + "/api/zentao/actions/batch"


def load_settings() -> Settings:
    """从环境变量构建 Settings。

    默认值只用于本地开发和模板兜底；真实部署时应通过 .env 明确配置。
    """
    sync_streams = tuple(
        stream.strip()
        for stream in os.getenv(
            "SYNC_STREAMS",
            (
                "actions,aiscore_results,tasks,bugs,stories,users,depts,"
                "aiscore_rules,ai_prompts,pivot_configs,cases,docs"
            ),
        ).split(",")
        if stream.strip()
    )

    return Settings(
        zentao_db_host=os.getenv("ZENTAO_DB_HOST", "10.70.33.18"),
        zentao_db_port=_get_int("ZENTAO_DB_PORT", 3380),
        zentao_db_name=os.getenv("ZENTAO_DB_NAME", "zentaoep"),
        zentao_db_user=os.getenv("ZENTAO_DB_USER", "bi_reader"),
        zentao_db_password=os.getenv("ZENTAO_DB_PASSWORD", ""),
        bi_center_url=os.getenv("BI_CENTER_URL", ""),
        bi_center_token=os.getenv("BI_CENTER_TOKEN", ""),
        manager_api_token=os.getenv("MANAGER_API_TOKEN", ""),
        initial_sync_start=os.getenv("INITIAL_SYNC_START", "2026-05-01 00:00:00"),
        sync_interval_minutes=_get_int("SYNC_INTERVAL_MINUTES", 60),
        batch_size=_get_int("BATCH_SIZE", 1000),
        max_batches_per_stream=_get_int("MAX_BATCHES_PER_STREAM", 1),
        sync_streams=sync_streams,
        failed_retry_interval_minutes=_get_int("FAILED_RETRY_INTERVAL_MINUTES", 10),
        max_failed_retry_batches=_get_int("MAX_FAILED_RETRY_BATCHES", 20),
        request_timeout_seconds=_get_int("REQUEST_TIMEOUT_SECONDS", 30),
        sqlite_path=os.getenv("SQLITE_PATH", "/app/data/zentao_manager.sqlite3"),
        timezone=os.getenv("TZ", "Asia/Shanghai"),
    )

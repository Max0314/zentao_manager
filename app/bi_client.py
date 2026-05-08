# -*- coding: utf-8 -*-
"""bi_center HTTP 客户端。

这个模块负责把同步批次 POST 到 bi_center。只要 bi_center 返回非 2xx，
调用方就会把该批次写入失败队列，等待后续重试。
"""

from typing import Any

import httpx

from app.config import Settings


class BiCenterClient:
    """bi_center 推送客户端。"""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def send_batch(self, payload: dict[str, Any], endpoint_path: str | None = None) -> None:
        """发送一个批次到 bi_center。

        当前主要依赖 bi_center 对调用方出口 IP 做白名单；如果配置了
        BI_CENTER_TOKEN，则额外带上 Bearer Token，方便后续升级鉴权。
        """
        if not self.settings.bi_center_url:
            raise RuntimeError("BI_CENTER_URL is empty")

        headers = {"Content-Type": "application/json"}
        if self.settings.bi_center_token:
            headers["Authorization"] = f"Bearer {self.settings.bi_center_token}"

        # httpx.AsyncClient 用于异步请求，避免阻塞 FastAPI 事件循环。
        url = self.settings.bi_center_url.rstrip("/") + (endpoint_path or "/api/zentao/actions/batch")
        async with httpx.AsyncClient(timeout=self.settings.request_timeout_seconds) as client:
            response = await client.post(
                url,
                json=payload,
                headers=headers,
            )
            # 非 2xx 会抛出异常，交给同步服务记录失败队列。
            response.raise_for_status()

# Architecture

## 数据流

```text
Zentao MariaDB -> zentao_client -> sync_service -> bi_client -> bi_center
```

## 关键模块

- `app/main.py`：服务入口和调度。
- `app/config.py`：环境变量配置。
- `app/zentao_client.py`：禅道数据库读取。
- `app/bi_client.py`：推送 BI 接口。
- `app/state_store.py`：同步游标和失败队列。
- `app/sync_service.py`：同步编排。

## 部署

Docker 配置以 `Dockerfile` 和 `docker-compose.yml` 为入口。真实 `.env` 不进入 Git。

# zentao_manager

`zentao_manager` 是一个禅道数据中转服务。它定时从禅道 MariaDB 拉取操作流水、AI 评分结果、任务/Bug/需求维度、用户部门、评分规则和月度配置后，主动推送到 `bi_center`。

## 目录结构

```text
app/
  main.py           FastAPI 入口和定时任务
  config.py         环境变量配置
  zentao_client.py  禅道数据库多数据流查询
  bi_client.py      bi_center 推送
  state_store.py    SQLite 游标、失败队列、同步日志
  sync_service.py   同步编排逻辑
scripts/
  db_probe.py                       数据库只读探测脚本
  create_bi_reader.sql              三张核心表只读授权模板
  grant_bi_reader_all_tables.sql    zentaoep 全库只读授权模板
Dockerfile
docker-compose.yml
.env.example
BI_CENTER_INTEGRATION.md
requirements.txt
```

## 初始化配置

复制环境变量模板：

```bash
cp .env.example .env
```

测试服务器也可以使用：

```bash
cp .env.test.example .env
```

正式服务器可以使用：

```bash
cp .env.prod.example .env
```

编辑 `.env`：

```env
ZENTAO_DB_HOST=10.70.33.18
ZENTAO_DB_PORT=3380
ZENTAO_DB_NAME=zentaoep
ZENTAO_DB_USER=bi_reader
ZENTAO_DB_PASSWORD=你的只读账号密码

BI_CENTER_URL=https://neoflow.neo-net.com/bi_center
BI_CENTER_TOKEN=
MANAGER_API_TOKEN=ZENTAO

INITIAL_SYNC_START=2026-03-01 00:00:00
SYNC_INTERVAL_MINUTES=60
BATCH_SIZE=1000
MAX_BATCHES_PER_STREAM=1
SNAPSHOT_MAX_BATCHES_PER_STREAM=1
SYNC_STREAMS=actions,aiscore_results,tasks,bugs,stories,users,depts,aiscore_rules,ai_prompts,pivot_configs,cases,docs
```

`SYNC_STREAMS` 可以按需删减。默认同步的数据流如下：

| 数据流 | 来源表 | 推送接口 | 说明 |
|---|---|---|---|
| `actions` | `zt_action` | `/api/zentao/actions/batch` | 操作流水 |
| `aiscore_results` | `zt_aiscore_result` | `/api/zentao/aiscore-results/batch` | AI 分数核心表 |
| `tasks` | `zt_task` | `/api/zentao/tasks/batch` | 任务维度 |
| `bugs` | `zt_bug` | `/api/zentao/bugs/batch` | Bug 维度 |
| `stories` | `zt_story` | `/api/zentao/stories/batch` | 需求维度 |
| `users` | `zt_user` | `/api/zentao/users/batch` | 用户维度 |
| `depts` | `zt_dept` | `/api/zentao/depts/batch` | 部门维度 |
| `aiscore_rules` | `zt_aiscore_rules` | `/api/zentao/aiscore-rules/batch` | 评分规则 |
| `ai_prompts` | `zt_ai_prompt` | `/api/zentao/ai-prompts/batch` | AI 提词和触发动作 |
| `pivot_configs` | `zt_config` | `/api/zentao/pivot-configs/batch` | 月度折算和等级配置 |
| `cases` | `zt_case` | `/api/zentao/cases/batch` | 用例创建分维度 |
| `docs` | `zt_doc` | `/api/zentao/docs/batch` | 原创文档创建分维度 |

`MAX_BATCHES_PER_STREAM` 控制事件流每轮最多同步几个批次。`SNAPSHOT_MAX_BATCHES_PER_STREAM` 控制快照流每轮最多同步几个批次；任务、Bug、用例、文档等快照落后时，可以临时调到 `20` 追数，稳定后再调回 `5`、`10` 或 `1`，减少对禅道库和 `bi_center` 的压力。

## 启动

```bash
docker compose up -d --build
```

查看日志：

```bash
docker compose logs -f
```

默认 `docker-compose.yml` 只把管理端口绑定到宿主机 `127.0.0.1:7891`。本服务会主动推送到 `bi_center`，不需要对公网开放管理端口。需要远程管理时，建议使用 SSH 登录服务器后执行 `curl`，或通过 SSH 隧道访问。

## 管理接口

- `GET /health`
- `GET /sync/status`
- `POST /sync/run`
- `POST /sync/backfill`
- `POST /sync/retry-failed`

本机测试：

```bash
curl http://127.0.0.1:7891/health
curl -H "Authorization: Bearer $MANAGER_API_TOKEN" http://127.0.0.1:7891/sync/status
curl -H "Authorization: Bearer $MANAGER_API_TOKEN" -X POST http://127.0.0.1:7891/sync/run
```

手动回填示例。适用于首次上线时 `INITIAL_SYNC_START` 设得过晚，或快照流还没有扫到近期 `zt_task` / `zt_bug` 主键的情况。`bi_center` 端按主键 upsert，重复推送不会重复计分。

```bash
curl -H "Authorization: Bearer $MANAGER_API_TOKEN" -X POST \
  "http://127.0.0.1:7891/sync/backfill?initial_sync_start=2026-03-01%2000:00:00&streams=actions,aiscore_results,tasks,bugs&max_batches_per_stream=120&reset_cursors=true"
```

## bi_center 对接

对接说明见：

```text
BI_CENTER_INTEGRATION.md
```

中转服务会调用：

```text
POST {BI_CENTER_URL}/api/zentao/<stream>/batch
```

`bi_center` 需要按每个流的主键做幂等入库，避免失败重试产生重复数据。例如 `actions` 用 `action_id`，`aiscore_results` 用 `score_result_id`，`tasks` 用 `task_id`。

## 数据库权限建议

不要使用 root 账号。当前多表同步需要读取 `zentaoep` 中多张表，建议直接授予 `zentaoep.*` 只读权限：

```sql
GRANT SELECT ON zentaoep.* TO 'bi_reader'@'内网服务器IP';
FLUSH PRIVILEGES;
```

如果只运行旧版操作流水同步，最小权限只需要三张表：

```sql
CREATE USER IF NOT EXISTS 'bi_reader'@'内网服务器IP' IDENTIFIED BY '强密码';

GRANT SELECT ON zentaoep.zt_action TO 'bi_reader'@'内网服务器IP';
GRANT SELECT ON zentaoep.zt_user TO 'bi_reader'@'内网服务器IP';
GRANT SELECT ON zentaoep.zt_dept TO 'bi_reader'@'内网服务器IP';

FLUSH PRIVILEGES;
```

如果后续希望 `bi_reader` 读取 `zentaoep` 库下所有表，可以使用：

```sql
GRANT SELECT ON zentaoep.* TO 'bi_reader'@'内网服务器IP';
FLUSH PRIVILEGES;
```

模板文件：

```text
scripts/create_bi_reader.sql
scripts/grant_bi_reader_all_tables.sql
```

注意：这里建议授权 `zentaoep.*`，不是 `*.*`。这样可以读取禅道库全部表，但不会读取 MariaDB 里的其他数据库。

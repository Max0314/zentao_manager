# bi_center 接入禅道数据中转服务说明

本文档说明 `bi_center` 需要如何配合 `zentao_manager`，完成禅道多数据流接收、入库、去重和后续积分计算。

## 1. 数据流

`zentao_manager` 部署在能访问禅道数据库的内网机器上，定时读取禅道 MariaDB 中的多个数据流，然后主动推送到 `bi_center`。

```text
禅道 MariaDB
  -> zentao_manager 每小时读取多数据流
  -> POST 到 bi_center 接收接口
  -> bi_center 幂等入库
  -> bi_center 自行计算月份、积分、报表
```

由于 `bi_center` 无法访问内网禅道服务器，所以必须由 `zentao_manager` 主动向 `bi_center` 发起 HTTP 请求。

## 2. bi_center 需要提供的接口

当前服务会按数据流调用不同接口。`bi_center` 建议全部提供：

| 数据流 | 接口 | 幂等主键 | 来源表 |
|---|---|---|---|
| `actions` | `POST /api/zentao/actions/batch` | `action_id` | `zt_action` |
| `aiscore_results` | `POST /api/zentao/aiscore-results/batch` | `score_result_id` | `zt_aiscore_result` |
| `tasks` | `POST /api/zentao/tasks/batch` | `task_id` | `zt_task` |
| `bugs` | `POST /api/zentao/bugs/batch` | `bug_id` | `zt_bug` |
| `stories` | `POST /api/zentao/stories/batch` | `story_id` | `zt_story` |
| `users` | `POST /api/zentao/users/batch` | `user_id` | `zt_user` |
| `depts` | `POST /api/zentao/depts/batch` | `dept_id` | `zt_dept` |
| `aiscore_rules` | `POST /api/zentao/aiscore-rules/batch` | `rule_id` | `zt_aiscore_rules` |
| `ai_prompts` | `POST /api/zentao/ai-prompts/batch` | `prompt_id` | `zt_ai_prompt` |
| `pivot_configs` | `POST /api/zentao/pivot-configs/batch` | `config_id` | `zt_config` |
| `cases` | `POST /api/zentao/cases/batch` | `case_id` | `zt_case` |
| `docs` | `POST /api/zentao/docs/batch` | `doc_id` | `zt_doc` |

旧版操作流水接口仍然是：

```text
POST /api/zentao/actions/batch
Content-Type: application/json
```

如果 `bi_center` 使用完整域名，例如：

```text
https://bi.example.com
```

那么 `zentao_manager` 中的配置应为：

```env
BI_CENTER_URL=https://bi.example.com
```

服务会按流自动请求，例如：

```text
https://bi.example.com/api/zentao/actions/batch
https://bi.example.com/api/zentao/aiscore-results/batch
https://bi.example.com/api/zentao/tasks/batch
```

## 3. 请求体格式

所有数据流的外层 JSON 结构一致，区别在于 `stream`、`cursor_field` 和 `items` 字段内容。

`actions` 示例：

```json
{
  "source": "zentao",
  "database": "zentaoep",
  "stream": "actions",
  "cursor_field": "action_id",
  "batch_id": "3f7cbb73-26df-4c88-a8d4-64b095d0e1da",
  "sent_at": "2026-05-07T18:00:00+08:00",
  "items": [
    {
      "action_id": 1621515,
      "actor": "chenpenglie",
      "action": "opened",
      "object_type": "task",
      "object_id": 10001,
      "action_date": "2026-05-07T10:15:00",
      "extra": "",
      "dept_id": 58,
      "dept_name": "研发部",
      "user_deleted": "0"
    }
  ]
}
```

`aiscore_results` 示例：

```json
{
  "source": "zentao",
  "database": "zentaoep",
  "stream": "aiscore_results",
  "cursor_field": "score_result_id",
  "batch_id": "f79d16f4-113f-4f5e-a82d-37d570d02088",
  "sent_at": "2026-05-08T16:00:00+08:00",
  "items": [
    {
      "score_result_id": 179502,
      "object_type": "task",
      "object_id": 81799,
      "action": "finished",
      "action_id": 0,
      "field": "",
      "score": "25",
      "suggestions": "任务名称较为宽泛...",
      "score_times": 3,
      "create_by": "jiangxiaoqing",
      "create_date": "2026-05-08T15:04:32",
      "realname": "姓名",
      "dept_id": 12,
      "dept_name": "应用开发组",
      "user_deleted": "0"
    }
  ]
}
```

字段说明：

| 字段 | 含义 |
|---|---|
| `source` | 数据来源，固定为 `zentao` |
| `database` | 禅道数据库名，例如 `zentaoep` |
| `stream` | 数据流名称，例如 `actions`、`aiscore_results` |
| `cursor_field` | 本数据流的游标字段名 |
| `batch_id` | 批次唯一 ID，用于日志追踪 |
| `sent_at` | 中转服务发送时间 |
| `items` | 本批次的禅道操作明细 |
| `items[].action_id` | `zt_action.id`，全局去重主键 |
| `items[].actor` | 操作人账号，对应 `zt_action.actor` |
| `items[].action` | 操作类型，例如 `opened`、`finished`、`resolved` |
| `items[].object_type` | 对象类型，例如 `bug`、`task`、`story`、`case` |
| `items[].object_id` | 对象 ID |
| `items[].action_date` | 禅道操作发生时间 |
| `items[].extra` | 禅道 action 的附加字段 |
| `items[].dept_id` | 用户部门 ID，来自 `zt_user.dept` |
| `items[].dept_name` | 用户部门名，来自 `zt_dept.name` |
| `items[].user_deleted` | 用户是否删除，来自 `zt_user.deleted` |

当前中转服务不会发送 `zt_action.comment`，避免备注文本中包含敏感信息。

## 4. bi_center 入库要求

`bi_center` 必须按各数据流主键做幂等处理。

操作流水推荐建表思路：

```sql
CREATE TABLE zentao_action_event (
  action_id BIGINT PRIMARY KEY,
  actor VARCHAR(100) NOT NULL,
  action VARCHAR(100) NOT NULL,
  object_type VARCHAR(100) NOT NULL,
  object_id BIGINT NOT NULL,
  action_date DATETIME NOT NULL,
  extra VARCHAR(255),
  dept_id BIGINT,
  dept_name VARCHAR(255),
  user_deleted VARCHAR(10),
  source VARCHAR(50) NOT NULL,
  source_database VARCHAR(100) NOT NULL,
  batch_id VARCHAR(64) NOT NULL,
  received_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);
```

AI 评分结果推荐建表思路：

```sql
CREATE TABLE zentao_ai_score_result (
  score_result_id BIGINT PRIMARY KEY,
  object_type VARCHAR(50) NOT NULL,
  object_id BIGINT NOT NULL,
  action VARCHAR(100) NOT NULL,
  action_id BIGINT NOT NULL,
  field VARCHAR(100) NOT NULL,
  score VARCHAR(50) NOT NULL,
  suggestions TEXT,
  score_times INT NOT NULL,
  create_by VARCHAR(100) NOT NULL,
  create_date DATETIME NOT NULL,
  realname VARCHAR(255),
  dept_id BIGINT,
  dept_name VARCHAR(255),
  user_deleted VARCHAR(10),
  source VARCHAR(50) NOT NULL,
  source_database VARCHAR(100) NOT NULL,
  batch_id VARCHAR(64) NOT NULL,
  received_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);
```

如果 `bi_center` 使用 MySQL/MariaDB，可以使用 `INSERT ... ON DUPLICATE KEY UPDATE`；如果使用 PostgreSQL，可以使用 `ON CONFLICT (<本流主键>) DO UPDATE` 或 `DO NOTHING`。

幂等很重要：当网络异常或 `bi_center` 返回非 2xx 时，`zentao_manager` 会把批次写入失败队列并重试，重试可能导致同一主键再次发送。

## 5. 响应约定

成功时返回任意 2xx 状态码即可，例如：

```json
{
  "success": true,
  "inserted": 1000,
  "duplicated": 0
}
```

失败时返回非 2xx 状态码，例如 `400`、`401`、`500`。`zentao_manager` 会把这次批次视为失败并进入重试队列。

## 6. 鉴权与网络

当前方案优先使用 IP 白名单：

1. `bi_center` 允许 `zentao_manager` 所在服务器的出口 IP 访问接收接口。
2. 其他来源 IP 直接拒绝。
3. 如果后续需要更强鉴权，可以在 `zentao_manager` 配置 `BI_CENTER_TOKEN`，服务会发送：

```text
Authorization: Bearer <BI_CENTER_TOKEN>
```

`bi_center` 可以在保留 IP 白名单的基础上校验 Token。

`zentao_manager` 自己的管理接口也建议配置 `MANAGER_API_TOKEN`。配置后，`/sync/status`、`/sync/run`、`/sync/retry-failed` 都需要：

```text
Authorization: Bearer <MANAGER_API_TOKEN>
```

默认 Docker Compose 只把管理端口绑定到 `127.0.0.1:8000`，因为 `bi_center` 不需要反向访问 `zentao_manager`。

## 7. 积分计算建议

`zentao_manager` 只负责同步禅道数据，不负责最终积分计算。`bi_center` 可按以下两种口径计算：

1. AI 原始实际分：来自 `aiscore_results`，只累加 `field=''` 和 `field='comment'`。
2. 禅道月度折算分：结合 `aiscore_results`、`tasks`、`bugs`、`stories`、`cases`、`docs`、`pivot_configs` 复现禅道月报公式。

如果只做旧版操作行为分，可以继续使用 `actions`：

| 行为 | 建议分值 |
|---|---:|
| 修复 Bug，`object_type=bug`、`action=resolved`、`extra=fixed` | 5 |
| 其他有效行为，例如创建任务、完成任务、创建 Bug、创建需求、创建用例 | 2 |

操作流水月份归属建议使用 `action_date`；AI 评分月份归属建议使用 `create_date`。不要使用 `received_at`，避免历史补数时归错月份。

## 8. 联调步骤

1. `bi_center` 先提供测试地址，例如 `https://bi.example.com/api/zentao/actions/batch`。
2. 将 `zentao_manager` 的 `BI_CENTER_URL` 配成 `https://bi.example.com`。
3. 在 `zentao_manager` 调用：

```bash
curl -H "Authorization: Bearer $MANAGER_API_TOKEN" -X POST http://127.0.0.1:8000/sync/run
```

4. 查看 `bi_center` 是否收到数据。
5. 查看 `zentao_manager` 状态：

```bash
curl -H "Authorization: Bearer $MANAGER_API_TOKEN" http://127.0.0.1:8000/sync/status
```

6. 如果失败，调用：

```bash
curl -H "Authorization: Bearer $MANAGER_API_TOKEN" -X POST http://127.0.0.1:8000/sync/retry-failed
```

## 9. 注意事项

- `bi_center` 不要依赖批次顺序做唯一判断，应以当前数据流的主键为准。
- 单批默认最多 `1000` 条，可通过 `BATCH_SIZE` 调整。
- 如果要继续同步更多禅道表，需要双方重新约定接口字段。
- 如果 `bi_center` 入库很慢，应先落本地队列再异步处理，避免 HTTP 请求超时。
- 接收端应记录 `batch_id`、`source`、`received_at`，方便排查链路问题。

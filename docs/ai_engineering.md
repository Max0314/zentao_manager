# AI 工程化说明

`zentao_manager` 负责把禅道数据同步到 `bi_center`。AI Agent 修改时要优先判断是否影响数据流、字段契约、同步游标或失败重试。

## 默认流程

1. 阅读 `AGENTS.md` 和当前任务文件。
2. 确认涉及的数据流和推送接口。
3. 修改最小范围代码。
4. 运行 `make compile` 和相关测试。
5. 说明是否需要同步 `bi_center`。

## 高风险区域

- `app/config.py`
- `app/zentao_client.py`
- `app/bi_client.py`
- `app/sync_service.py`
- `app/state_store.py`

数据库连接、token 和人员数据按敏感信息处理。

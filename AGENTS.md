# AGENTS.md

本文件是 `zentao_manager` 的 AI 工程化约束。Codex 或其他 AI Agent 修改本项目时，应优先读取本文件、`README.md`、`docs/workflow.md`、`docs/architecture.md`、`docs/coding-style.md` 和对应的 `tasks/*.md`。

## 项目边界

- 本项目是禅道数据中转服务，GitHub 主分支为 `main`。
- 它定时读取禅道数据库并推送到 `bi_center`，不直接提供 BI 页面。
- 不修改 `bi_center`、`SOP`、`ai_code_review` 或 `motion_analysis`，除非任务明确要求跨项目同步。
- `.env`、数据库密码、API token、同步游标、失败队列、运行日志和人员/工号 CSV 不进入 Git。

## 技术栈

- Python 后端。
- 入口：`app/main.py`。
- 配置：`app/config.py`。
- 禅道读取：`app/zentao_client.py`。
- BI 推送：`app/bi_client.py`。
- 同步编排：`app/sync_service.py`。
- 部署：`Dockerfile`、`docker-compose.yml`。

## 编码规则

- 查询禅道数据库时保持只读原则。
- 推送 `bi_center` 的接口字段要保持兼容。
- 同步状态、失败队列和重试逻辑不能丢数据。
- 日志不得输出数据库密码、token 或人员敏感数据。
- 人员/工号 CSV 仅作本地排查资料，默认不提交。

## 验证规则

默认验证入口：

```bash
make check
```

涉及同步逻辑时运行 `make test` 和 `make compile`。

## Git 规则

- 主分支为 `main`。
- 一个任务一个分支，推荐格式：`feature/task-xxx-short-name` 或 `fix/task-xxx-short-name`。
- 提交前确认 `.env`、运行状态、日志、数据库文件和 CSV 没有进入暂存区。

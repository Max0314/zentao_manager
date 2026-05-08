# -*- coding: utf-8 -*-
"""禅道数据库访问模块。

本模块只做只读查询，不执行 INSERT/UPDATE/DELETE/DDL。当前同步范围包含
操作流水、AI 评分结果、任务/Bug/需求/用户/部门/评分规则/Prompt/配置等
BI 统计所需数据。
"""

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import pymysql
from pymysql.cursors import DictCursor

from app.config import Settings


@dataclass(frozen=True)
class StreamSpec:
    """一个禅道同步数据流的查询定义。

    name 是内部流名称；endpoint_path 是推送给 bi_center 的路径；cursor_field 是
    返回数据里用于推进游标的字段。snapshot=True 表示该表会按 ID 分页循环快照，
    以便捕获任务状态、用户部门等原地更新的数据。
    """

    name: str
    endpoint_path: str
    cursor_field: str
    sql: str
    uses_initial_sync_start: bool = False
    snapshot: bool = False


STREAM_SPECS: dict[str, StreamSpec] = {
    "actions": StreamSpec(
        name="actions",
        endpoint_path="/api/zentao/actions/batch",
        cursor_field="action_id",
        uses_initial_sync_start=True,
        sql="""
            SELECT
                a.id AS action_id,
                a.actor AS actor,
                a.action AS action,
                a.objectType AS object_type,
                a.objectID AS object_id,
                a.date AS action_date,
                a.extra AS extra,
                u.dept AS dept_id,
                d.name AS dept_name,
                u.deleted AS user_deleted
            FROM zt_action a
            LEFT JOIN zt_user u ON a.actor = u.account
            LEFT JOIN zt_dept d ON u.dept = d.id
            WHERE a.id > %s
              AND a.date >= %s
            ORDER BY a.id ASC
            LIMIT %s
        """,
    ),
    "aiscore_results": StreamSpec(
        name="aiscore_results",
        endpoint_path="/api/zentao/aiscore-results/batch",
        cursor_field="score_result_id",
        uses_initial_sync_start=True,
        sql="""
            SELECT
                r.id AS score_result_id,
                r.objectType AS object_type,
                r.objectID AS object_id,
                r.action AS action,
                r.actionID AS action_id,
                r.field AS field,
                r.score AS score,
                r.suggestions AS suggestions,
                r.times AS score_times,
                r.createBy AS create_by,
                r.createDate AS create_date,
                u.realname AS realname,
                u.dept AS dept_id,
                d.name AS dept_name,
                u.deleted AS user_deleted
            FROM zt_aiscore_result r
            LEFT JOIN zt_user u ON r.createBy = u.account
            LEFT JOIN zt_dept d ON u.dept = d.id
            WHERE r.id > %s
              AND r.createDate >= %s
            ORDER BY r.id ASC
            LIMIT %s
        """,
    ),
    "tasks": StreamSpec(
        name="tasks",
        endpoint_path="/api/zentao/tasks/batch",
        cursor_field="task_id",
        snapshot=True,
        sql="""
            SELECT
                id AS task_id,
                project,
                execution,
                name,
                type,
                status,
                version,
                openedBy AS opened_by,
                openedDate AS opened_date,
                assignedTo AS assigned_to,
                finishedBy AS finished_by,
                finishedDate AS finished_date,
                lastEditedBy AS last_edited_by,
                lastEditedDate AS last_edited_date,
                deleted,
                aiScore AS ai_score
            FROM zt_task
            WHERE id > %s
            ORDER BY id ASC
            LIMIT %s
        """,
    ),
    "bugs": StreamSpec(
        name="bugs",
        endpoint_path="/api/zentao/bugs/batch",
        cursor_field="bug_id",
        snapshot=True,
        sql="""
            SELECT
                id AS bug_id,
                project,
                product,
                execution,
                title,
                type,
                status,
                openedBy AS opened_by,
                openedDate AS opened_date,
                assignedTo AS assigned_to,
                resolvedBy AS resolved_by,
                resolvedDate AS resolved_date,
                lastEditedBy AS last_edited_by,
                lastEditedDate AS last_edited_date,
                deleted,
                aiScore AS ai_score
            FROM zt_bug
            WHERE id > %s
            ORDER BY id ASC
            LIMIT %s
        """,
    ),
    "stories": StreamSpec(
        name="stories",
        endpoint_path="/api/zentao/stories/batch",
        cursor_field="story_id",
        snapshot=True,
        sql="""
            SELECT
                id AS story_id,
                product,
                title,
                type,
                status,
                version,
                openedBy AS opened_by,
                openedDate AS opened_date,
                assignedTo AS assigned_to,
                reviewedBy AS reviewed_by,
                reviewedDate AS reviewed_date,
                lastEditedBy AS last_edited_by,
                lastEditedDate AS last_edited_date,
                deleted,
                aiScore AS ai_score
            FROM zt_story
            WHERE id > %s
            ORDER BY id ASC
            LIMIT %s
        """,
    ),
    "users": StreamSpec(
        name="users",
        endpoint_path="/api/zentao/users/batch",
        cursor_field="user_id",
        snapshot=True,
        sql="""
            SELECT
                u.id AS user_id,
                u.account,
                u.realname,
                u.type,
                u.dept AS dept_id,
                d.name AS dept_name,
                u.role,
                u.superior,
                u.deleted,
                u.scoreStatistic AS score_statistic
            FROM zt_user u
            LEFT JOIN zt_dept d ON u.dept = d.id
            WHERE u.id > %s
            ORDER BY u.id ASC
            LIMIT %s
        """,
    ),
    "depts": StreamSpec(
        name="depts",
        endpoint_path="/api/zentao/depts/batch",
        cursor_field="dept_id",
        snapshot=True,
        sql="""
            SELECT
                id AS dept_id,
                name,
                parent,
                path,
                grade,
                `order` AS sort_order,
                position,
                `function` AS dept_function,
                manager
            FROM zt_dept
            WHERE id > %s
            ORDER BY id ASC
            LIMIT %s
        """,
    ),
    "aiscore_rules": StreamSpec(
        name="aiscore_rules",
        endpoint_path="/api/zentao/aiscore-rules/batch",
        cursor_field="rule_id",
        snapshot=True,
        sql="""
            SELECT
                id AS rule_id,
                objectType AS object_type,
                field,
                rules,
                scoreMin AS score_min,
                scoreMax AS score_max,
                editDate AS edit_date
            FROM zt_aiscore_rules
            WHERE id > %s
            ORDER BY id ASC
            LIMIT %s
        """,
    ),
    "ai_prompts": StreamSpec(
        name="ai_prompts",
        endpoint_path="/api/zentao/ai-prompts/batch",
        cursor_field="prompt_id",
        snapshot=True,
        sql="""
            SELECT
                id AS prompt_id,
                name,
                `desc` AS description,
                model,
                module,
                source,
                triggerControl AS trigger_control,
                targetForm AS target_form,
                purpose,
                elaboration,
                role,
                characterization,
                status,
                createdBy AS created_by,
                createdDate AS created_date,
                editedBy AS edited_by,
                editedDate AS edited_date,
                deleted
            FROM zt_ai_prompt
            WHERE id > %s
            ORDER BY id ASC
            LIMIT %s
        """,
    ),
    "pivot_configs": StreamSpec(
        name="pivot_configs",
        endpoint_path="/api/zentao/pivot-configs/batch",
        cursor_field="config_id",
        snapshot=True,
        sql="""
            SELECT
                id AS config_id,
                vision,
                owner,
                module,
                section,
                `key` AS config_key,
                value
            FROM zt_config
            WHERE id > %s
              AND owner = 'system'
              AND module = 'pivot'
              AND section IN ('aiScoreAvgSettingPoint', 'aiScoreAvgLevel')
            ORDER BY id ASC
            LIMIT %s
        """,
    ),
    "cases": StreamSpec(
        name="cases",
        endpoint_path="/api/zentao/cases/batch",
        cursor_field="case_id",
        snapshot=True,
        sql="""
            SELECT
                id AS case_id,
                project,
                product,
                execution,
                module,
                story,
                storyVersion AS story_version,
                title,
                pri,
                type,
                status,
                openedBy AS opened_by,
                openedDate AS opened_date,
                reviewedBy AS reviewed_by,
                reviewedDate AS reviewed_date,
                lastEditedBy AS last_edited_by,
                lastEditedDate AS last_edited_date,
                version,
                deleted
            FROM zt_case
            WHERE id > %s
            ORDER BY id ASC
            LIMIT %s
        """,
    ),
    "docs": StreamSpec(
        name="docs",
        endpoint_path="/api/zentao/docs/batch",
        cursor_field="doc_id",
        snapshot=True,
        sql="""
            SELECT
                id AS doc_id,
                vision,
                project,
                product,
                execution,
                lib,
                module,
                title,
                type,
                status,
                parent,
                path,
                grade,
                `order` AS sort_order,
                addedBy AS added_by,
                addedDate AS added_date,
                editedBy AS edited_by,
                editedDate AS edited_date,
                version,
                deleted
            FROM zt_doc
            WHERE id > %s
            ORDER BY id ASC
            LIMIT %s
        """,
    ),
}


class ZentaoClient:
    """禅道 MariaDB 客户端。

    连接信息来自 Settings。生产环境中该账号应只具备 SELECT 权限。
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def _connect(self):
        """创建 MariaDB 连接。

        charset 使用 utf8mb4，尽量避免中文部门名、用户名在读取时出现乱码。
        timeout 设置为有限值，防止数据库网络异常时任务长期卡住。
        """
        return pymysql.connect(
            host=self.settings.zentao_db_host,
            port=self.settings.zentao_db_port,
            user=self.settings.zentao_db_user,
            password=self.settings.zentao_db_password,
            database=self.settings.zentao_db_name,
            charset="utf8mb4",
            cursorclass=DictCursor,
            connect_timeout=10,
            read_timeout=30,
            write_timeout=30,
        )

    def health_check(self) -> bool:
        """执行最轻量的数据库连通性检查。"""
        with self._connect() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT 1 AS ok")
                row = cursor.fetchone()
                return row["ok"] == 1

    def fetch_actions(
        self,
        last_action_id: int,
        initial_sync_start: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        """读取一批 zt_action 增量数据。

        last_action_id 是本地游标，只拉取更大的 action id。
        initial_sync_start 是首次补数边界，避免第一次运行时扫描过老历史。
        limit 控制单批大小，防止一次请求传输过多数据。
        """

        return self.fetch_stream("actions", last_action_id, initial_sync_start, limit)

    def get_stream_spec(self, stream_name: str) -> StreamSpec:
        """返回数据流定义，配置错误时给出明确异常。"""
        try:
            return STREAM_SPECS[stream_name]
        except KeyError as exc:
            supported = ", ".join(sorted(STREAM_SPECS))
            raise ValueError(f"Unsupported sync stream: {stream_name}. Supported: {supported}") from exc

    def fetch_stream(
        self,
        stream_name: str,
        last_id: int,
        initial_sync_start: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        """按数据流读取一批禅道数据。

        action 和 AI 评分结果是事件流，会使用 initial_sync_start 做首次补数边界。
        任务、Bug、需求等维度表是快照流，会按主键循环分页同步。
        """
        spec = self.get_stream_spec(stream_name)
        params: tuple[Any, ...]
        if spec.uses_initial_sync_start:
            params = (last_id, initial_sync_start, limit)
        else:
            params = (last_id, limit)

        with self._connect() as conn:
            with conn.cursor() as cursor:
                # 使用参数化 SQL，避免字符串拼接带来的注入风险。
                cursor.execute(spec.sql, params)
                rows = cursor.fetchall()
        return [self._normalize_row(row) for row in rows]

    def _normalize_row(self, row: dict[str, Any]) -> dict[str, Any]:
        """把数据库行转换成可 JSON 序列化的字典。

        PyMySQL 会把 DATETIME/DATE 转成 Python 对象，这里统一转字符串。
        Decimal、bytes 等类型也在这里做轻量归一化，避免 JSON 序列化失败。
        """
        return {key: self._normalize_value(value) for key, value in row.items()}

    def _normalize_value(self, value: Any) -> Any:
        """把数据库字段值转换成 JSON 友好的基础类型。"""
        if isinstance(value, datetime):
            return value.isoformat()
        if isinstance(value, date):
            return value.isoformat()
        if isinstance(value, Decimal):
            return int(value) if value == value.to_integral_value() else float(value)
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace")
        return value

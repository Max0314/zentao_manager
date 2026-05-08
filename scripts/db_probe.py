# -*- coding: utf-8 -*-
"""禅道数据库只读探测脚本。

用途：
1. 验证当前机器是否能连接禅道 MariaDB。
2. 验证账号是否能读取中转服务需要的核心表。
3. 输出基础计数和最大 action id，方便确认同步起点。

脚本只执行 SELECT，不会修改数据库。
"""

import argparse
import json
from typing import Any

import pymysql
from pymysql.cursors import DictCursor


def scalar(cursor: Any, sql: str) -> Any:
    """执行只返回一个值的查询。"""
    cursor.execute(sql)
    row = cursor.fetchone()
    if not row:
        return None
    return next(iter(row.values()))


def main() -> None:
    parser = argparse.ArgumentParser(description="安全探测禅道 MariaDB 数据库连接。")
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument("--user", required=True)
    parser.add_argument("--password", required=True)
    parser.add_argument("--database", required=True)
    args = parser.parse_args()

    result: dict[str, Any] = {
        "host": args.host,
        "port": args.port,
        "user": args.user,
        "database": args.database,
        "connected": False,
    }

    # 使用 utf8mb4 读取中文字段，尽量避免部门名、姓名出现编码问题。
    with pymysql.connect(
        host=args.host,
        port=args.port,
        user=args.user,
        password=args.password,
        database=args.database,
        charset="utf8mb4",
        cursorclass=DictCursor,
        connect_timeout=8,
        read_timeout=20,
        write_timeout=20,
    ) as conn:
        with conn.cursor() as cursor:
            result["connected"] = True

            # 基础环境信息：确认连到的是预期 MariaDB 和预期数据库。
            result["server_version"] = scalar(cursor, "SELECT VERSION()")
            result["current_database"] = scalar(cursor, "SELECT DATABASE()")

            # 中转服务当前需要同步的表。逐表 COUNT 可以快速暴露只读权限缺失。
            tables = [
                "zt_action",
                "zt_aiscore_result",
                "zt_task",
                "zt_bug",
                "zt_story",
                "zt_user",
                "zt_dept",
                "zt_aiscore_rules",
                "zt_ai_prompt",
                "zt_config",
                "zt_case",
                "zt_doc",
            ]
            result["table_counts"] = {}
            for table in tables:
                result["table_counts"][table] = scalar(cursor, f"SELECT COUNT(*) FROM {table}")

            # 最大 action id 可用于判断当前数据库最新操作位置。
            cursor.execute(
                """
                SELECT id AS max_action_id, date AS max_action_date
                FROM zt_action
                ORDER BY id DESC
                LIMIT 1
                """
            )
            row = cursor.fetchone()
            if row:
                result["max_action_id"] = row["max_action_id"]
                result["max_action_date"] = str(row["max_action_date"])

            cursor.execute(
                """
                SELECT id AS max_score_result_id, createDate AS max_score_create_date
                FROM zt_aiscore_result
                ORDER BY id DESC
                LIMIT 1
                """
            )
            row = cursor.fetchone()
            if row:
                result["max_score_result_id"] = row["max_score_result_id"]
                result["max_score_create_date"] = str(row["max_score_create_date"])

    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

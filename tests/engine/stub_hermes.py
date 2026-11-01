# -*- coding: utf-8 -*-
"""Поддельный `hermes` для тестов: только `kanban --board <b> create …`.

Зачем: end-to-end проверка стража не должна трогать настоящую борду. Скрипт пишет
карточку в указанную sqlite-базу (те же колонки, что читает `gate.read_cards`) и
печатает id вида `t_…`, который страж вылавливает из вывода.

Ожидаемая строка вызова:
    hermes kanban --board <b> create <title> --body-file <path> --assignee <a>
        --tenant <t> --workspace dir:<repo> --idempotency-key <key>
        --max-runtime <r> [--skill <s>]… [--parent <id>]…
"""
from __future__ import annotations

import os
import sqlite3
import sys
import time
import uuid

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    title TEXT,
    status TEXT DEFAULT 'ready',
    assignee TEXT,
    idempotency_key TEXT,
    completed_at INTEGER,
    result TEXT,
    last_failure_error TEXT,
    created_at INTEGER,
    tenant TEXT,
    workspace TEXT,
    max_runtime TEXT
);
"""


def parse(argv: list[str]) -> dict:
    out: dict = {"skills": [], "parents": []}
    i = 0
    if argv and argv[0] == "kanban":
        i = 1
    while i < len(argv):
        token = argv[i]
        if token == "--board":
            out["board"] = argv[i + 1]
            i += 2
        elif token == "create":
            out["title"] = argv[i + 1] if i + 1 < len(argv) and not argv[i + 1].startswith("-") else ""
            i += 2 if out["title"] else 1
        elif token in ("--body-file", "--assignee", "--tenant", "--workspace",
                       "--idempotency-key", "--max-runtime"):
            out[token.lstrip("-").replace("-", "_")] = argv[i + 1]
            i += 2
        elif token == "--skill":
            out["skills"].append(argv[i + 1])
            i += 2
        elif token == "--parent":
            out["parents"].append(argv[i + 1])
            i += 2
        else:
            i += 1
    return out


def main() -> int:
    fields = parse(sys.argv[1:])
    if "title" not in fields:
        print("stub hermes: поддерживается только `kanban create`", file=sys.stderr)
        return 2
    db = os.environ.get("PIPELINE_KANBAN_DB")
    if not db:
        print("stub hermes: нет PIPELINE_KANBAN_DB", file=sys.stderr)
        return 2
    con = sqlite3.connect(db)
    try:
        con.executescript(SCHEMA)
        task_id = "t_" + uuid.uuid4().hex[:8]
        con.execute(
            "INSERT INTO tasks (id, title, status, assignee, idempotency_key, completed_at,"
            " result, last_failure_error, created_at, tenant, workspace, max_runtime)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (task_id, fields.get("title", ""), "ready", fields.get("assignee", ""),
             fields.get("idempotency_key", ""), None, "", "", int(time.time()),
             fields.get("tenant", ""), fields.get("workspace", ""),
             fields.get("max_runtime", "")),
        )
        con.commit()
    finally:
        con.close()
    print(task_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

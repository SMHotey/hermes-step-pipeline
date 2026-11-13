# -*- coding: utf-8 -*-
"""Поддельный `hermes` для тестов: `kanban --board <b> create …` и `… comment …`.

Зачем: end-to-end проверка стража не должна трогать настоящую борду. Скрипт пишет
карточку в указанную sqlite-базу (те же колонки, что читает `gate.read_cards`) и
печатает id вида `t_…`, который страж вылавливает из вывода. Комментарий (находки
ревьюера в карточке правок) пишется строкой в таблицу `comments`.

Ожидаемые строки вызова:
    hermes kanban --board <b> create <title> --body-file <path> --assignee <a>
        --tenant <t> --workspace dir:<repo> --idempotency-key <key>
        --max-runtime <r> [--skill <s>]… [--parent <id>]…
    hermes kanban --board <b> comment <task_id> <text> [--author <a>] [--max-len <n>]

Переменная `PIPELINE_STUB_COMMENT_RC`: по умолчанию 0 — комментарий пишется; иное
число — команда печатает причину в stderr и возвращает это число (так тест изображает
отказ CLI).

Переменная `PIPELINE_STUB_LOST_CREATE`: `1` — карточка не пишется, но id печатается.
Так изображается тихая потеря: CLI сказал «создано», а строки на борде нет. Страж обязан
это заметить (`_create_card` сверяет карточку с бордой), а не стоять в простое.
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
CREATE TABLE IF NOT EXISTS comments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT,
    body TEXT,
    author TEXT,
    created_at INTEGER
);
"""


def parse(argv: list[str]) -> dict:
    """Разбор строки: общие флаги, подкоманда `create` либо `comment` и её аргументы."""
    out: dict = {"skills": [], "parents": [], "words": []}
    i = 0
    if argv and argv[0] == "kanban":
        i = 1
    while i < len(argv):
        token = argv[i]
        if token == "--board":
            out["board"] = argv[i + 1]
            i += 2
        elif token == "create":
            out["command"] = "create"
            title = argv[i + 1] if i + 1 < len(argv) and not argv[i + 1].startswith("-") else ""
            out["title"] = title
            i += 2 if title else 1
        elif token == "comment":
            out["command"] = "comment"
            if i + 1 < len(argv) and not argv[i + 1].startswith("-"):
                out["task_id"] = argv[i + 1]
                i += 2
            else:
                i += 1
        elif token in ("--body-file", "--assignee", "--tenant", "--workspace",
                       "--idempotency-key", "--max-runtime", "--author", "--max-len"):
            out[token.lstrip("-").replace("-", "_")] = argv[i + 1]
            i += 2
        elif token == "--skill":
            out["skills"].append(argv[i + 1])
            i += 2
        elif token == "--parent":
            out["parents"].append(argv[i + 1])
            i += 2
        elif token.startswith("-"):
            i += 1                      # незнакомый флаг: значение не угадываем
        else:
            if "command" not in out:
                out["command"] = token  # первая неизвестная подкоманда — в ошибку
            else:
                out["words"].append(token)
            i += 1
    return out


def comment(fields: dict, db: str) -> int:
    """Записать комментарий в `comments` либо изобразить отказ CLI кодом возврата."""
    task_id = str(fields.get("task_id") or "").strip()
    text = " ".join(fields.get("words") or []).strip()
    if not task_id or not text:
        print("stub hermes: нужны id карточки и текст", file=sys.stderr)
        return 2
    rc = int(os.environ.get("PIPELINE_STUB_COMMENT_RC") or "0")
    if rc:
        print("stub hermes: комментарии недоступны", file=sys.stderr)
        return rc
    try:
        max_len = int(fields.get("max_len") or 0)
    except ValueError:
        max_len = 0
    body = text[:max_len] if max_len > 0 else text
    # timeout: под нагрузкой (полный прогон, несколько агентов) запись ждёт, а не падает
    # «database is locked» — иначе карточка терялась бы молча.
    con = sqlite3.connect(db, timeout=15)
    try:
        con.execute("PRAGMA busy_timeout = 15000")
        con.executescript(SCHEMA)
        con.execute("INSERT INTO comments (task_id, body, author, created_at)"
                    " VALUES (?,?,?,?)",
                    (task_id, body, fields.get("author", ""), int(time.time())))
        con.commit()
    finally:
        con.close()
    print(f"комментарий в {task_id}")
    return 0


def main() -> int:
    fields = parse(sys.argv[1:])
    command = fields.get("command")
    if command not in ("create", "comment"):
        print("stub hermes: неизвестная подкоманда", file=sys.stderr)
        return 2
    db = os.environ.get("PIPELINE_KANBAN_DB")
    if not db:
        print("stub hermes: нет PIPELINE_KANBAN_DB", file=sys.stderr)
        return 2
    if command == "comment":
        return comment(fields, db)
    task_id = ""
    con = sqlite3.connect(db, timeout=15)
    try:
        con.execute("PRAGMA busy_timeout = 15000")
        con.executescript(SCHEMA)
        # Идемпотентность как у настоящего Hermes: на занятый ключ возвращается прежняя
        # карточка ЛЮБОГО статуса (включая done), новая не создаётся. Без этого движок
        # нельзя проверить на повторный заказ — именно так терялись круги правок плана.
        key = (fields.get("idempotency_key") or "").strip()
        if key:
            row = con.execute(
                "SELECT id FROM tasks WHERE idempotency_key = ? AND status <> 'archived'"
                " ORDER BY created_at DESC LIMIT 1", (key,)).fetchone()
            if row:
                print(row[0])
                return 0
        task_id = "t_" + uuid.uuid4().hex[:8]
        if os.environ.get("PIPELINE_STUB_LOST_CREATE") == "1":
            # Тихая потеря: CLI печатает id, строки на борде нет.
            print(task_id)
            return 0
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

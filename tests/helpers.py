# -*- coding: utf-8 -*-
"""Помощники тестов плагина: установленный проект, прогон стража, чтение борды."""
from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

TESTS = Path(__file__).resolve().parent
REPO = TESTS.parent
PLUGIN = REPO / "step_pipeline"
ENGINE = PLUGIN / "engine"
if str(TESTS) not in sys.path:
    sys.path.insert(0, str(TESTS))
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

PACKAGE_NAME = "step_pipeline"
ALIASES = ("project", "telegram", "snapshot", "setup_flow", "cli", "tools", "ui_server",
           "ui_control")


def load_plugin():
    """Загрузить плагин и выставить короткие алиасы модулей для тестов."""
    module = importlib.import_module(PACKAGE_NAME)
    for alias in ALIASES:
        sys.modules.setdefault(alias, importlib.import_module(f"{PACKAGE_NAME}.{alias}"))
    return module


load_plugin()

PLAN = """# 1. Демо-план конвейера

Цель: проверить визуализацию и действия UI.

## Шаги

### S1. Таблица счетов (1 дн)

- Файл: `app/models/invoice.py`, `tests/test_invoice.py`
- Тесты/гейты: `python -m pytest tests/test_invoice.py -q` → 3 passed
- Готово = таблица `invoice` создана, 3 теста зелёные

### S2. Эндпоинт списка (2 дн)

- Файл: `app/api/invoices.py`
- Тесты/гейты: `python -m pytest tests/test_api_invoice.py -q` → 5 passed
- Готово = `GET /invoices` отдаёт 200 и список

## Журнал выполнения

| Дата | Шаг | Коммиты | Прогоны и доказательства | Статус |
|---|---|---|---|---|
| — | S1 | — | — | открыт |
| — | S2 | — | — | открыт |
"""

CONFIG = {
    "project": "demo",
    "board": "testboard",
    "profiles": {"driver": "demo", "prompter": "prompter", "reviewer": "reviewer"},
    "skills": {"step": ["pipeline-next-step"], "review": ["pipeline-reviewer"],
               "prompt": ["pipeline-prompter", "agent-prompt-authoring"]},
    "paths": {"plans": "docs/plans", "reviews": "docs/reviews",
              "prompts": "docs/prompts", "state": ".pipeline/state"},
    "markers": {"journal": "журнал", "steps": "шаг"},
    "step_id_regex": r"(?P<id>[A-Za-z]{1,4}\d+[A-Za-z]?(?:-\d+)?)",
    "driven_plans": ["*"],
    "require_prompt": True,
    "runtimes": {"step": "3h", "review": "150m", "prompt": "45m"},
    "kanban": {"tenant": "plan"},
    "git": {"commit": False, "push": False},
    "notify": {"enabled": False, "token_env": "STEP_PIPELINE_TELEGRAM_BOT_TOKEN",
               "chat_env": "STEP_PIPELINE_TELEGRAM_CHAT_ID", "prefix": ""},
}

KANBAN_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY, title TEXT, status TEXT DEFAULT 'ready', assignee TEXT,
    idempotency_key TEXT, completed_at INTEGER, result TEXT, last_failure_error TEXT,
    created_at INTEGER, tenant TEXT, workspace TEXT, max_runtime TEXT
);
"""


def _write_approval(root: Path, plan: Path, plan_id: str = "1") -> Path:
    """Запись утверждения плана: без неё страж не ведёт план (шаг 4-S7).

    Та же причина, что у `engine_kit.make_project` (D8): фикстура ставит проект,
    который конвейер ведёт с первого тика, — значит утверждение обязано быть.
    """
    path = root / ".pipeline" / "state" / f"plan_{plan_id}_approval.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "plan": plan_id,
        "file": plan.relative_to(root).as_posix(),
        "sha256": hashlib.sha256(plan.read_bytes()).hexdigest(),
        "approved_at": "2026-01-01T00:00:00",
        "profile": "test",
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def install_project(root: Path, board: str = "testboard", approved: bool = True) -> Path:
    """Проект с установленным движком — как после `hermes pipeline setup`."""
    (root / ".pipeline").mkdir(parents=True, exist_ok=True)
    for src in ENGINE.glob("*.py"):
        shutil.copy2(src, root / ".pipeline" / src.name)
    for rel in ("docs/plans", "docs/reviews", "docs/prompts"):
        (root / rel).mkdir(parents=True, exist_ok=True)
    plan = root / "docs" / "plans" / "1-demo.md"
    plan.write_text(PLAN, encoding="utf-8")
    cfg = json.loads(json.dumps(CONFIG))
    cfg["board"] = board
    (root / "pipeline.json").write_text(json.dumps(cfg, ensure_ascii=False, indent=2),
                                        encoding="utf-8")
    if approved:
        _write_approval(root, plan)
    return root


def gate_python() -> str:
    return sys.executable


def run_gate(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([gate_python(), str(root / ".pipeline" / "gate.py"), *args],
                          cwd=str(root), capture_output=True, text=True,
                          encoding="utf-8", errors="replace")


def stub_cards(db: Path) -> list[dict]:
    if not db.is_file():
        return []
    con = sqlite3.connect(db)
    try:
        con.executescript(KANBAN_SCHEMA)
        rows = con.execute("SELECT id,title,status,assignee,idempotency_key,max_runtime"
                           " FROM tasks ORDER BY created_at").fetchall()
    finally:
        con.close()
    return [{"id": r[0], "title": r[1], "status": r[2], "assignee": r[3], "key": r[4],
             "max_runtime": r[5]} for r in rows]


def complete_card(db: Path, suffix: str) -> None:
    con = sqlite3.connect(db)
    try:
        con.executescript(KANBAN_SCHEMA)
        con.execute("UPDATE tasks SET status='done', completed_at=?, result='ok'"
                    " WHERE idempotency_key LIKE ?", (int(time.time()), "%" + suffix))
        con.commit()
    finally:
        con.close()

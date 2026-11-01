# -*- coding: utf-8 -*-
"""Общие данные и помощники тестов движка: временный проект, борда-заглушка.

Вынесено из conftest, чтобы тесты могли импортировать помощники по имени
(`tests/conftest.py` уже занят фикстурами плагина, и `import conftest` из
вложенного каталога разрешается не туда).
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import sys
import time
from pathlib import Path

KIT = Path(__file__).resolve().parents[2]
ENGINE = KIT / "step_pipeline" / "engine"
sys.path.insert(0, str(ENGINE))

ENGINE_FILES = ("config.py", "registry.py", "next_step.py", "gate.py",
                "step_card.py", "check_task_prompt.py", "tick.py", "__init__.py",
                "state_io.py")

PLAN = """# 1. Демо-план конвейера

Цель: проверить, что страж ведёт шаги.

## Шаги

### S1. Таблица счетов (1 дн)

- Файл: `app/models/invoice.py`, `tests/test_invoice.py`
- Тесты/гейты: `python -m pytest tests/test_invoice.py -q` → 3 passed
- Готово = таблица `invoice` создана, 3 теста зелёные

### S2. Эндпоинт списка (2 дн) `backend`

- Файл: `app/api/invoices.py`
- Тесты/гейты: `python -m pytest tests/test_api_invoice.py -q` → 5 passed
- Готово = `GET /invoices` отдаёт 200 и список
- Важно: не менять контракт существующих ручек

## Журнал выполнения

| Дата | Шаг | Коммиты | Прогоны и доказательства | Статус |
|---|---|---|---|---|
| — | S1 | — | — | открыт |
| — | S2 | — | — | открыт |
"""


def make_project(root: Path, board: str = "testboard", **overrides) -> Path:
    """Создать проект-приёмник: копия движка, план, pipeline.json."""
    (root / ".pipeline").mkdir(parents=True, exist_ok=True)
    for name in ENGINE_FILES:
        shutil.copy2(ENGINE / name, root / ".pipeline" / name)
    plans = root / "docs" / "plans"
    plans.mkdir(parents=True, exist_ok=True)
    (plans / "1-demo.md").write_text(PLAN, encoding="utf-8")
    (root / "docs" / "reviews").mkdir(parents=True, exist_ok=True)
    (root / "docs" / "prompts").mkdir(parents=True, exist_ok=True)

    data = json.loads((KIT / "step_pipeline" / "templates" / "pipeline.json.example")
                      .read_text(encoding="utf-8"))
    data.update({"project": "demo", "board": board})
    data["profiles"] = {"driver": "demo", "prompter": "prompter", "reviewer": "reviewer"}
    data["git"] = {"commit": False, "push": False}
    data.update(overrides)
    (root / "pipeline.json").write_text(json.dumps(data, ensure_ascii=False, indent=2),
                                       encoding="utf-8")
    return root


def cards(db: Path) -> list[dict]:
    """Все карточки из поддельной борды (для проверок)."""
    con = sqlite3.connect(db)
    try:
        rows = con.execute(
            "SELECT id, title, status, assignee, idempotency_key, max_runtime FROM tasks"
            " ORDER BY created_at").fetchall()
    finally:
        con.close()
    return [{"id": r[0], "title": r[1], "status": r[2], "assignee": r[3],
             "key": r[4], "max_runtime": r[5]} for r in rows]


def complete(db: Path, key_suffix: str, status: str = "done", result: str = "") -> None:
    """Отметить карточку завершённой (как это делает воркер)."""
    con = sqlite3.connect(db)
    try:
        con.execute("UPDATE tasks SET status=?, completed_at=?, result=?"
                    " WHERE idempotency_key LIKE ?", (status, int(time.time()), result,
                                                      "%" + key_suffix))
        con.commit()
    finally:
        con.close()

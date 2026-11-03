# -*- coding: utf-8 -*-
"""Общие данные и помощники тестов движка: временный проект, борда-заглушка.

Вынесено из conftest, чтобы тесты могли импортировать помощники по имени
(`tests/conftest.py` уже занят фикстурами плагина, и `import conftest` из
вложенного каталога разрешается не туда).

`make_project` пишет и запись утверждения плана: после шага 4-S7 страж не ведёт план
без `plan_<НОМЕР>_approval.json` (нет записи — нет карточки).
"""
from __future__ import annotations

import hashlib
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


def plan_sha256(path) -> str:
    """sha256 сырых байтов файла плана — то же, что пишет `plan_accept`."""
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_approval(root: Path, plan, plan_id=None, *, sha: str | None = None,
                   **extra) -> Path:
    """Записать `plan_<НОМЕР>_approval.json` как это делает `plan_accept._write_approval`.

    `sha` и `**extra` — чтобы тесты могли положить испорченную запись (чужой хэш,
    лишние ключи). Номер берётся из `plan_id` или из имени файла плана.
    """
    root = Path(root)
    plan = Path(plan)
    number = str(int(plan_id)) if plan_id is not None else plan.name.split("-")[0]
    path = root / ".pipeline" / "state" / f"plan_{number}_approval.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "plan": number,
        "file": plan.resolve().relative_to(root.resolve()).as_posix(),
        "sha256": sha or plan_sha256(plan),
        "approved_at": "2026-01-01T00:00:00",
        "profile": "test",
    }
    payload.update(extra)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def make_project(root: Path, board: str = "testboard", approved: bool = True,
                 **overrides) -> Path:
    """Создать проект-приёмник: копия движка, план, pipeline.json, запись утверждения."""
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
    if approved:
        write_approval(root, plans / "1-demo.md")
    return root


def plan_body(count: int = 4, closed: dict | None = None) -> str:
    """План из `count` шагов с журналом; `closed` задаёт статус строки (`S1` → «ЗАКРЫТ»).

    Нужен тестам предзаказа постановки (план 2, S6): им важны и число шагов (последний
    шаг предзаказ не получает), и уже закрытые строки журнала.
    """
    statuses = closed or {}
    parts = ["# 1. Демо-план конвейера", "", "Цель: проверить предзаказ постановки.", "",
             "## Шаги", ""]
    for i in range(1, count + 1):
        parts += [f"### S{i}. Шаг номер {i} (1 дн)", "",
                  f"- Файл: `app/s{i}.py`",
                  f"- Тесты/гейты: `python -m pytest tests/test_s{i}.py -q` → 1 passed",
                  f"- Готово = шаг S{i} сделан", ""]
    parts += ["## Журнал выполнения", "",
              "| Дата | Шаг | Коммиты | Прогоны и доказательства | Статус |",
              "|---|---|---|---|---|"]
    for i in range(1, count + 1):
        status = statuses.get(f"S{i}", "открыт")
        parts.append(f"| — | S{i} | — | — | {status} |")
    return "\n".join(parts) + "\n"


def write_plan(root: Path, text: str) -> Path:
    """Переписать план проекта и переутвердить его (запись хранит sha файла плана)."""
    root = Path(root)
    path = root / "docs" / "plans" / "1-demo.md"
    path.write_text(text, encoding="utf-8")
    write_approval(root, path)
    return path


def write_prompt(root: Path, step: str, recon: str | None = None) -> Path:
    """Постановка шага; `recon` — отметка `recon_commit:` в шапке (план 2, S6)."""
    root = Path(root)
    folder = root / "docs" / "prompts"
    folder.mkdir(parents=True, exist_ok=True)
    head = f"recon_commit: {recon}\n" if recon else ""
    path = folder / f"2026-11-04_1-{step}_prompt.md"
    path.write_text(f"# Постановка {step}\n\n{head}\n## Ревью\n\nроль `reviewer`.\n",
                    encoding="utf-8")
    return path


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

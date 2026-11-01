# -*- coding: utf-8 -*-
"""Снимок состояния конвейера для UI и инструментов агента.

Всё читается из проекта и борды, ничего не меняется. Источники:
    gate.py --plans --json     планы, следующий шаг, стоп-флаги
    gate.py --status --json    решение стража по текущему плану
    next_step.py --json        шаги плана, контракт следующего, git
    kanban db (sqlite, ro)     карточки ролей
    файл плана                 хвост журнала
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

try:  # обычный путь — пакет плагина; __main__ — ручная проверка файлом
    from . import project as proj
except ImportError:  # pragma: no cover
    import project as proj  # type: ignore[no-redef]

CARD_COLUMNS = ("id", "title", "status", "assignee", "idempotency_key", "created_at",
                "completed_at", "result", "last_failure_error", "tenant", "max_runtime")
STAGE_LABELS = {"create_step": "заказ постановки", "create_review": "ревью",
                "create_fix": "правки после ревью", "close_step": "журнал и коммит",
                "create_rereview": "повторное ревью", "idle": "ждёт",
                "stop": "остановлен", "create_rereview_after_fix": "повторное ревью"}
ACTION_LABELS = {"create_step": "шаг", "create_prompt": "постановка", "create_review": "ревью",
                 "create_fix": "правки", "create_rereview": "ревью", "close_step": "журнал",
                 "idle": "ждёт", "stop": "стоп", "retry_pending": "довыкладка"}


def _ro(path: Path) -> sqlite3.Connection | None:
    if not path.is_file():
        return None
    try:
        return sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    except sqlite3.Error:
        return None


def cards(db: Path, limit: int = 30) -> list[dict]:
    """Последние карточки борды. Схема читается по факту: колонки менялись между версиями."""
    con = _ro(db)
    if con is None:
        return []
    try:
        present = {row[1] for row in con.execute("PRAGMA table_info(tasks)")}
        cols = [c for c in CARD_COLUMNS if c in present]
        if not cols:
            return []
        order = "created_at" if "created_at" in present else cols[0]
        rows = con.execute(f"SELECT {', '.join(cols)} FROM tasks ORDER BY {order} DESC"
                           f" LIMIT {int(limit)}").fetchall()
    except sqlite3.Error:
        return []
    finally:
        con.close()
    return [dict(zip(cols, row)) for row in rows]


def journal_tail(project: Path, plan_file: str | None, limit: int = 8) -> list[list[str]]:
    """Последние строки таблицы журнала (без шапки и разделителя)."""
    if not plan_file:
        return []
    path = Path(project) / plan_file
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    marker = (proj.read_config(project).get("markers") or {}).get("journal") or "журнал"
    start = None
    for index, line in enumerate(lines):
        if line.strip().startswith("#") and marker.lower() in line.strip().lower():
            start = index
            break
    if start is None:
        return []
    rows: list[list[str]] = []
    for line in lines[start + 1:]:
        stripped = line.strip()
        if stripped.startswith("#"):
            break
        if not stripped.startswith("|"):
            continue
        cells = [c.strip() for c in stripped.strip("|").split("|")]
        if not cells or set("".join(cells)) <= set("-: "):
            continue
        if cells and cells[0].lower() in ("дата", "date"):
            continue
        rows.append(cells)
    return rows[-int(limit):]


def telegram_state(project: Path) -> dict:
    """Настроены ли уведомления (значения секретов наружу не отдаются)."""
    import os
    cfg = proj.read_config(project)
    notify = cfg.get("notify") or {}
    token_env = notify.get("token_env") or "STEP_PIPELINE_TELEGRAM_BOT_TOKEN"
    chat_env = notify.get("chat_env") or "STEP_PIPELINE_TELEGRAM_CHAT_ID"
    return {
        "enabled": bool(notify.get("enabled")),
        "token_set": bool(os.environ.get(token_env)),
        "chat": os.environ.get(chat_env) or "",
        "token_env": token_env,
        "chat_env": chat_env,
    }


def snapshot(project: Path | str | None = None, plan: str | None = None,
             preview: bool = False, board: str | None = None) -> dict:
    """Полный снимок: конфиг, планы, шаги, карточки, журнал, решение стража.

    *preview* — читать проект, в котором конвейер не установлен: движок берётся из
    плагина, конфиг — умолчания (борду можно задать явно). Ничего не пишется.
    """
    root = Path(project).resolve() if project else proj.find_project()
    cfg = proj.read_config(root)
    engine_dir = proj.ENGINE_SRC if preview else None
    extra_env = {"PIPELINE_ASSUME_DEFAULTS": "1"} if preview else {}
    if preview and board:
        extra_env["PIPELINE_BOARD"] = board
    out: dict[str, Any] = {
        "project": cfg.get("project") or root.name,
        "root": str(root),
        "board": board or cfg.get("board") or "default",
        "profiles": cfg.get("profiles") or {},
        "runtimes": cfg.get("runtimes") or {},
        "driven": cfg.get("driven_plans") or [],
        "require_prompt": cfg.get("require_prompt", True),
        "telegram": telegram_state(root),
        "preview": preview,
        "plans": [],
        "plan": None,
        "package": None,
        "decision": None,
        "status": "",
        "cards": cards(proj.board_db(root, board)),
        "journal": [],
        "errors": [],
    }
    try:
        out["plans"] = proj.engine_json(root, "gate.py", "--plans", engine_dir=engine_dir,
                                        extra_env=extra_env) or []
    except Exception as exc:                                    # noqa: BLE001 - показываем в UI
        out["errors"].append(f"планы: {exc}")
    chosen = plan
    if chosen is None and out["plans"]:
        # по умолчанию — план, у которого есть открытые шаги (иначе первый по номеру)
        pending = [p["id"] for p in out["plans"]
                   if str(p.get("next") or "") not in ("", "—", "None")]
        chosen = str(pending[0]) if pending else str(out["plans"][0]["id"])
    args = ["--plan", str(chosen)] if chosen else []
    try:
        decision = proj.engine_json(root, "gate.py", "--status", *args,
                                    engine_dir=engine_dir, extra_env=extra_env) or {}
        out["decision"] = decision.get("decision") or {}
        out["status"] = decision.get("status") or ""
    except Exception as exc:                                    # noqa: BLE001
        out["errors"].append(f"решение: {exc}")
    try:
        pkg = proj.engine_json(root, "next_step.py", *args,
                               engine_dir=engine_dir, extra_env=extra_env) or {}
        out["package"] = pkg
        out["plan"] = {
            "id": pkg.get("plan_id"),
            "file": pkg.get("plan_file"),
            "steps_total": pkg.get("steps_total", 0),
            "closed": pkg.get("closed") or [],
            "steps": pkg.get("steps") or [],
            "next": pkg.get("next"),
            "note": pkg.get("note") or "",
        }
        out["journal"] = journal_tail(root, pkg.get("plan_file"))
        out["git"] = pkg.get("git") or {}
    except Exception as exc:                                    # noqa: BLE001
        out["errors"].append(f"шаги: {exc}")
    decision = out.get("decision") or {}
    out["stage"] = ACTION_LABELS.get(decision.get("action") or "", decision.get("action") or "")
    out["stop"] = any((p.get("stop") for p in out["plans"]))
    return out


if __name__ == "__main__":  # ручная проверка: python -m plugin.snapshot <проект>
    import sys
    print(json.dumps(snapshot(sys.argv[1] if len(sys.argv) > 1 else None),
                     ensure_ascii=False, indent=2, default=str))

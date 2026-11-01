# -*- coding: utf-8 -*-
"""Инструменты агента: состояние, тик и управление конвейером.

Три инструмента покрывают весь цикл «посмотреть → толкнуть → остановить»:
    pipeline_status   снимок: планы, шаги, карточки, решение стража, Telegram
    pipeline_run      один тик стража (создаёт карточку постановки/шага/ревью)
    pipeline_control  стоп/продолжить/ручной допуск шага
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

try:
    from . import project as proj
    from . import snapshot as snap
except ImportError:  # pragma: no cover - отладка файлом
    import project as proj  # type: ignore
    import snapshot as snap  # type: ignore

STATUS_SCHEMA = {
    "name": "pipeline_status",
    "description": ("Состояние конвейера шагов в проекте: планы, шаги с контрактом, "
                    "карточки ролей, решение стража и следующий шаг."),
    "parameters": {
        "type": "object",
        "properties": {
            "project": {"type": "string", "description": "каталог проекта (по умолчанию — текущий)"},
            "plan": {"type": "string", "description": "номер плана"},
            "cards": {"type": "integer", "description": "сколько карточек показать (по умолчанию 10)"},
        },
    },
}

RUN_SCHEMA = {
    "name": "pipeline_run",
    "description": ("Один тик стража: заказать постановку, выдать карточку шага, "
                    "закрыть шаг после APPROVE или поставить следующий. Идемпотентно."),
    "parameters": {
        "type": "object",
        "properties": {
            "project": {"type": "string"},
            "plan": {"type": "string", "description": "номер плана (по умолчанию все ведомые)"},
        },
    },
}

CONTROL_SCHEMA = {
    "name": "pipeline_control",
    "description": ("Управление конвейером: stop (остановиться после текущего шага), "
                    "resume (продолжить), hold/unhold (ручной допуск шага)."),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["stop", "resume", "hold", "unhold"]},
            "reason": {"type": "string", "description": "причина остановки"},
            "step": {"type": "string", "description": "шаг для hold/unhold, например S3"},
            "project": {"type": "string"},
        },
        "required": ["action"],
    },
}


def _project(args: dict) -> Path:
    raw = str(args.get("project") or "").strip()
    return Path(raw).expanduser().resolve() if raw else proj.find_project()


def _error(message: str) -> str:
    return json.dumps({"ok": False, "error": message}, ensure_ascii=False)


def _json_block(text: str) -> Any:
    """Разобрать JSON из вывода движка, даже если перед ним что-то напечатано."""
    text = (text or "").strip()
    if not text:
        return None
    start = text.find("{")
    if start < 0:
        start = text.find("[")
    if start < 0:
        return None
    try:
        return json.loads(text[start:])
    except json.JSONDecodeError:
        return None


def handle_status(args: dict) -> str:
    try:
        project = _project(args)
        data = snap.snapshot(project, str(args.get("plan") or "") or None)
    except proj.ProjectNotFound as exc:
        return _error(str(exc))
    except Exception as exc:                                     # noqa: BLE001
        return _error(f"{type(exc).__name__}: {exc}")
    limit = int(args.get("cards") or 10)
    data["cards"] = (data.get("cards") or [])[:limit]
    if data.get("errors") and not data.get("plans") and not data.get("plan"):
        return _error("; ".join(data["errors"]))
    data["ok"] = True
    return json.dumps(data, ensure_ascii=False, default=str)


def handle_run(args: dict) -> str:
    try:
        project = _project(args)
        extra = ["--plan", str(args["plan"])] if args.get("plan") else []
        res = proj.run_engine(project, "gate.py", "--run", "--json", *extra, timeout=900)
    except proj.ProjectNotFound as exc:
        return _error(str(exc))
    except Exception as exc:                                     # noqa: BLE001
        return _error(f"{type(exc).__name__}: {exc}")
    payload: dict[str, Any] = {"ok": res.returncode == 0, "stdout": (res.stdout or "").strip(),
                               "stderr": (res.stderr or "").strip()}
    payload["result"] = _json_block(res.stdout or "")
    return json.dumps(payload, ensure_ascii=False, default=str)


def handle_control(args: dict) -> str:
    action = str(args.get("action") or "").strip().lower()
    try:
        project = _project(args)
        if action == "stop":
            extra = ["--stop", str(args.get("reason") or "остановка владельца")]
        elif action == "resume":
            extra = ["--resume"]
        elif action in ("hold", "unhold"):
            step = str(args.get("step") or "").strip()
            if not step:
                return _error("для hold/unhold нужен параметр step")
            extra = [f"--{action}", step]
        else:
            return _error(f"неизвестное действие: {action}")
        res = proj.run_engine(project, "gate.py", *extra)
    except proj.ProjectNotFound as exc:
        return _error(str(exc))
    except Exception as exc:                                     # noqa: BLE001
        return _error(f"{type(exc).__name__}: {exc}")
    return json.dumps({"ok": res.returncode == 0, "action": action,
                       "stdout": (res.stdout or "").strip(),
                       "stderr": (res.stderr or "").strip()}, ensure_ascii=False)


TOOLS = (
    ("pipeline_status", STATUS_SCHEMA, handle_status, "🧭"),
    ("pipeline_run", RUN_SCHEMA, handle_run, "⚙️"),
    ("pipeline_control", CONTROL_SCHEMA, handle_control, "⏯"),
)

# -*- coding: utf-8 -*-
"""Запуск и остановка плана из панели — одна точка правил (план 6 S3).

Два жеста владельца: `POST /api/plan/start` (добавить номер в `driven_plans` и снять
стоп адресованной командой движка) и `POST /api/plan/stop` (`--plan N --stop "причина"`).
Правила записи ключа владельца `driven_plans` и вызова движка живут здесь, а `ui_server`
держит только маршрут: иначе S3A (`/api/plan/delete`) копировал бы ту же правку списка.

Движок как модуль не импортируется — только `proj.run_engine`, как в `ui_server._run_engine`:
плагин и движок живут раздельно (движок копируется в проект и обязан работать без плагина).

Оба жеста адресные (`--plan N`): без адреса движок берёт план по умолчанию
(`registry.default_plan_id`), а при пустом `default_plan` — наименьший номер, и нажатие на
плане 2 сняло бы стоп плана 1.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

try:  # обычный путь — пакет плагина; __main__ — ручная проверка файлом
    from . import project as proj
except ImportError:  # pragma: no cover
    import project as proj  # type: ignore[no-redef]

# Порядок источников каталога состояния — как у движка (`engine/config.py:337`):
# `PIPELINE_STATE_DIR` → `paths.state` из `pipeline.json` → `.pipeline/state`.
DEFAULT_STATE_DIR = ".pipeline/state"
ENGINE_TIMEOUT = 120       # `--resume`/`--stop` не ждут блокировку тика, 120 с с запасом
NO_PLAN = "нужно поле plan: номер плана"


def _plan_id(value: Any) -> str:
    """Номер плана в каноническом виде: `02` → `2`, ` 31 ` → `31` (как `registry.normalize`)."""
    text = str(value if value is not None else "").strip()
    if not text.isdigit():
        raise ValueError(NO_PLAN)
    return str(int(text))


def _state_dir(project: Path) -> Path:
    """Каталог состояния проекта в порядке движка.

    Переменная окружения стоит выше файла не для красоты: при заданном `PIPELINE_STATE_DIR`
    движок пишет стоп-флаг туда, и панель, читающая только `paths.state`, ответила бы
    «стоп снят», не тронув флаг.
    """
    root = Path(project)
    raw = os.environ.get("PIPELINE_STATE_DIR") or (proj.read_config(root).get("paths") or {}).get("state")
    path = Path(str(raw or DEFAULT_STATE_DIR))
    return path if path.is_absolute() else root / path


def stop_flag(project: Path, value: Any) -> Path:
    """Путь стоп-флага плана: `plan_N_stop.json` в каталоге состояния (номер канонический)."""
    return _state_dir(project) / f"plan_{_plan_id(value)}_stop.json"


def read_stop(project: Path, value: Any) -> dict | None:
    """Payload стоп-флага; файла нет или он битый/не словарь → None."""
    path = stop_flag(project, value)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _driven(project: Path) -> list[str]:
    """`driven_plans` как список строк — в том виде, в каком его прочитает движок.

    Отсутствующий ключ — `["*"]`: движок сливает файл с `DEFAULTS` (`config.py:60`) и ведёт
    все планы. Явный `null` — пустой список: `_deep_merge` оставляет значение файла, и
    `raw.get("driven_plans") or []` даёт ноль ведомых планов. Строка делится по запятым —
    так её присылает форма панели, и так же её разбирает валидатор конфига `ui_server`.
    Число и объект привести к ведомым планам нельзя: обе формы движок не итерирует.
    """
    cfg = proj.read_config(project)
    if "driven_plans" not in cfg:
        return ["*"]
    raw = cfg.get("driven_plans")
    if raw is None:
        return []
    if isinstance(raw, str):
        return [part.strip() for part in raw.split(",") if part.strip()]
    if isinstance(raw, list):
        return [str(item).strip() for item in raw if str(item).strip()]
    return []


def _write_config(project: Path, data: dict) -> None:
    """Записать `pipeline.json` атомарно: временный файл рядом, fsync, `os.replace`.

    `proj.write_config` пишет `write_text` без `os.replace`: оборванная запись оставила бы
    владельца без конфигурации. Порядок ключей сохраняется — `json` читает в dict, а dict
    помнит порядок вставки.
    """
    path = Path(project) / proj.MARKER
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def _engine(project: Path, *args: str) -> dict:
    """Вызвать движок проекта и вернуть разобранный результат, без бросания исключений."""
    res = proj.run_engine(project, "gate.py", *args, timeout=ENGINE_TIMEOUT)
    return {"ok": res.returncode == 0, "code": res.returncode,
            "stdout": (res.stdout or "").strip(), "stderr": (res.stderr or "").strip()}


def _engine_error(result: dict) -> dict:
    """Отказ движка: `stdout`/`stderr` отдаются как есть, разбором прозы код не занимается."""
    error = result["stderr"] or result["stdout"] or f"gate.py вышел с {result['code']}"
    return {"ok": False, "status": "engine_error", "error": error,
            "stdout": result["stdout"], "stderr": result["stderr"]}


def start(project: Path, value: Any) -> dict:
    """Запустить план в работу: `--plan N --resume`, затем номер в `driven_plans`.

    Движок зовётся первым: неизвестный номер он отвергает кодом 2, и ключ владельца после
    отказа остаётся нетронутым — неудавшийся жест не портит конфигурацию. Обратный порядок
    записал бы «план ведётся» у несуществующего плана.

    Стоп-флаг сверяется **до и после** команды: при занятом тике движок печатает «тик уже идёт
    … команда resume не применена» и выходит с кодом 0, оставляя флаг на месте. Снимок «до»
    в одиночку доложил бы «стоп снят» и записал бы `driven_plans` поверх неудавшегося жеста.
    """
    try:
        plan = _plan_id(value)
    except ValueError:
        return {"ok": False, "status": "errors", "error": NO_PLAN}
    project = Path(project)
    cfg = proj.read_config(project)
    if not cfg:
        return {"ok": False, "status": "errors",
                "error": f"pipeline.json не читается: {project / proj.MARKER}"}

    before = read_stop(project, plan)

    engine = _engine(project, "--plan", plan, "--resume")
    if not engine["ok"]:
        return _engine_error(engine)
    after = read_stop(project, plan)
    if before is not None and after == before:
        detail = engine["stdout"] or "движок не снял стоп-флаг"
        return {"ok": False, "status": "engine_error", "plan": plan, "error": detail,
                "stdout": engine["stdout"], "stderr": engine["stderr"],
                "message": f"план {plan} не запущен: {detail}"}
    cleared = before is not None and after is None

    driven = _driven(project)
    changed = False
    if "*" in driven:
        # `*` — правило «все распознанные планы»: замена перечислением молча сняла бы
        # ведение остальных, поэтому файл не переписывается.
        message = "все планы уже ведутся (driven_plans: *)"
    elif plan in {str(int(item)) for item in driven if item.isdigit()}:
        # Сравнение по каноническому номеру: `["02"]` движок читает как план 2, и повторный
        # жест не должен оставлять семантический дубликат `["02", "2"]`.
        message = f"план {plan} уже в driven_plans"
    else:
        driven = [*driven, plan]
        cfg["driven_plans"] = driven
        _write_config(project, cfg)
        changed = True
        message = f"план {plan} добавлен в driven_plans"

    if cleared:
        message += "; стоп снят"
    return {"ok": True, "status": "started", "plan": plan, "changed": changed,
            "cleared": cleared, "driven": driven, "stdout": engine["stdout"],
            "message": message}


def stop(project: Path, value: Any, reason: Any = "") -> dict:
    """Остановить план после текущего шага: `--plan N --stop "причина"`.

    Порядок аргументов именно такой: `--stop` берёт ровно одно значение, пара
    `--stop --plan 2` не разбирается — причиной стал бы `--plan`. `driven_plans` не
    трогается: остановленный план остаётся ведомым, как и после `--stop` в терминале.

    Флаг сверяется **до и после** команды, а не «поставлен ли он вообще»: при занятом тике
    движок печатает «тик уже идёт … команда stop не применена» и выходит с кодом 0, оставляя
    прежний флаг. Проверка «флаг есть» вернула бы `ok: true` со **старой** причиной.
    """
    try:
        plan = _plan_id(value)
    except ValueError:
        return {"ok": False, "status": "errors", "error": NO_PLAN}
    project = Path(project)
    reason = str(reason or "").strip() or "остановка владельца"
    before = read_stop(project, plan)
    engine = _engine(project, "--plan", plan, "--stop", reason)
    if not engine["ok"]:
        return _engine_error(engine)
    payload = read_stop(project, plan)
    if payload is None or payload == before:
        # Флага нет вовсе или он остался прежним (`at`/`reason` те же) — жест не применился.
        detail = engine["stdout"] or "движок не поставил стоп-флаг"
        return {"ok": False, "status": "engine_error", "plan": plan, "error": detail,
                "stdout": engine["stdout"], "stderr": engine["stderr"],
                "message": f"план {plan} не остановлен: {detail}"}
    return {"ok": True, "status": "stopped", "plan": plan, "stop": payload,
            "stdout": engine["stdout"],
            "message": f"план {plan} остановлен после текущего шага: {payload.get('reason', reason)}"}

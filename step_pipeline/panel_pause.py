# -*- coding: utf-8 -*-
"""Глобальная пауза панели: «Остановить после шага» и «Запустить всё».

Жест один на весь реестр, а не на проект: каждому открытому плану каждого проекта
ставится owner-стоп-флаг движка (`gate.py --stop --plan N`). Поэтому текущий шаг
доводится до конца, а следующий не начинается — владелец просил «после шага».

Состояние кнопки считается из планов и борд, а намерение (какие проекты и планы
остановлены жестом) хранится в реестре панели ключом `pause`. Поэтому после
перезагрузки страницы и перезапуска панели кнопка остаётся «Запустить всё», а
«Запустить всё» снимает ровно те флаги, которые поставила пауза.

Движок не правится: пауза — это его же команда владельца, только на весь реестр.
"""
from __future__ import annotations

import subprocess
import threading
import time
from pathlib import Path
from typing import Any

try:  # обычный путь — пакет плагина; __main__ — ручная проверка файлом
    from . import project as proj
    from . import snapshot as snap
except ImportError:  # pragma: no cover
    import project as proj  # type: ignore[no-redef]
    import snapshot as snap  # type: ignore[no-redef]

PAUSE_REASON = "остановка после шага: владелец нажал «Остановить после шага»"
PLANS_TTL = 5.0          # секунд: кэш `gate.py --plans` на проект
CARDS_LIMIT = 200        # сколько последних карточек смотреть на «в работе»
RUNNING_STATUS = "running"
NO_STEP = ("", "—", "None")
# `gate.py --stop` ждёт блокировку тика, а её держит идущий шаг: до получаса. Жест идёт
# в фоне панели, поэтому ждём столько же — иначе таймаут убивал бы стоп именно тогда,
# когда он нужнее всего (во время шага).
STOP_TIMEOUT = 1900

_PLANS_CACHE: dict[Path, tuple[float, list[dict]]] = {}
_CACHE_LOCK = threading.Lock()


def clear_cache() -> None:
    """Сбросить кэш планов: после жеста состояние обязано читаться заново."""
    with _CACHE_LOCK:
        _PLANS_CACHE.clear()


def _installed(root: Path) -> bool:
    """Конвейер установлен: в проекте есть `.pipeline/gate.py` (§4.1)."""
    return (Path(root) / ".pipeline" / "gate.py").is_file()


def plans(root: Path) -> list[dict]:
    """Планы проекта из движка (`--plans --json`) с кэшем на `PLANS_TTL`.

    Кэш нужен сторожу паузы: он сверяет стоп-флаги каждые несколько секунд, а запуск
    движка на проект — это процесс. Сбой движка не глотается: вызывающий кладёт текст
    ошибки в состояние проекта, иначе панель молчала бы о нечитаемом проекте.
    """
    root = Path(root)
    now = time.monotonic()
    with _CACHE_LOCK:
        hit = _PLANS_CACHE.get(root)
        if hit is not None and now - hit[0] <= PLANS_TTL:
            return hit[1]
    data = proj.engine_json(root, "gate.py", "--plans", timeout=90)
    rows = [row for row in data if isinstance(row, dict)] if isinstance(data, list) else []
    with _CACHE_LOCK:
        _PLANS_CACHE[root] = (now, rows)
    return rows


def open_plans(plan_rows: list[dict]) -> list[dict]:
    """Планы, у которых есть следующий шаг: закрытым планам пауза не нужна."""
    out = []
    for row in plan_rows:
        ahead = str(row.get("next") or "")
        if ahead not in NO_STEP:
            out.append(row)
    return out


def running_cards(root: Path, board: str | None = None) -> list[dict]:
    """Карточки борды в работе: по ним видно, что шаг ещё идёт.

    Читает `snap.cards` — в панели это единственный читатель sqlite, схема борды у него
    уже учтена. «В работе» — это `status = running`, а не догадка по времени.
    """
    rows = snap.cards(proj.board_db(Path(root), board), limit=CARDS_LIMIT)
    return [row for row in rows if str(row.get("status") or "") == RUNNING_STATUS]


def project_state(row: dict, board: str | None = None) -> dict:
    """Строка состояния паузы по одному проекту реестра.

    `stopped` — стоп-флаги стоят у всех открытых планов; проект без открытых планов
    в расчёт не идёт (`paused: False`, `finished: True`): останавливать нечего.
    """
    root = Path(row["root"])
    item: dict[str, Any] = {"id": str(row["id"]), "name": str(row["name"]),
                            "root": str(root), "installed": _installed(root),
                            "plans": 0, "open": 0, "stopped_plans": [], "running": [],
                            "paused": False, "finished": False, "error": ""}
    if not item["installed"]:
        return item
    try:
        plan_rows = plans(root)
        item["plans"] = len(plan_rows)
        opened = open_plans(plan_rows)
        item["open"] = len(opened)
        item["stopped_plans"] = [str(row_plan.get("id")) for row_plan in opened
                                 if row_plan.get("stop")]
        item["running"] = [str(card.get("id")) for card in running_cards(root, board)]
    except Exception as exc:                                # noqa: BLE001
        item["error"] = f"{type(exc).__name__}: {exc}"
        return item
    item["finished"] = item["open"] == 0
    item["paused"] = item["open"] > 0 and len(item["stopped_plans"]) == item["open"]
    return item


def state(rows: list[dict], board: str | None = None) -> dict:
    """Состояние жеста для панели: строки проектов, «всё остановлено», «всё на паузе».

    `all_stopped` — стоп-флаги у всех открытых планов всех проектов: на него смотрит
    подпись кнопки (нажать «Запустить всё» можно и во время последнего шага).
    `all_paused` — плюс ни одной карточки в работе: на него смотрит уведомление.
    """
    marker = proj.read_pause()
    projects = [project_state(row, board) for row in rows]
    counted = [item for item in projects
               if item["installed"] and not item["error"] and item["open"] > 0]
    busy = [item for item in counted if item["running"]]
    all_stopped = bool(counted) and all(item["paused"] for item in counted)
    return {
        "active": bool(marker.get("active")),
        "all_stopped": all_stopped,
        "all_paused": all_stopped and not busy,
        "projects": projects,
        "running": [item["id"] for item in busy],
        "unstopped": [item["id"] for item in counted if not item["paused"]],
        "requested_at": float(marker.get("requested_at") or 0.0),
        "requested_at_text": str(marker.get("requested_at_text") or ""),
        "completed_at": float(marker.get("completed_at") or 0.0),
        "notified": bool(marker.get("notified_at")),
        "reason": str(marker.get("reason") or ""),
        "error": str(marker.get("error") or ""),
    }


def _stop_plan(root: Path, plan_id: str) -> tuple[bool, str]:
    """Стоп-флаг плана командой владельца движка: (получилось, текст ошибки)."""
    try:
        res = proj.run_engine(root, "gate.py", "--stop", PAUSE_REASON,
                             "--plan", plan_id, timeout=STOP_TIMEOUT)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"{type(exc).__name__}: {exc}"
    if res.returncode != 0:
        text = ((res.stderr or "") + (res.stdout or "")).strip()
        return False, text or f"gate.py завершился с кодом {res.returncode}"
    return True, ""


def _resume_plan(root: Path, plan_id: str) -> tuple[bool, str]:
    """Снять стоп-флаг плана командой владельца движка."""
    try:
        res = proj.run_engine(root, "gate.py", "--resume", "--plan", plan_id,
                              timeout=STOP_TIMEOUT)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"{type(exc).__name__}: {exc}"
    if res.returncode != 0:
        text = ((res.stderr or "") + (res.stdout or "")).strip()
        return False, text or f"gate.py завершился с кодом {res.returncode}"
    return True, ""


def _raw(marker: dict) -> dict:
    """Маркер в том виде, в каком он лежит в файле: вычисленные поля не пишутся."""
    return {key: value for key, value in marker.items() if key not in ("active", "error")}


def mark(**fields: Any) -> dict:
    """Дописать поля в маркер паузы (время полной остановки, отметка доставки)."""
    marker = _raw(proj.read_pause())
    if not marker:
        return proj.read_pause()
    marker.update(fields)
    try:
        return proj.write_pause(marker)
    except (OSError, ValueError):
        return proj.read_pause()


def pause_all(rows: list[dict], board: str | None = None) -> dict:
    """Остановить после шага все проекты реестра.

    Каждая команда движка берёт блокировку тика, поэтому шаг, который идёт сейчас,
    доводится до конца, а следующий не начнётся. Маркер пишется один на весь жест:
    по нему «Запустить всё» знает, какие планы снимать, а уведомление — что пауза
    запрошена именно владельцем.
    """
    stopped: dict[str, dict] = {}
    failed: list[dict] = []
    for row in rows:
        root = Path(row["root"])
        if not _installed(root):
            continue
        try:
            plan_rows = open_plans(plans(root))
        except Exception as exc:                            # noqa: BLE001
            failed.append({"project": str(row["id"]), "name": str(row["name"]), "plan": "",
                           "error": f"{type(exc).__name__}: {exc}"})
            continue
        done: list[str] = []
        for plan_row in plan_rows:
            plan_id = str(plan_row.get("id") or "")
            if not plan_id:
                continue
            ok, error = _stop_plan(root, plan_id)
            if ok:
                done.append(plan_id)
                continue
            failed.append({"project": str(row["id"]), "name": str(row["name"]),
                           "plan": plan_id, "error": error})
        if done:
            stopped[str(row["id"])] = {"name": str(row["name"]), "plans": done}
    if not stopped:
        reason = ("нечего останавливать: открытых планов нет" if not failed
                  else "ни один план не остановлен")
        clear_cache()
        return {"ok": False, "error": reason, "failed": failed,
                "state": state(rows, board)}
    marker = {"requested_at": time.time(),
              "requested_at_text": time.strftime("%d.%m.%Y %H:%M:%S"),
              "reason": PAUSE_REASON, "projects": stopped,
              "completed_at": 0.0, "notified_at": 0.0}
    try:
        proj.write_pause(marker)
    except (OSError, ValueError) as exc:
        # Флаги уже стоят — проекты остановлены, потеряна только память жеста.
        failed.append({"project": "", "name": "реестр", "plan": "",
                       "error": f"маркер паузы не записан: {exc}"})
    clear_cache()
    names = ", ".join(f"{item['name']} (планы {', '.join(item['plans'])})"
                      for item in stopped.values())
    return {"ok": True, "stopped": stopped, "failed": failed,
            "text": f"остановка после шага: {names}",
            "state": state(rows, board)}


def resume_all(rows: list[dict], board: str | None = None) -> dict:
    """Снять паузу: «Запустить всё».

    Маркер есть — снимаются ровно те планы, которые остановил жест. Маркера нет —
    снимаются все owner-флаги реестра: кнопка «Запустить всё» обязана работать и
    тогда, когда владелец ставил стоп по проектам вручную. Флаги инцидента
    (`kind=incident`) не трогаются: их снимает страж, а не панель.
    """
    marker = proj.read_pause()
    roots = {str(row["id"]): (Path(row["root"]), str(row["name"])) for row in rows}
    targets: dict[str, list[str]] = {}
    for project_id, info in (marker.get("projects") or {}).items():
        if project_id in roots and info.get("plans"):
            targets[project_id] = list(info["plans"])
    if not targets:
        for project_id, (root, _name) in roots.items():
            if not _installed(root):
                continue
            try:
                plan_rows = plans(root)
            except Exception as exc:                        # noqa: BLE001
                proj.debug_note("pause/resume", exc)
                continue
            ids = [str(row_plan.get("id")) for row_plan in open_plans(plan_rows)
                   if isinstance(row_plan.get("stop"), dict)
                   and str(row_plan["stop"].get("kind") or "owner") == "owner"]
            if ids:
                targets[project_id] = ids
    resumed: dict[str, dict] = {}
    failed: list[dict] = []
    for project_id, plan_ids in targets.items():
        root, name = roots[project_id]
        done: list[str] = []
        for plan_id in plan_ids:
            ok, error = _resume_plan(root, plan_id)
            if ok:
                done.append(plan_id)
                continue
            failed.append({"project": project_id, "name": name, "plan": plan_id,
                           "error": error})
        if done:
            resumed[project_id] = {"name": name, "plans": done}
    if marker.get("active"):
        try:
            proj.write_pause(None)
        except (OSError, ValueError) as exc:
            failed.append({"project": "", "name": "реестр", "plan": "",
                           "error": f"маркер паузы не снят: {exc}"})
    clear_cache()
    if not resumed and not failed:
        return {"ok": True, "resumed": {}, "state": state(rows, board),
                "text": "снимать нечего: стоп-флагов не было"}
    names = ", ".join(f"{item['name']} (планы {', '.join(item['plans'])})"
                      for item in resumed.values())
    return {"ok": True, "resumed": resumed, "failed": failed,
            "text": f"пауза снята: {names}" if names else "пауза снята",
            "state": state(rows, board)}

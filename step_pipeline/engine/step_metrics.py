# -*- coding: utf-8 -*-
"""Метрика попытки шага: время по фазам, вызовы модели, инструменты (план 17, S1).

Зачем модуль: 20.11.2026 одна попытка шага шла 91 минуту — 17 минут модель (63 вызова),
34 минуты инструменты, 40 минут паузы между событиями. Ни страж, ни панель этих чисел не
видели: единственный источник — `agent.log` профиля, которым исполнялась карточка.

Что делает: разбирает лог попытки тремя шаблонами строк, складывает время по фазам и
пишет строку в `<state_dir>/metrics.jsonl`. Файл — журнал попыток, а не тиков: у стража
рядом лежит `history.jsonl` (метрики тиков), и путать имена нельзя.

Почему разбор регексами, а не «мнение модели»: страж детерминирован, а метрика — вход
следующего шага S4 («повтор после убийства по лимиту требует изменения условий»). Числа
должны быть функцией лога, а не суждения.

Модуль вендорится в проекты вместе с движком (`config.ENGINE_FILES`) и обязан работать без
установленного плагина: только стандартная библиотека.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from pathlib import Path

import state_io

import config as config_mod

METRICS_NAME = "metrics.jsonl"
# Предел файла по строкам, как у истории тиков (`gate.HISTORY_MAX_LINES`): разрезанная
# строка JSONL — битая строка, поэтому режем по границам строк, а последняя запись
# не выбрасывается никогда.
METRICS_MAX_LINES = 2000
# Допуск окна попытки по краям, секунды (Д4): метка строки — начало записи, метка прогона —
# целые секунды, и на границе одна строка может оказаться чуть снаружи.
WINDOW_PAD = 2.0
LOG_NAME = "agent.log"
LOG_FILES = 4          # agent.log плюс три ротации рядом: agent.log.1 … agent.log.3

# Строка метки начала записи: `2026-11-20 10:00:00,000`.
_STAMP = r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3})"

# Вызов модели: `API call #1: model=… provider=… in=… out=… total=… latency=9.6s …`
API_RE = re.compile(
    _STAMP + r".*?\bAPI call #\d+: model=(?P<model>\S+) provider=\S+"
    r" in=(?P<in>\d+) out=(?P<out>\d+) total=\d+ latency=(?P<latency>[\d.]+)s")
# Инструмент завершился: `tool terminal completed (0.14s, 467 chars)` — тега сессии
# в такой строке может не быть, и это нормально (живой лог).
TOOL_OK_RE = re.compile(
    _STAMP + r".*?\btool (?P<name>\w+) completed \((?P<sec>[\d.]+)s")
# Инструмент упал: `Tool patch returned error (420.00s): …`. Таймаут приходит именно этой
# строкой, в теле — `timed out`; строки `[deadline] … timed out after` инструментом не
# являются и в счёт не идут (иначе один таймаут считался бы дважды).
TOOL_ERR_RE = re.compile(
    _STAMP + r".*?\bTool (?P<name>\w+) returned error \((?P<sec>[\d.]+)s\)(?P<body>.*)$")
# Лимит попытки: в `agent.log` его нет (живой замер — 0 строк по всем профилям), текст
# живёт в `task_runs.error`. Шаблон — запасной путь и проверка фикстурой.
LIMIT_RE = re.compile(
    _STAMP + r".*?\belapsed (?P<elapsed>\d+)s > limit (?P<limit>\d+)s")

# Текст ошибки прогона, которым борда говорит «убит по лимиту».
LIMIT_TEXT_RE = re.compile(r"elapsed\s+\d+s\s*>\s*limit\s+\d+s")


def _epoch(stamp: str) -> float:
    """Метка строки лога в epoch (локальное время, как `time.time()` у борды)."""
    try:
        return time.mktime(time.strptime(stamp, "%Y-%m-%d %H:%M:%S,%f"))
    except (OverflowError, ValueError):
        return 0.0


def _int(value) -> int:
    """Целое из значения борды: `None`, пустая строка и мусор — ноль."""
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def parse_events(text: str) -> list[dict]:
    """События лога в порядке строк; строки нераспознанного вида пропускаются.

    Формат события — контракт для следующих шагов (`{"ts", "kind", "name", "model", "in",
    "out", "ms", "timeout", "elapsed_s", "limit_s"}`), незаполненные поля нули и пустая
    строка.
    """
    events: list[dict] = []
    for line in str(text or "").splitlines():
        event = _event(line)
        if event is not None:
            events.append(event)
    return events


def _blank(kind: str, ts: float) -> dict:
    return {"ts": ts, "kind": kind, "name": "", "model": "", "in": 0, "out": 0, "ms": 0,
            "timeout": False, "elapsed_s": 0, "limit_s": 0}


def _event(line: str) -> dict | None:
    match = API_RE.match(line)
    if match:
        event = _blank("api", _epoch(match["ts"]))
        event.update(model=match["model"], **{"in": int(match["in"]),
                                             "out": int(match["out"])},
                     ms=int(round(float(match["latency"]) * 1000)))
        return event
    match = TOOL_OK_RE.match(line)
    if match:
        event = _blank("tool", _epoch(match["ts"]))
        event.update(name=match["name"], ms=int(round(float(match["sec"]) * 1000)))
        return event
    match = TOOL_ERR_RE.match(line)
    if match:
        event = _blank("tool", _epoch(match["ts"]))
        event.update(name=match["name"], ms=int(round(float(match["sec"]) * 1000)),
                     timeout="timed out" in (match["body"] or "").lower())
        return event
    match = LIMIT_RE.match(line)
    if match:
        event = _blank("limit", _epoch(match["ts"]))
        event.update(elapsed_s=int(match["elapsed"]), limit_s=int(match["limit"]))
        return event
    return None


def _hermes_dirs() -> list[Path]:
    """Каталоги Hermes, где искать `profiles/<роль>/logs/agent.log`.

    `HERMES_HOME` в кроне указывает на сам профиль (`<корень>/profiles/<имя>`), поэтому
    рядом с ним проверяется и корень установки. Дальше — обычные места (`LOCALAPPDATA`,
    домашний каталог): движок читает лог того профиля, которым исполнялась карточка.
    """
    dirs: list[Path] = []
    env = os.environ.get("HERMES_HOME")
    if env:
        home = Path(env)
        dirs.append(home)
        if home.parent.name == "profiles":
            dirs.append(home.parent.parent)
    local = os.environ.get("LOCALAPPDATA")
    if local:
        dirs.append(Path(local) / "hermes")
    dirs.append(Path.home() / ".hermes")
    return dirs


def agent_log(role: str) -> Path | None:
    """Файл лога профиля-исполнителя; нет ни лога, ни ротаций — `None`.

    Ротации (`agent.log.1` … `agent.log.3`) берутся, только если самого `agent.log` нет:
    строки одной попытки в разных файлах не склеиваются, иначе окно Д4 потеряло бы смысл.
    """
    role = str(role or "").strip()
    if not role:
        return None
    names = [LOG_NAME] + [f"{LOG_NAME}.{index}" for index in range(1, LOG_FILES)]
    for base in _hermes_dirs():
        folder = base / "profiles" / role / "logs"
        for name in names:
            candidate = folder / name
            try:
                if candidate.is_file():
                    return candidate
            except OSError:
                continue
    return None


def _read_log(path) -> str:
    if path is None:
        return ""
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _most_common(values: list[str]) -> str:
    """Самое частое значение; при равенстве — первое встреченное (Д2)."""
    best, best_count = "", 0
    for value in values:
        count = values.count(value)
        if count > best_count:
            best, best_count = value, count
    return best


def stall_thresholds(cfg=None) -> tuple[int, int]:
    """Пороги залипания инструмента: `(seconds, repeats)` из `cfg.tools` (17-S2, §7).

    Единственное место, где порог читается: без `cfg` (или без ключа) — умолчания из
    `config.DEFAULTS["tools"]`. Своих чисел модуль не держит, иначе порог из `pipeline.json`
    подменялся бы молча, а карточка говорила бы одно, страж считал другое.
    """
    tools = getattr(cfg, "tools", None)
    if not isinstance(tools, dict) or not tools:
        tools = config_mod.DEFAULTS.get("tools") or {}
    seconds = _int(tools.get("stall_seconds"))
    repeats = _int(tools.get("stall_repeats"))
    defaults = config_mod.DEFAULTS["tools"]
    return (seconds or int(defaults["stall_seconds"]),
            repeats or int(defaults["stall_repeats"]))


def _stall_empty() -> dict:
    return {"stall": False, "tool": "", "calls": 0, "s": 0, "timeouts": 0}


def stall_of(tool_events, seconds: int | None = None,
             repeats: int | None = None) -> dict:
    """Признак залипания по событиям инструментов окна попытки (Д1, Д4, 17-S2 П1).

    Залипание — не «один длинный вызов», а метод: `repeats` вызовов одного инструмента
    подряд, каждый не короче `seconds` (Д4: вызов другого инструмента отрезок рвёт), либо
    вызов, упавший по таймауту — тогда порог не нужен. Названный инструмент — тот из
    залипших, у которого суммарное время в окне больше; при равенстве побеждает чей признак
    раньше в логе, иначе порядок решал бы словарь, а не лог.

    `seconds`/`repeats` по умолчанию берутся из `stall_thresholds()`: чисел порога в модуле
    нет. Пустой список событий (и список без залипаний) — `stall: false` и нули.
    """
    if seconds is None or repeats is None:
        default_seconds, default_repeats = stall_thresholds()
        seconds = default_seconds if seconds is None else seconds
        repeats = default_repeats if repeats is None else repeats
    events = [event for event in (tool_events or [])
              if isinstance(event, dict) and str(event.get("kind") or "tool") == "tool"]
    if not events:
        return _stall_empty()
    slow_ms = max(1, int(seconds)) * 1000
    span = max(1, int(repeats))
    names = [str(event.get("name") or "") for event in events]
    times = [_int(event.get("ms")) for event in events]
    timeouts = [bool(event.get("timeout")) for event in events]
    first: dict[str, int] = {}
    for index, name in enumerate(names):
        if not name or name in first:
            continue
        if timeouts[index]:
            first[name] = index
            continue
        run_ms = times[index:index + span]
        if (len(run_ms) == span and all(item >= slow_ms for item in run_ms)
                and len(set(names[index:index + span])) == 1):
            first[name] = index
    if not first:
        return _stall_empty()
    totals: dict[str, int] = {}
    for name, ms in zip(names, times):
        if name in first:
            totals[name] = totals.get(name, 0) + ms
    tool = min(first, key=lambda name: (-totals[name], first[name]))
    return {"stall": True,
            "tool": tool,
            "calls": sum(1 for name in names if name == tool),
            "s": int(totals[tool] / 1000),
            "timeouts": sum(1 for index, name in enumerate(names)
                            if name == tool and timeouts[index])}


CARD_KEY_PREFIX = "planloop"


def card_kind(key) -> str:
    """Вид карточки из ключа идемпотентности: `step`, `fix`, `review` и прочие (Д6).

    Ключ — `planloop:<план>:<шаг>:<вид>[:<номер>]`, вид стоит сразу после шага, а номер
    круга или попытки — хвостом. Ключ чужой формы или пустой — пустая строка, а не догадка:
    правило метода адресуется только карточкам шага и правок.
    """
    parts = str(key or "").split(":")
    if len(parts) >= 4 and parts[0] == CARD_KEY_PREFIX:
        return parts[3]
    return ""


def attempt_row(plan_id, step_id, card: dict, run: dict | None = None, attempt: int = 1,
                now=None, log_path=None, stall_seconds: int | None = None,
                stall_repeats: int | None = None) -> dict:
    """Одна запись метрики по одной попытке шага (ключи — контракт панели и шага S4).

    Окно разбора — от `started_at` до `ended_at` прогона с допуском `WINDOW_PAD` (Д4):
    строки без тега сессии внутри окна принимаются, соседние попытки в счёт не идут.
    Нет записи прогона — берётся весь лог: запасной путь для `killed_by` по строке лимита.

    `killed_by` считается по записи прогона (Д2): `runtime` — исход `timed_out` или текст
    `elapsed Ns > limit Ms`, `review` — исход `changes_requested`, иначе `none`. Строка
    лимита в логе — запасной путь, когда у прогона нет исхода.
    """
    card = card if isinstance(card, dict) else {}
    run = run if isinstance(run, dict) else {}
    stamp = float(now) if now is not None else time.time()
    started = _int(run.get("started_at")) or None
    ended = _int(run.get("ended_at")) or None
    events = parse_events(_read_log(log_path))
    if started and ended:
        low, high = started - WINDOW_PAD, ended + WINDOW_PAD
    elif started:
        low, high = started - WINDOW_PAD, float("inf")
    else:
        low, high = float("-inf"), float("inf")
    window = [event for event in events if low <= float(event["ts"]) <= high]
    api = [event for event in window if event["kind"] == "api"]
    tools = [event for event in window if event["kind"] == "tool"]
    limits = [event for event in window if event["kind"] == "limit"]
    limit_event = limits[-1] if limits else None

    tools_ms: dict[str, int] = {}
    for event in tools:
        tools_ms[event["name"]] = tools_ms.get(event["name"], 0) + int(event["ms"])

    outcome = str(run.get("outcome") or "").strip().lower()
    error = str(run.get("error") or "").strip()
    if outcome == "timed_out" or LIMIT_TEXT_RE.search(error):
        killed_by = "runtime"
    elif outcome == "changes_requested":
        killed_by = "review"
    elif not outcome and limit_event is not None:
        killed_by = "runtime"
    else:
        killed_by = "none"

    if started and ended:
        wall_ms = max(0, ended - started) * 1000
    elif limit_event is not None:
        wall_ms = int(limit_event["elapsed_s"]) * 1000
    elif window:
        wall_ms = int((max(float(item["ts"]) for item in window)
                       - min(float(item["ts"]) for item in window)) * 1000)
    else:
        wall_ms = 0

    limit_s = _int(run.get("max_runtime_seconds")) or (
        int(limit_event["limit_s"]) if limit_event is not None else 0)
    source = "agent.log" if events else "none"
    # Залипание (17-S2): порог приходит из конфигурации (`record`), а прямой вызов без него
    # берёт умолчания через `stall_thresholds()` — строка одного вида, кто бы её ни писал.
    stall = stall_of(tools, stall_seconds, stall_repeats)
    return {
        "ts": stamp,
        "at": time.strftime("%d.%m.%Y %H:%M:%S", time.localtime(stamp)),
        "plan": str(plan_id or ""),
        "step": str(step_id or ""),
        "card": str(card.get("id") or ""),
        "card_kind": card_kind(card.get("key")),
        "attempt": int(attempt or 1),
        "run_id": str(run.get("id") or ""),
        "role": str(run.get("profile") or card.get("assignee") or ""),
        "model": _most_common([event["model"] for event in api]),
        "calls": len(api),
        "in": sum(int(event["in"]) for event in api),
        "out": sum(int(event["out"]) for event in api),
        "model_ms": sum(int(event["ms"]) for event in api),
        "tools_ms": tools_ms,
        "tools_total_ms": sum(tools_ms.values()),
        "tool_timeouts": sum(1 for event in tools if event["timeout"]),
        "stall": bool(stall["stall"]),
        "stall_tool": stall["tool"],
        "stall_calls": stall["calls"],
        "stall_s": stall["s"],
        "stall_timeouts": stall["timeouts"],
        "wall_ms": wall_ms,
        "killed_by": killed_by,
        "limit_s": limit_s,
        "source": source,
        "log": str(log_path) if source == "agent.log" else "",
    }


def metrics_path(cfg) -> Path:
    """Журнал метрики проекта: `<state_dir>/metrics.jsonl` (рядом с историей тиков)."""
    return Path(cfg.state_dir) / METRICS_NAME


def read_rows(path) -> list[dict]:
    """Строки журнала; файла нет или строка битая — пропуск, а не исключение.

    Тик не имеет права падать из-за метрики: битый файл — повод перезаписать его
    следующей записью, а не остановить конвейер (Д5).
    """
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    rows: list[dict] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _card_runs(db, card_id: str) -> list[dict]:
    """Прогоны карточки из `task_runs`; колонки — только те, что есть в борде.

    Состав колонок в бордах разных версий разный (`profile`, `error`,
    `max_runtime_seconds` появились позже), поэтому список читается через
    `PRAGMA table_info`, как в `snapshot._card_runs`. Нет таблицы или базы — пустой
    список: метрика не имеет права валить тик.
    """
    path = Path(db)
    if not path.is_file():
        return []
    try:
        con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    except sqlite3.Error:
        return []
    try:
        present = {row[1] for row in con.execute("PRAGMA table_info(task_runs)")}
        if not present or "task_id" not in present:
            return []
        cols = [name for name in ("id", "task_id", "profile", "status", "started_at",
                                  "ended_at", "outcome", "error", "summary",
                                  "max_runtime_seconds") if name in present]
        order = "started_at" if "started_at" in present else "rowid"
        rows = con.execute(
            f"SELECT {', '.join(cols)} FROM task_runs WHERE task_id = ?"
            f" ORDER BY {order}, rowid", (str(card_id),)).fetchall()
    except sqlite3.Error:
        return []
    finally:
        con.close()
    return [dict(zip(cols, row)) for row in rows]


def record(cfg, kanban_db, plan_id, step_id, cards, now=None) -> int:
    """Дописать строку метрики на каждый завершённый прогон карточек шага; вернуть число.

    Ключ идемпотентности — `run_id` (`task_runs.id`, Д5): повторный тик видит строку и
    вторую не пишет, поэтому «сколько дописано» и есть ответ на вопрос «что нового».
    Номер попытки — порядок прогонов карточки по `started_at`, с единицы.
    """
    path = metrics_path(cfg)
    known = {str(row.get("run_id") or "") for row in read_rows(path)}
    # Порог залипания читается один раз на тик, а не на строку: значения — из конфигурации
    # проекта, и все строки одного заказа должны мериться одинаково (17-S2).
    stall_seconds, stall_repeats = stall_thresholds(cfg)
    pending: list[dict] = []
    for card in cards or []:
        if not isinstance(card, dict):
            continue
        card_id = str(card.get("id") or "")
        if not card_id:
            continue
        runs = _card_runs(kanban_db, card_id)
        for attempt, run in enumerate(runs, start=1):
            run_id = str(run.get("id") or "")
            if not run_id or run_id in known:
                continue
            if not _int(run.get("ended_at")):
                continue          # прогон идёт: строки ещё нет, числа не сложены
            role = str(run.get("profile") or card.get("assignee") or "")
            pending.append(attempt_row(plan_id, step_id, card, run, attempt,
                                       now=now, log_path=agent_log(role),
                                       stall_seconds=stall_seconds,
                                       stall_repeats=stall_repeats))
            known.add(run_id)
    if not pending:
        return 0
    _append(path, pending)
    return len(pending)


def _append(path: Path, rows: list[dict]) -> None:
    """Дописать строки атомарно и уложить файл в `METRICS_MAX_LINES` (по строкам).

    Обрезка по строкам, а не по байтам: разрезанная строка JSONL — битая строка. Последняя
    записанная строка не выбрасывается никогда — `lines[-N:]` её сохраняет.
    """
    try:
        old = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        old = []
    lines = [line for line in old if line.strip()]
    lines.extend(json.dumps(row, ensure_ascii=False) for row in rows)
    if len(lines) > METRICS_MAX_LINES:
        lines = lines[-METRICS_MAX_LINES:]
    state_io.atomic_write_text(path, "\n".join(lines) + "\n")

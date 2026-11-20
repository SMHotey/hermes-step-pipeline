# -*- coding: utf-8 -*-
"""Детерминированный сканер симптомов конвейера (план 10, S2).

Симптом — наблюдаемый факт о состоянии проекта, а не мнение: его даёт уже
существующий источник (борда, файлы состояния, cron, git, каталог скиллов), а не
модель. Один вызов `scan(...)` (или `python .pipeline/keeper_scan.py --scan --json`)
за секунды собирает список, на который опираются cron-часовой (S4) и панель.

Схема вывода — единственное описание контракта:
    {project, repo, scanned_at, scanned_ts, symptoms[], sources[]}
    симптом:  {project, symptom_id, kind, severity, address, evidence, healable}
    источник: {name, status, detail}, status — ok | skipped | error
`symptom_id` — хеш от `(проект, вид, адрес)`, без времени и текста: дедупликация S4
и повторный скан не зависят от переформулировки причины.

Жёсткие ограничения шага: **только чтение** — сканер не пишет ни в состояние
проекта, ни в борду, ни в план (починка принадлежит S3). Без моделей, без внешней
сети (единственное исключение — health-check панели на 127.0.0.1), без новых
пакетов: stdlib и `hermes` из PATH. Каталог пакета плагина приходит флагом
`--plugin-dir`: движок работает копией в `.pipeline/` и пакета не видит, а
`ENGINE_FILES` — единственный список файлов движка, по которому идут копирование,
хеш и проверка дрейфа.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import traceback
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

# Плоский импорт: модуль запускают файлом (`python .pipeline/keeper_scan.py`), и он
# лежит рядом с остальными файлами движка в копии проекта.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import config as config_mod  # noqa: E402 - модуль рядом
import state_io  # noqa: E402 - модуль рядом: содержимое замка и живость pid

#: Стоп-флаг старше этого возраста — симптом (D2). Ключа конфига нет: план заводит
#: в `config.py` ровно два ключа для смотрителя и стража, у этого владельца нет.
STOP_AGE_MINUTES = 60

#: Сколько назад смотрим инциденты cron: «cron incidents за 24 ч» по плану.
CRON_WINDOW_HOURS = 24

#: Остальные статусы борды нетерминальны: `blocked` висит так же, как `todo`.
TERMINAL_STATUSES = ("done", "archived")

#: Живость панели на 127.0.0.1 — как у `ui_control.is_up`.
PROBE_TIMEOUT = 1.5

#: Предел одной внешней команды (`git`, `hermes`): скан не должен висеть на них.
RUN_TIMEOUT = 30.0

#: Вид симптома → (severity, healable). D5: `healable` — предварительная пометка
#: «у вида есть известная команда починки», а не белый список: его делает S3.
KIND_RULES = {
    "card_stuck": ("high", False),
    "stop_stale": ("normal", False),
    "cron_incident": ("high", False),
    "tick_lock_dead": ("high", True),
    "panel_down": ("low", True),
    "engine_drift": ("high", True),
    "version_skew": ("normal", True),
    "skills_drift": ("normal", True),
    "git_dirty": ("low", False),
}

#: Порядок источников в выводе: два прогона отличаются только временем и находками.
SOURCE_NAMES = ("cron", "board", "stop_flags", "tick_lock", "panel", "git",
                "engine", "version", "skills")

#: Ровно эти три статуса источника (D4): нечитаемый источник — строка, не симптом.
SOURCE_STATUSES = ("ok", "skipped", "error")

#: Пустой ответ `hermes cron incidents list`.
CRON_EMPTY = "No cron failure incidents recorded."

#: Инциденты в этих состояниях действующей поломкой не считаются: владелец
#: подтвердил (`closed`) или джоба отработала после сбоя (`resolved`).
CRON_CALM_STATES = ("closed", "resolved")


# ───────────────────────────── сборка симптома ─────────────────────────────

def symptom_id(project: str, kind: str, address: str) -> str:
    """Стабильный id: sha256 от `проект|вид|адрес`, первые 16 знаков.

    Ни времени, ни текста причины в хеше нет — иначе дедупликация S4 ломалась бы
    на каждой переформулировке `evidence`.
    """
    key = f"{project}|{kind}|{address}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def make_symptom(cfg: config_mod.Config, kind: str, address: str, evidence: str) -> dict:
    """Симптом по схеме: severity и healable берутся из `KIND_RULES`."""
    if kind not in KIND_RULES:
        raise KeyError(f"неизвестный вид симптома: {kind}")
    severity, healable = KIND_RULES[kind]
    text = " ".join(str(evidence).split())[:199]
    return {
        "project": str(cfg.project),
        "symptom_id": symptom_id(str(cfg.project), kind, str(address)),
        "kind": kind,
        "severity": severity,
        "address": str(address),
        "evidence": text,
        "healable": bool(healable),
    }


def engine_digest(base: Path) -> str:
    """sha256 по файлам движка из `ENGINE_FILES`: имя, `\\0`, байты, `\\0`.

    Алгоритм тот же, что у `gate.py::engine_digest`, и это не дублирование «на
    всякий случай»: `gate.py` в объём шага S2 не входит, а после S2A страж сам
    импортирует `keeper_scan` — импорт в обратную сторону дал бы цикл. Паритет
    реализаций стережёт тест `test_engine_digest_matches_gate_digest`.
    """
    digest = hashlib.sha256()
    for name in sorted(config_mod.ENGINE_FILES):
        path = Path(base) / name
        if not path.is_file():
            continue
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


# ───────────────────────────── контекст прогона ─────────────────────────────

class _Scan:
    """Состояние одного прогона: конфигурация, подменяемые зависимости, находки."""

    def __init__(self, cfg: config_mod.Config, plugin_dir, now: float, run, probe,
                 hermes: str) -> None:
        self.cfg = cfg
        self.plugin_dir = _package_root(plugin_dir) if plugin_dir else None
        self.now = float(now)
        self.run = run
        self.probe = probe
        self.hermes = hermes
        self.symptoms: list[dict] = []
        self.sources: list[dict] = []

    # находки и строки источников
    def add(self, kind: str, address: str, evidence: str) -> None:
        self.symptoms.append(make_symptom(self.cfg, kind, address, evidence))

    def source(self, name: str, status: str, detail: str) -> None:
        if status not in SOURCE_STATUSES:
            raise ValueError(f"недопустимый статус источника {status!r}")
        self.sources.append({"name": name, "status": status,
                             "detail": " ".join(str(detail).split())})

    def has_source(self, name: str) -> bool:
        return any(item["name"] == name for item in self.sources)

    def watchdog(self, key: str) -> int:
        """Порог смотрителя из `Config.watchdog`: общий со стражем (S2A)."""
        data = self.cfg.watchdog if isinstance(self.cfg.watchdog, dict) else {}
        return int(data.get(key) or 0)

    def call(self, cmd: list[str]):
        """Вызов внешней команды через подменяемый `run` (в тестах — фейк)."""
        return self.run(cmd, RUN_TIMEOUT)


def _package_root(plugin_dir) -> Path:
    """Каталог пакета: `plugin_dir` — он сам или корень установки с `step_pipeline/`.

    Плагин передаёт `proj.PLUGIN_DIR` (каталог пакета: `engine/`, `roles/`,
    `plugin.yaml`), а человек или тест может назвать корень установки — принимаем
    обе формы, чтобы источник не выдумывал «пакета нет» из-за лишнего уровня.
    """
    path = Path(plugin_dir)
    if (path / "engine").is_dir() or (path / "plugin.yaml").is_file():
        return path
    nested = path / "step_pipeline"
    return nested if nested.is_dir() else path


def _alive(pid: int) -> bool:
    """Жив ли pid (D7): публичной обёртки в `state_io` нет, файл вне объёма шага."""
    if pid <= 0:
        return False
    return bool(state_io._pid_alive(pid))


def _stamp(value: object) -> int:
    """Метка времени в epoch; кривое значение — 0, а не исключение."""
    try:
        return int(value or 0)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def _hours(seconds: float) -> str:
    """Часы одной цифрой после запятой: «31.0 ч», «0.5 ч»."""
    return f"{max(0.0, seconds) / 3600:.1f} ч"


def _names(items: list[str], limit: int = 5) -> str:
    """Первые имена списка и «и ещё N» — чтобы `evidence` оставался строкой."""
    head = ", ".join(items[:limit])
    return head + (f" и ещё {len(items) - limit}" if len(items) > limit else "")


def _read_json(path: Path) -> dict:
    """Объект из JSON-файла; нет файла или мусор — пустой словарь."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


# ───────────────────────────── источники ─────────────────────────────

def _src_cron(s: _Scan) -> None:
    """Открытые инциденты cron профиля драйвера за `CRON_WINDOW_HOURS` часов.

    Профиль — из метки `.pipeline/cron.json` (её пишет мастер установки), `cfg.driver`
    — запасной источник. Метки нет или в ней `skipped: true` — cron при установке не
    заводили, проверять нечего.
    """
    marker = _read_json(Path(s.cfg.repo) / ".pipeline" / "cron.json")
    if not marker:
        s.source("cron", "skipped", "нет .pipeline/cron.json — cron при установке не заводили")
        return
    if marker.get("skipped") is True:
        s.source("cron", "skipped", "cron при установке не заводили (skipped)")
        return
    profile = str(marker.get("driver") or "").strip() or str(s.cfg.driver or "").strip()
    cmd = ([s.hermes, "-p", profile] if profile else [s.hermes]) + ["cron", "incidents", "list"]
    try:
        res = s.call(cmd)
    except (OSError, subprocess.SubprocessError) as exc:
        s.source("cron", "skipped", f"hermes недоступен: {exc}")
        return
    out = res.stdout or ""
    if res.returncode != 0:
        detail = " ".join(((res.stderr or out) or "").split())[:160]
        s.source("cron", "error", f"hermes cron incidents list вышел с {res.returncode}: {detail}")
        return
    if not out.strip() or CRON_EMPTY in out:
        s.source("cron", "ok", "действующих инцидентов нет")
        return
    found = 0
    for incident in _parse_incidents(out):
        state = str(incident["state"]).lower()
        if state in CRON_CALM_STATES:
            continue
        seen, parsed = _parse_time(incident["fields"].get("last seen", ""))
        if parsed and s.now - seen > CRON_WINDOW_HOURS * 3600:
            continue
        found += 1
        job = incident["fields"].get("job", "")
        err = incident["fields"].get("error", "")
        tail = f" последний раз {_hours(s.now - seen)}" if parsed else " время не разобрано"
        s.add("cron_incident", incident["id"],
              f"инцидент cron {incident['id']} ({state}) джобы {job or '?'}:"
              f" {err or 'без текста ошибки'};{tail}")
    s.source("cron", "ok", f"действующих инцидентов {found}")


def _parse_incidents(text: str) -> list[dict]:
    """Разобрать блоки `hermes cron incidents list`.

    Блок — строка с двумя пробелами в начале (`идентификатор  состояние`), затем поля
    с отступом четыре пробела (`Job:`, `Type:`, `First seen:`, `Last seen:`, `Error:`
    — первый токен времени поэтому берётся по двоеточию, а не по пробелу). Итоговая
    строка вида `  N incident(s) | ...` пропускается: она тоже с двумя пробелами, но
    токенов в ней больше двух.
    """
    incidents: list[dict] = []
    current: dict | None = None
    for raw in text.splitlines():
        if not raw.startswith("  "):
            continue
        if raw.startswith("    "):
            match = re.match(r"^\s{4}([A-Za-z][A-Za-z ]*):\s?(.*)$", raw)
            if match and current is not None:
                current["fields"][match.group(1).strip().lower()] = match.group(2).strip()
            continue
        if "incident(s)" in raw:
            continue
        parts = raw.strip().split()
        if len(parts) != 2:
            continue
        current = {"id": parts[0], "state": parts[1], "fields": {}}
        incidents.append(current)
    return incidents


def _parse_time(value: str) -> tuple[float, bool]:
    """Время ISO из вывода cron в epoch; не разобралось — `(0.0, False)`."""
    text = str(value or "").strip()
    if not text:
        return 0.0, False
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return 0.0, False
    return moment.timestamp(), True


def _src_board(s: _Scan) -> None:
    """Карточки в нетерминальном статусе, у которых последнее событие старше предела.

    Колонки читаются по фактической схеме (`PRAGMA table_info`): в поддельной борде
    тестов части колонок и таблиц нет, и падать на «no such column» сканер не должен.
    Возраст — от последнего события карточки, а не от `waiting_since` стража: иначе
    карточка, созданная до простоя, не детектируется.
    """
    deadline_hours = s.watchdog("card_deadline_hours")
    if deadline_hours <= 0:
        s.source("board", "skipped", "предел watchdog.card_deadline_hours=0 — проверка выключена")
        return
    db = Path(s.cfg.kanban_db)
    if not db.is_file():
        s.source("board", "skipped", "борды нет")
        return
    try:
        con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
        try:
            cards = _read_cards(con)
        finally:
            con.close()
    except sqlite3.Error as exc:
        s.source("board", "error", f"борда не читается: {exc}")
        return
    fresh = s.watchdog("heartbeat_fresh_minutes") * 60
    deadline = deadline_hours * 3600
    stuck = 0
    nonterminal = 0
    for card in cards:
        status = str(card.get("status") or "")
        if status in TERMINAL_STATUSES:
            continue
        nonterminal += 1
        heartbeat = _stamp(card.get("last_heartbeat_at"))
        # `running` со свежим heartbeat — живой шаг, а не висящая карточка.
        if status == "running" and heartbeat and s.now - heartbeat <= fresh:
            continue
        last = max(_stamp(card.get("created_at")), _stamp(card.get("started_at")),
                   heartbeat, _stamp(card.get("last_event")))
        if last <= 0:
            continue                      # времени нет — возраст не вычислить, не гадаем
        age = s.now - last
        if age <= deadline:
            continue
        stuck += 1
        title = str(card.get("title") or "")
        s.add("card_stuck", str(card.get("id")),
              f"карточка в статусе {status} висит {_hours(age)} (предел {deadline_hours} ч):"
              f" {title[:60]}")
    s.source("board", "ok", f"карточек {len(cards)}, нетерминальных {nonterminal}, висящих {stuck}")


def _read_cards(con: sqlite3.Connection) -> list[dict]:
    """Карточки борды с вычисленным «последним событием» (только чтение)."""
    have = {row[1] for row in con.execute("PRAGMA table_info(tasks)")}
    if not {"id", "status"} <= have:
        raise sqlite3.Error("в таблице tasks нет колонок id/status")
    columns = [name for name in ("id", "title", "status", "created_at", "started_at",
                                 "last_heartbeat_at") if name in have]
    rows = [dict(zip(columns, row))
            for row in con.execute("SELECT " + ", ".join(columns) + " FROM tasks")]
    events: dict[str, int] = {}
    for table, column in (("task_events", "created_at"),
                          ("task_runs", "last_heartbeat_at"),
                          ("task_runs", "ended_at")):
        try:
            for card_id, stamp in con.execute(
                    f"SELECT task_id, MAX({column}) FROM {table} GROUP BY task_id"):
                if stamp is None:
                    continue
                events[str(card_id)] = max(events.get(str(card_id), 0), _stamp(stamp))
        except sqlite3.Error:
            continue                      # таблицы или колонки нет — берём, что есть
    for card in rows:
        card["last_event"] = events.get(str(card.get("id")), 0)
    return rows


def _src_stop_flags(s: _Scan) -> None:
    """Стоп-флаги планов старше `STOP_AGE_MINUTES` — забытый стоп, а не свежий.

    Возраст — по `st_mtime` (Ф6): `at` внутри файла — строка без epoch, разбирать её
    ради возраста значит зависеть от формата даты.
    """
    state_dir = Path(s.cfg.state_dir)
    if not state_dir.is_dir():
        s.source("stop_flags", "skipped", "каталога состояния нет")
        return
    flags = sorted(state_dir.glob("plan_*_stop.json"))
    stale = 0
    for path in flags:
        try:
            age = s.now - path.stat().st_mtime
        except OSError:
            continue
        if age <= STOP_AGE_MINUTES * 60:
            continue
        stale += 1
        plan = re.sub(r"^plan_|_stop\.json$", "", path.name)
        payload = _read_json(path)
        reason = str(payload.get("reason") or payload.get("source") or "")
        s.add("stop_stale", plan,
              f"стоп-флаг плана {plan} висит {_hours(age)} (предел {STOP_AGE_MINUTES} мин):"
              f" {reason[:80] or 'без причины'}")
    s.source("stop_flags", "ok", f"стоп-флагов {len(flags)}, старше {STOP_AGE_MINUTES} мин {stale}")


def _src_tick_lock(s: _Scan) -> None:
    """`tick.lock` с мёртвым pid: блокировку держит процесс, которого уже нет."""
    payload = state_io.holder(Path(s.cfg.state_dir))
    if not payload:
        s.source("tick_lock", "skipped", "блокировки нет")
        return
    pid = _stamp(payload.get("pid"))
    if _alive(pid):
        s.source("tick_lock", "ok", f"тик идёт, держит pid {pid}")
        return
    s.add("tick_lock_dead", state_io.LOCK_NAME,
          f"{state_io.LOCK_NAME} держит мёртвый pid {pid or 'не записан'}"
          f" (с {payload.get('at') or '?'})")
    s.source("tick_lock", "ok", "блокировка с мёртвым pid")


def _src_panel(s: _Scan) -> None:
    """Панель, которая запускалась (`ui.json`), но не отвечает.

    Метки нет — панель не запускалась, проверять нечего: иначе чистый проект на
    машине без панели получал бы симптом без всякой поломки.
    """
    marker = Path(s.cfg.repo) / ".pipeline" / "ui.json"
    if not marker.is_file():
        s.source("panel", "skipped", "панель не запускалась (нет .pipeline/ui.json)")
        return
    info = _read_json(marker)
    pid = _stamp(info.get("pid"))
    host = str(info.get("host") or "127.0.0.1")
    port = _stamp(info.get("port"))
    if not _alive(pid):
        s.add("panel_down", "ui",
              f"панель не работает: pid {pid or 'не записан'} мёртв (метка ui.json)")
        s.source("panel", "ok", "метка ui.json с мёртвым pid")
        return
    if not port:
        s.add("panel_down", "ui", f"панель с pid {pid} не указала порт в ui.json")
        s.source("panel", "ok", "в ui.json нет порта")
        return
    if not s.probe(host, port):
        s.add("panel_down", "ui", f"панель не отвечает на порту {port} (pid {pid})")
        s.source("panel", "ok", f"порт {port} не отвечает")
        return
    s.source("panel", "ok", f"панель отвечает на {host}:{port}")


def _src_git(s: _Scan) -> None:
    """Грязное рабочее дерево проекта: изменённые пути по `git status --porcelain`."""
    try:
        res = s.call(["git", "-C", str(Path(s.cfg.repo)), "status", "--porcelain"])
    except (OSError, subprocess.SubprocessError) as exc:
        s.source("git", "skipped", f"git недоступен: {exc}")
        return
    if res.returncode != 0:
        detail = " ".join(((res.stderr or res.stdout) or "").split())[:120]
        s.source("git", "skipped", f"git не отвечает ({res.returncode}): {detail}")
        return
    paths = [" ".join(line.split()) for line in (res.stdout or "").splitlines() if line.strip()]
    if not paths:
        s.source("git", "ok", "рабочее дерево чистое")
        return
    s.add("git_dirty", "repo", f"в рабочем дереве {len(paths)} изменённых путей: {_names(paths)}")
    s.source("git", "ok", f"изменённых путей {len(paths)}")


def _src_engine(s: _Scan) -> None:
    """Дрейф копии движка: маркер `ENGINE.sha256`, пропавшие файлы, отличие от пакета.

    Один симптом на причину, а не на файл (D6): имена файлов идут в `evidence`, иначе
    одна рассинхронизация давала бы до четырнадцати симптомов.
    """
    base = Path(s.cfg.repo) / ".pipeline"
    problems: list[str] = []
    missing = [name for name in sorted(config_mod.ENGINE_FILES)
               if not (base / name).is_file()]
    if missing:
        problems.append(f"в копии нет файла движка: {_names(missing)}")
    marker = base / "ENGINE.sha256"
    if marker.is_file() and not missing:
        try:
            text = marker.read_text(encoding="utf-8").strip()
        except OSError as exc:
            problems.append(f"маркер целостности не читается: {exc}")
            text = ""
        expected = text.split()[0].removeprefix("sha256:") if text else ""
        if not text:
            problems.append("маркер целостности пуст")
        elif expected:
            actual = engine_digest(base)
            if actual != expected:
                problems.append("движок изменён после установки:"
                                f" {actual[:12]} != {expected[:12]}")
    if s.plugin_dir is not None:
        package_engine = s.plugin_dir / "engine"
        if not package_engine.is_dir():
            problems.append("в пакете нет каталога engine")
        else:
            differs = [name for name in sorted(config_mod.ENGINE_FILES)
                       if (package_engine / name).is_file()
                       and (base / name).is_file()
                       and (package_engine / name).read_bytes() != (base / name).read_bytes()]
            if differs:
                problems.append(f"копия расходится с пакетом: {len(differs)} файла"
                                f" ({_names(differs)})")
    if problems:
        s.add("engine_drift", "engine", "; ".join(problems))
        s.source("engine", "ok", f"причин дрейфа {len(problems)}")
        return
    s.source("engine", "ok",
             "копия сходится с маркером" + ("" if s.plugin_dir else " (пакет не проверялся)"))


def _src_version(s: _Scan) -> None:
    """Версия движка проекта (`.pipeline/VERSION`) против версии пакета (`plugin.yaml`)."""
    if s.plugin_dir is None:
        s.source("version", "skipped", "нет --plugin-dir")
        return
    try:
        project_version = (Path(s.cfg.repo) / ".pipeline" / "VERSION").read_text(
            encoding="utf-8").strip()
    except OSError:
        s.source("version", "skipped", "маркера .pipeline/VERSION нет")
        return
    package_version = _plugin_version(s.plugin_dir / "plugin.yaml")
    if not package_version:
        s.source("version", "skipped", "в пакете нет plugin.yaml")
        return
    if project_version == package_version:
        s.source("version", "ok", f"версия {project_version}")
        return
    s.add("version_skew", "version",
          f"проект {project_version}, пакет {package_version} — hermes pipeline setup --upgrade")
    s.source("version", "ok", f"проект {project_version}, пакет {package_version}")


def _plugin_version(path: Path) -> str:
    """`version:` из `plugin.yaml` без PyYAML — тот же разбор, что у мастера установки."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return ""
    match = re.search(r"(?m)^version:\s*['\"]?([^'\"\s]+)", text)
    return match.group(1) if match else ""


def _src_skills(s: _Scan) -> None:
    """Роли пакета против `.hermes/skills/*/<роль>` проекта.

    Сравниваются только файлы пакета: свои файлы проекта расхождением не считаются
    (Ф12) — иначе проектный скилл считался бы поломкой установки.
    """
    if s.plugin_dir is None:
        s.source("skills", "skipped", "нет --plugin-dir")
        return
    roles_dir = s.plugin_dir / "roles"
    if not roles_dir.is_dir():
        s.source("skills", "skipped", "в пакете нет каталога roles")
        return
    roles = sorted(item.name for item in roles_dir.iterdir() if item.is_dir())
    skills_root = Path(s.cfg.repo) / ".hermes" / "skills"
    drifted = 0
    for role in roles:
        package_role = roles_dir / role
        installed = sorted(skills_root.glob(f"*/{role}"))
        if not installed:
            drifted += 1
            s.add("skills_drift", role, f"роль {role} не установлена в .hermes/skills")
            continue
        bad: list[str] = []
        for rel in _files(package_role):
            want = (package_role / rel).read_bytes()
            if any(not (target / rel).is_file() or (target / rel).read_bytes() != want
                   for target in installed):
                bad.append(rel)
        if bad:
            drifted += 1
            s.add("skills_drift", role,
                  f"роль {role} расходится с пакетом: файлов {len(bad)}"
                  f" ({_names(sorted(set(bad)), 3)})")
    s.source("skills", "ok", f"ролей в пакете {len(roles)}, расходятся {drifted}")


def _files(root: Path) -> list[str]:
    """Файлы каталога рекурсивно, относительно корня, без `__pycache__`."""
    out: list[str] = []
    for path in sorted(Path(root).rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root)
        if "__pycache__" in rel.parts:
            continue
        out.append(rel.as_posix())
    return out


#: Источник → функция. Порядок вывода задаёт `SOURCE_NAMES`, не этот словарь.
_SOURCES = {
    "cron": _src_cron,
    "board": _src_board,
    "stop_flags": _src_stop_flags,
    "tick_lock": _src_tick_lock,
    "panel": _src_panel,
    "git": _src_git,
    "engine": _src_engine,
    "version": _src_version,
    "skills": _src_skills,
}


# ───────────────────────────── внешние зависимости ─────────────────────────────

def _default_run(cmd: list[str], timeout: float):
    """`subprocess.run` с выключенным цветом: вывод cron разбирается текстом."""
    env = dict(os.environ)
    env["NO_COLOR"] = "1"
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout, env=env)


def _default_probe(host: str, port: int) -> bool:
    """Живость панели: единственный сетевой обход сканера, только localhost."""
    url = f"http://{host}:{port}/api/health"
    try:
        with urllib.request.urlopen(url, timeout=PROBE_TIMEOUT) as resp:
            return 200 <= resp.status < 300
    except (urllib.error.URLError, OSError, ValueError):
        return False


# ───────────────────────────── API шага ─────────────────────────────

def scan(cfg: config_mod.Config | None = None, *, plugin_dir: Path | str | None = None,
         now: float | None = None, run=None, probe=None, hermes: str | None = None) -> dict:
    """Собрать симптомы проекта. Только чтение: ни одной записи по пути.

    `plugin_dir` — каталог пакета плагина (`None` — источники `version`, `skills` и
    пакетная часть `engine` помечаются `skipped`); `now` — epoch для тестов;
    `run` — `(cmd, timeout) → объект с returncode/stdout/stderr` (через него идут `git`
    и `hermes`, поэтому тесты не зависят от PATH); `probe` — живость панели;
    `hermes` — путь к CLI (по умолчанию из PATH).
    """
    if cfg is None:
        cfg = config_mod.load()
    session = _Scan(
        cfg=cfg,
        plugin_dir=plugin_dir,
        now=time.time() if now is None else now,
        run=_default_run if run is None else run,
        probe=_default_probe if probe is None else probe,
        hermes=(shutil.which("hermes") or "hermes") if hermes is None else hermes,
    )
    for name in SOURCE_NAMES:
        try:
            _SOURCES[name](session)
        except Exception as exc:          # noqa: BLE001 - один сбой не отменяет остальных
            session.source(name, "error", f"сбой источника: {type(exc).__name__}: {exc}")
        if not session.has_source(name):
            # Молчаливая потеря проверки хуже строки в `sources`: источник обязан отчитаться.
            session.source(name, "error", "источник не отчитался о результате")
    symptoms = sorted(session.symptoms, key=lambda item: (item["kind"], item["address"]))
    return {
        "project": str(cfg.project),
        "repo": Path(cfg.repo).resolve().as_posix(),
        "scanned_at": datetime.fromtimestamp(session.now).strftime("%d.%m.%Y %H:%M:%S"),
        "scanned_ts": int(session.now),
        "symptoms": symptoms,
        "sources": session.sources,
    }


def format_human(result: dict) -> str:
    """Человеческий вывод: шапка, строка на симптом, строка источников. Без цвета."""
    lines = [f"Проект {result.get('project')}: симптомов {len(result.get('symptoms') or [])}"]
    for item in result.get("symptoms") or []:
        lines.append(f"{item['severity']}  {item['kind']}  {item['address']} — {item['evidence']}")
    source_lines = ", ".join(
        f"{item['name']}={item['status']}"
        + (f" ({item['detail']})" if item["status"] != "ok" else "")
        for item in result.get("sources") or [])
    lines.append(f"источники: {source_lines}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """Точка входа процесса: `--scan [--json] [--plugin-dir PATH] [--project PATH]`.

    Коды возврата: `0` — сканирование выполнено (найденные симптомы — не ошибка),
    `2` — конфигурация не читается, `1` — непойманный сбой. Для cron (S4) ненулевой
    код означает «сканер сломан», а не «есть находки».
    """
    parser = argparse.ArgumentParser(
        prog="keeper_scan.py", description="Детерминированный сканер симптомов конвейера")
    parser.add_argument("--scan", action="store_true", help="собрать симптомы (по умолчанию)")
    parser.add_argument("--json", action="store_true", help="машинный вывод")
    parser.add_argument("--plugin-dir", default="", help="каталог пакета плагина")
    parser.add_argument("--project", default="", help="корень проекта")
    parser.add_argument("--hermes", default="", help="путь к CLI hermes")
    args = parser.parse_args(argv)
    if args.project:
        os.environ["PIPELINE_REPO"] = str(Path(args.project).expanduser().resolve())
    try:
        result = scan(plugin_dir=args.plugin_dir or None, hermes=args.hermes or None)
    except config_mod.ConfigError as exc:
        print(f"конфигурация не читается: {exc}", file=sys.stderr)
        return 2
    except Exception:                     # noqa: BLE001 - код 1: сканер сломан
        traceback.print_exc()
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2) if args.json else format_human(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())

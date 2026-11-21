# -*- coding: utf-8 -*-
"""Починки смотрителя по белому списку, предохранители и часовой обход (план 10, S3–S4).

Модуль берёт симптомы сканера (`keeper_scan.py`) и применяет закрытую таблицу
«вид → действие» (`ACTIONS`): снять мёртвый `tick.lock`, перезапустить упавшую
панель, привести движок и роли к пакету через `hermes pipeline setup --upgrade`.
Всё, чего в таблице нет, остаётся эскалацией: починки «по смыслу» нет.

Часовой обход (`--hourly`, S4) — один вход для cron-джобы: скан, починки белого
списка, карточка со скиллом `pipeline-keeper` на профиль `profiles.keeper`
(умолчание — `profiles.driver`) с JSON симптомов и отчётом, сообщение владельцу
при находке `severity=high`. Карточка заводится при находках в любом режиме
починок: `keeper.on` разрешает команды, а не извещение. Пустой скан не заводит
карточку вовсе — модель не будится на целом проекте. Обход не идёт, пока
держится живой `tick.lock` или на борде идёт карточка конвейера (`running` со
свежим heartbeat): две работы разом не запускаются.

Запускают его cron-часовой (S4) командой `python .pipeline/keeper_heal.py --hourly`
и роль `pipeline-keeper`, которая читает журнал. Своих решений о шагах, планах и
карточках шага модуль не принимает: вердикт, закрытие шага и строка журнала — не
работа смотрителя.

Что модуль пишет: только журнал `keeper.jsonl`, (по команде человека) маркеры
`keeper.on`/`keeper.off` в каталоге состояния проекта и — в режиме apply — одну
карточку на борде. Единственный файл, который он
удаляет, — `tick.lock` там же, и только если pid в полезной нагрузке мёртв. Код
проекта, файлы пакета плагина, `pipeline.json`, `driven_plans` не меняются никогда:
запретов здесь нет, просто нет таких действий — функция, которая правит движок,
в модуле отсутствует.

Импорты плоские (`import config as config_mod`, `import state_io`, `import keeper_scan`):
модуль копируется в `.pipeline/` проекта и запускается файлом, движок обязан работать
без установленного плагина. Зависимости — stdlib и CLI `hermes` из PATH.

Режим по умолчанию — dry-run: пока владелец не положил `state/keeper.on`, ни одна
внешняя команда не запускается и журнал не пишется. Файл `state/keeper.off` сильнее
подтверждения: он выключает починки целиком.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

# Плоский импорт: модуль запускают файлом (`python .pipeline/keeper_heal.py`) и он лежит
# рядом с остальными файлами движка в копии проекта.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import config as config_mod  # noqa: E402 - модуль рядом
import keeper_scan  # noqa: E402 - модуль рядом: симптомы, живость pid, внешние вызовы
import state_io  # noqa: E402 - модуль рядом: замок тика

# ───────────────────────────── константы шага ─────────────────────────────

JOURNAL_NAME = "keeper.jsonl"        # журнал починок: строка JSON на симптом
OFF_MARKER = "keeper.off"            # kill-switch: файл есть — починок нет
ON_MARKER = "keeper.on"              # подтверждение владельца: без файла только dry-run
MAX_HEALS_PER_HOUR = 3               # лимит действий в час
HEAL_WINDOW_SECONDS = 3600.0         # окно лимита
COOLDOWN_MINUTES = 120.0             # кулдаун на тот же symptom_id
RUN_TIMEOUT = 120.0                  # потолок одной внешней команды (обновление движка)
DETAIL_LIMIT = 200                   # длина `detail` в строке журнала

#: Ровно четыре исхода: `planned` — dry-run, команда не запускалась.
STATUSES = ("ok", "failed", "refused", "planned")

REASON_OFF = "off"
REASON_BLACKLIST = "not_in_whitelist"
REASON_LIVE_TICK = "live_tick"
REASON_COOLDOWN = "cooldown"
REASON_LIMIT = "limit"
REASON_NO_HERMES = "no_hermes"

#: Белый список — единственный источник права на действие: вид → действие.
#: Ключи обязаны совпадать с `healable=True` в `keeper_scan.KIND_RULES`; соответствие
#: стережёт тест. Вид вне таблицы — отказ `not_in_whitelist`: пометка сканера
#: предварительная, таблица шага сильнее (и правку движка делает человек, а не модуль).
ACTIONS = {
    "tick_lock_dead": "lock_clear",
    "panel_down": "panel_restart",
    "engine_drift": "engine_upgrade",
    "version_skew": "engine_upgrade",
    "skills_drift": "engine_upgrade",
}

#: Порядок `results`: severity по убыванию, внутри — вид, затем адрес.
SEVERITY_ORDER = {"high": 0, "normal": 1, "low": 2}

#: Действия с внешней командой; `lock_clear` работает с файлом замка.
COMMAND_ACTIONS = ("panel_restart", "engine_upgrade")

#: Умолчание порта панели — как у `step_pipeline/cli.py::cmd_ui`.
DEFAULT_UI_PORT = 8770


# ─────────────────── константы часового обхода (S4) ───────────────────

#: Скилл карточки: вид работы различает он, а не профиль (S1).
KEEPER_SKILL = "pipeline-keeper"

#: Потолок карточки, если `runtimes.keeper` не задан: обход — не шаг плана.
DEFAULT_KEEPER_RUNTIME = "30m"

#: Ключ своих карточек смотрителя и префикс ключей конвейера (D5): по первому
#: обход узнаёт себя, по второму — идущую работу.
KEEPER_KEY_PREFIX = "keeper:"
PLANLOOP_KEY_PREFIX = "planloop:"

#: Замок обхода — свой файл в каталоге состояния (D11): взять `tick.lock` значило бы
#: отключить все починки белого списка (`heal` отказывает при живом держателе), включая
#: снятие мёртвого замка. Форма нагрузки и признак протухания — как у `state_io`, имя
#: своё; правка `state_io` вне трёх путей шага (Г7).
KEEPER_LOCK_NAME = "keeper.lock"
KEEPER_LOCK_STALE = 1800.0

#: Окно «работа идёт сейчас», если `watchdog.heartbeat_fresh_minutes` не задан:
#: то же умолчание, что в шаблоне `pipeline.json`.
DEFAULT_FRESH_MINUTES = 30

CARD_TITLE_LIMIT = 90       # заголовок карточки — одна строка борды
NOTIFY_TEXT_LIMIT = 300     # сообщение владельцу — одна строка
NOTIFY_HIGH_LIMIT = 3       # сколько high-симптомов перечислять в сообщении

SEVERITY_HIGH = "high"

#: Причины пропуска прогона — значения `reason` в схеме §4.1.
REASON_KEEPER_RUNNING = "keeper_running"
REASON_TICK_RUNNING = "tick_running"
REASON_CONVEYOR_BUSY = "conveyor_busy"


# ───────────────────────────── состояние прогона ─────────────────────────────

@dataclass
class HealState:
    """Всё, что нужно одному прогону: конфигурация, режим и подменяемые зависимости."""

    cfg: config_mod.Config
    state_dir: Path
    plugin_dir: Path | None
    now: float
    mode: str                       # "off" | "apply" | "dry-run"
    hermes: str                     # путь к CLI; не найден — слово `hermes`
    hermes_found: bool
    run: Callable
    probe: Callable
    commit: str | None = None
    #: D7: обновление одно на прогон — исход первого вызова делится на всю группу.
    upgrade: tuple[str, str] | None = field(default=None)

    @property
    def dry_run(self) -> bool:
        return self.mode != "apply"


# ───────────────────────────── маркеры и журнал ─────────────────────────────

def marker_path(state_dir, name: str) -> Path:
    """Путь маркера режима в каталоге состояния."""
    return Path(state_dir) / name


def journal_path(state_dir) -> Path:
    """Путь журнала починок в каталоге состояния."""
    return Path(state_dir) / JOURNAL_NAME


def read_journal(state_dir) -> list[dict]:
    """Записи журнала; битая строка пропускается и файл не переписывается (риск 6)."""
    try:
        lines = journal_path(state_dir).read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out: list[dict] = []
    for line in lines:
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict):
            out.append(item)
    return out


def append_journal(state_dir, line: dict) -> Path:
    """Дописать строку журнала: append, UTF-8, без перезаписи (журнал только растёт)."""
    path = journal_path(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(line, ensure_ascii=False) + "\n")
    return path


def _stamp(value: object) -> int:
    """Метка времени в epoch; кривое значение — 0 (журнал не должен ронять прогон)."""
    try:
        return int(value or 0)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def heals_in_window(state_dir, now: float) -> int:
    """Сколько починок действительно запускалось за последний час (`ran: true`)."""
    return sum(1 for line in read_journal(state_dir)
               if line.get("ran") is True
               and str(line.get("result") or "") in ("ok", "failed")
               and now - _stamp(line.get("ts")) < HEAL_WINDOW_SECONDS)


def last_entry(state_dir, symptom_id: str) -> dict | None:
    """Последняя строка журнала по этому симптому (источник кулдауна)."""
    last: dict | None = None
    for line in read_journal(state_dir):
        if str(line.get("symptom_id") or "") != str(symptom_id):
            continue
        if last is None or _stamp(line.get("ts")) > _stamp(last.get("ts")):
            last = line
    return last


# ───────────────────────────── предохранители ─────────────────────────────

def live_tick_pid(state_dir) -> int:
    """pid живого тика из `tick.lock` или 0: под живым тиком починок нет."""
    payload = state_io.holder(Path(state_dir))
    if not payload:
        return 0
    pid = _stamp(payload.get("pid"))
    return pid if pid > 0 and keeper_scan._alive(pid) else 0


def guard(symptom: dict, state: HealState) -> tuple[str, str] | None:
    """Предохранители до действия: `(код причины, пояснение)` или `None`, если можно.

    Порядок один и тот же в dry-run и в apply: `off` → белый список → живой тик →
    кулдаун → лимит. Проверка CLI (`no_hermes`) стоит не здесь, а внутри действия:
    dry-run ничего не запускает, и отсутствие `hermes` ему безразлично.
    """
    if state.mode == "off":
        return REASON_OFF, (f"kill-switch: {OFF_MARKER} в каталоге состояния —"
                            " починки выключены владельцем")
    kind = str(symptom.get("kind") or "")
    if kind not in ACTIONS:
        return REASON_BLACKLIST, (f"вид {kind or '?'} вне белого списка починок:"
                                  f" {', '.join(sorted(ACTIONS))}")
    pid = live_tick_pid(state.state_dir)
    if pid:
        return REASON_LIVE_TICK, f"тик идёт: {state_io.LOCK_NAME} держит живой pid {pid}"
    entry = last_entry(state.state_dir, str(symptom.get("symptom_id") or ""))
    if entry is not None:
        age = state.now - _stamp(entry.get("ts"))
        if age <= COOLDOWN_MINUTES * 60:
            return (REASON_COOLDOWN,
                    f"кулдаун: симптом чинился {age / 60:.0f} мин назад,"
                    f" предел {COOLDOWN_MINUTES:.0f} мин")
    heals = heals_in_window(state.state_dir, state.now)
    if heals >= MAX_HEALS_PER_HOUR:
        return REASON_LIMIT, (f"лимит починок {MAX_HEALS_PER_HOUR}/час исчерпан:"
                              f" за час запущено {heals}")
    return None


# ───────────────────────────── действия ─────────────────────────────

@dataclass
class Plan:
    """Что действие сделало бы: команда (пустая у `lock_clear`) и адрес панели."""

    command: str = ""
    argv: list[str] = field(default_factory=list)
    host: str = ""
    port: int = 0


def _read_json(path: Path) -> dict:
    """Объект из JSON-файла; нет файла или мусор — пустой словарь."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _int(value: object) -> int:
    """Целое из значения конфигурации или метки; мусор — 0."""
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return 0


def panel_target(cfg: config_mod.Config) -> tuple[str, int]:
    """Адрес панели: `ui.json` → `ui_port` конфига → 8770; хост `ui.json` → 127.0.0.1.

    Порядок тот же, что у `hermes pipeline ui`: команда и проба обязаны смотреть на
    одну пару host/port, иначе успешный перезапуск выглядел бы как `failed` и съел бы
    кулдаун на тот же симптом.
    """
    info = _read_json(Path(cfg.repo) / ".pipeline" / "ui.json")
    host = str(info.get("host") or "127.0.0.1")
    port = _int(info.get("port")) or _int((cfg.state or {}).get("ui_port")) or DEFAULT_UI_PORT
    return host, port


def plan_action(action: str, state: HealState) -> Plan:
    """Команда действия: одна и та же в dry-run (показать) и в apply (запустить)."""
    if action == "lock_clear":
        return Plan()
    if action == "panel_restart":
        host, port = panel_target(state.cfg)
        argv = [state.hermes, "pipeline", "ui", "--project", str(Path(state.cfg.repo)),
                "--detach", "--port", str(port), "--host", host]
        return Plan(command=" ".join(argv), argv=argv, host=host, port=port)
    argv = [state.hermes, "pipeline", "setup", "--project", str(Path(state.cfg.repo)),
            "--upgrade"]
    return Plan(command=" ".join(argv), argv=argv)


def _call(state: HealState, argv: list[str]):
    """Внешняя команда через подменяемый `run`; сбой запуска — `None`, не исключение."""
    try:
        return state.run(argv, RUN_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return None


def _tail(res) -> str:
    """Короткий хвост вывода команды для `detail`."""
    text = " ".join(((res.stderr or res.stdout) or "").split())
    return text[:120] or "без текста ошибки"


def clear_lock(state: HealState) -> tuple[str, str]:
    """Снять `tick.lock` с мёртвым pid; успех подтверждается повторным чтением замка."""
    path = Path(state.state_dir) / state_io.LOCK_NAME
    payload = state_io.holder(state.state_dir)
    if payload is None:
        return "failed", f"{state_io.LOCK_NAME} уже нет: снимать нечего"
    pid = _stamp(payload.get("pid"))
    if keeper_scan._alive(pid):
        return "failed", f"{state_io.LOCK_NAME} держит живой pid {pid}: чужой замок не снимаю"
    try:
        path.unlink()
    except FileNotFoundError:
        return "failed", f"{state_io.LOCK_NAME} исчез до снятия"
    except OSError as exc:
        return "failed", f"не смог снять {state_io.LOCK_NAME}: {exc}"
    if state_io.holder(state.state_dir) is not None:
        return "failed", f"{state_io.LOCK_NAME} остался после снятия: нужен человек"
    return "ok", f"{state_io.LOCK_NAME} мёртвого pid {pid or 'без pid'} снят"


def restart_panel(state: HealState, plan: Plan) -> tuple[str, str]:
    """Перезапустить панель и подтвердить успех пробой по той же паре host/port."""
    res = _call(state, plan.argv)
    if res is None:
        return "failed", "не смог запустить hermes pipeline ui"
    if res.returncode != 0:
        return "failed", f"hermes pipeline ui вышел с {res.returncode}: {_tail(res)}"
    if not state.probe(plan.host, plan.port):
        return "failed", f"код 0, но панель не отвечает на {plan.host}:{plan.port}"
    return "ok", f"панель отвечает на {plan.host}:{plan.port}"


def upgrade_engine(state: HealState, plan: Plan) -> tuple[str, str, bool]:
    """Обновить движок и роли; вторая и следующие попытки переиспользуют исход первой.

    Возврат — `(результат, деталь, запускалась ли команда)`: флаг `ran` уходит в
    журнал, и лимит в час считает только `ran: true` — три симптома одного
    обновления не съедают три слота из трёх (D7).
    """
    if state.upgrade is not None:
        outcome, detail = state.upgrade
        if outcome == "ok":
            return "ok", "выполнено тем же обновлением", False
        return "failed", f"тем же обновлением: {detail}", False
    res = _call(state, plan.argv)
    if res is None:
        result = ("failed", "не смог запустить hermes pipeline setup --upgrade")
    elif res.returncode != 0:
        result = ("failed", f"setup --upgrade вышел с {res.returncode}: {_tail(res)}")
    else:
        result = ("ok", "движок и роли приведены к пакету")
    state.upgrade = result
    return result[0], result[1], True


def _commit(state: HealState) -> str:
    """HEAD проекта через подменяемый `run`; git недоступен — пустая строка."""
    if state.commit is not None:
        return state.commit
    state.commit = ""
    res = _call(state, ["git", "-C", str(Path(state.cfg.repo)), "rev-parse", "HEAD"])
    if res is not None and res.returncode == 0:
        lines = (res.stdout or "").strip().splitlines()
        state.commit = lines[0].strip()[:40] if lines else ""
    return state.commit


def _journal_line(record: dict, state: HealState, ran: bool) -> dict:
    """Строка журнала: время, симптом, действие, `ran`, результат, коммит (D7)."""
    detail = " ".join(str(record["detail"]).split())[:DETAIL_LIMIT]
    return {
        "ts": int(state.now),
        "at": datetime.fromtimestamp(state.now).strftime("%d.%m.%Y %H:%M:%S"),
        "symptom_id": record["symptom_id"],
        "kind": record["kind"],
        "action": record["action"],
        "ran": bool(ran),
        "result": record["status"],
        "detail": detail,
        "commit": _commit(state),
    }


# ───────────────────────────── один симптом ─────────────────────────────

def _record(symptom: dict) -> dict:
    """Каркас строки отчёта: поля схемы §1.7 со значениями по умолчанию."""
    return {
        "symptom_id": str(symptom.get("symptom_id") or ""),
        "kind": str(symptom.get("kind") or ""),
        "address": str(symptom.get("address") or ""),
        "severity": str(symptom.get("severity") or ""),
        "action": "",
        "status": "refused",
        "reason": "",
        "detail": "",
        "command": "",
    }


def _refuse(record: dict, reason: str, detail: str) -> dict:
    """Отказ: права на действие нет, значит нет и команды."""
    record.update(status="refused", reason=reason, detail=detail)
    return record


def heal_one(symptom: dict, state: HealState) -> dict:
    """Один симптом: предохранители → dry-run (`planned`) → действие → журнал."""
    record = _record(symptom)
    blocked = guard(symptom, state)
    if blocked is not None:
        return _refuse(record, blocked[0], blocked[1])
    action = ACTIONS[record["kind"]]
    record["action"] = action
    plan = plan_action(action, state)
    record["command"] = plan.command
    if state.dry_run:
        record.update(status="planned",
                      detail=f"выполнил бы {action}" + (f": {plan.command}" if plan.command else ""))
        return record
    if action in COMMAND_ACTIONS and not state.hermes_found:
        return _refuse(record, REASON_NO_HERMES, "CLI hermes не найден в PATH")
    ran = True
    if action == "lock_clear":
        status, detail = clear_lock(state)
    elif action == "panel_restart":
        status, detail = restart_panel(state, plan)
    else:
        status, detail, ran = upgrade_engine(state, plan)
    record.update(status=status, detail=detail)
    if status in ("ok", "failed"):
        append_journal(state.state_dir, _journal_line(record, state, ran))
    return record


# ───────────────────────────── API шага ─────────────────────────────

def mode_of(state_dir, dry_run: bool | None = None) -> str:
    """Режим прогона по маркерам (D5): `off` сильнее `on`, без `on` — dry-run.

    `dry_run=True` принудительно включает dry-run; `dry_run=False` не делает apply
    вопреки маркерам — подтверждение владельца это файл `keeper.on`, а не флаг.
    """
    if marker_path(state_dir, OFF_MARKER).is_file():
        return "off"
    confirmed = marker_path(state_dir, ON_MARKER).is_file()
    return "apply" if confirmed and dry_run is not True else "dry-run"


def _order(symptoms: list[dict]) -> list[dict]:
    """Порядок `results`: severity по убыванию, внутри — вид, затем адрес."""
    return sorted(symptoms, key=lambda item: (SEVERITY_ORDER.get(str(item.get("severity")), 3),
                                              str(item.get("kind") or ""),
                                              str(item.get("address") or "")))


def heal(cfg: config_mod.Config | None = None, symptoms: list[dict] | None = None, *,
         plugin_dir=None, now: float | None = None, run=None, probe=None,
         hermes: str | None = None, dry_run: bool | None = None) -> dict:
    """Применить белый список к симптомам и отчитаться по каждому.

    `symptoms=None` — скан делается здесь же теми же зависимостями, что у сканера;
    переданный список берётся как есть (точка инъекции для тестов и для S5).
    `dry_run=None` — режим по маркерам; `True` — принудительный dry-run.
    `plugin_dir` — каталог пакета плагина: сканеру он нужен для источников версии и
    ролей, модулю — чтобы понимать, что пакет на месте.
    """
    if cfg is None:
        cfg = config_mod.load()
    moment = time.time() if now is None else float(now)
    package = keeper_scan._package_root(plugin_dir) if plugin_dir else None
    run_fn = keeper_scan._default_run if run is None else run
    probe_fn = keeper_scan._default_probe if probe is None else probe
    found = shutil.which("hermes") if hermes is None else (hermes or None)
    state_dir = Path(cfg.state_dir)
    state = HealState(
        cfg=cfg,
        state_dir=state_dir,
        plugin_dir=package,
        now=moment,
        mode=mode_of(state_dir, dry_run),
        hermes=found or "hermes",
        hermes_found=bool(found),
        run=run_fn,
        probe=probe_fn,
    )
    if symptoms is None:
        symptoms = keeper_scan.scan(cfg, plugin_dir=package, now=moment, run=run_fn,
                                    probe=probe_fn, hermes=hermes)["symptoms"]
    results = [heal_one(symptom, state) for symptom in _order(list(symptoms or []))]
    counts = {name: sum(1 for item in results if item["status"] == name) for name in STATUSES}
    return {
        "project": str(cfg.project),
        "repo": Path(cfg.repo).resolve().as_posix(),
        "healed_at": datetime.fromtimestamp(moment).strftime("%d.%m.%Y %H:%M:%S"),
        "healed_ts": int(moment),
        "mode": state.mode,
        "dry_run": state.dry_run,
        "results": results,
        "counts": counts,
        "journal": cfg.rel(journal_path(state_dir)),
    }


# ───────────────────────────── часовой обход (S4) ─────────────────────────────

def keeper_profile(cfg: config_mod.Config) -> str:
    """Исполнитель карточки обхода (D4): `profiles.keeper`, иначе `profiles.driver`.

    Поля для `profiles.keeper` в `Config` нет (Ф9), поэтому ключ читается из сырого
    `cfg._raw`. Имя, которого нет в `config_mod.known_profiles(cfg)`, — опечатка
    владельца: карточка с таким исполнителем не доставляется вовсе, поэтому берётся
    драйвер, а владелец видит строку в stderr. Пустой драйвер — `ConfigError`
    (код 2): карточки без исполнителя не бывает.
    """
    raw = cfg.state if isinstance(cfg.state, dict) else {}
    profiles = raw.get("profiles") if isinstance(raw.get("profiles"), dict) else {}
    value = (profiles or {}).get("keeper")
    name = value.strip() if isinstance(value, str) else ""
    driver = str(cfg.driver or "").strip()
    if not driver:
        raise config_mod.ConfigError(
            "нет исполнителя карточки смотрителя: profiles.driver не задан")
    if not name:
        return driver
    # Своё значение `known_profiles` тоже считает объявленным (оно лежит в `profiles`),
    # поэтому из набора вычитается вклад самого ключа: иначе проверка не проверяла бы
    # ничего и любая опечатка уходила бы в карточку несуществующему профилю.
    own = {str(item).strip() for key, item in (profiles or {}).items() if key == "keeper"}
    declared = (set(config_mod.known_profiles(cfg)) - own) | {driver}
    if name in declared:
        return name
    print(f"[keeper] профиль {name} из profiles.keeper не объявлен в конфиге"
          f" — карточка идёт на {driver}", file=sys.stderr)
    return driver


def keeper_runtime(cfg: config_mod.Config) -> str:
    """Потолок карточки: `runtimes.keeper` из сырого конфига, умолчание 30m (Ф9)."""
    raw = cfg.state if isinstance(cfg.state, dict) else {}
    runtimes = raw.get("runtimes") if isinstance(raw.get("runtimes"), dict) else {}
    value = (runtimes or {}).get("keeper")
    name = value.strip() if isinstance(value, str) else ""
    return name or DEFAULT_KEEPER_RUNTIME


def fresh_window(cfg: config_mod.Config) -> float:
    """Окно «работа идёт сейчас» в секундах: `watchdog.heartbeat_fresh_minutes`."""
    data = cfg.watchdog if isinstance(cfg.watchdog, dict) else {}
    minutes = _int(data.get("heartbeat_fresh_minutes"))
    return (minutes if minutes > 0 else DEFAULT_FRESH_MINUTES) * 60


def card_in_board(cfg: config_mod.Config, card_id: str) -> bool | None:
    """Есть ли карточка на борде: `True`/`False`, `None` — проверить нечем.

    Проверка повторяет урок `_create_card` стража: id из вывода CLI — ещё не
    карточка. Борды нет или она не читается — `None`: смотритель не выдумывает
    сбой из недоступной проверки, но и не выдаёт непроверенное за успех.
    """
    db = Path(cfg.kanban_db)
    if not card_id or not db.is_file():
        return None
    try:
        con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
        try:
            row = con.execute("SELECT 1 FROM tasks WHERE id = ?", (str(card_id),)).fetchone()
        finally:
            con.close()
    except sqlite3.Error:
        return None
    return row is not None


def conveyor_busy(cfg: config_mod.Config, now: float | None = None) -> tuple[bool, str]:
    """Идёт ли карточка конвейера прямо сейчас (D5): `(да/нет, описание для лога)`.

    «Идёт» — ровно `running` со свежим heartbeat не старше
    `watchdog.heartbeat_fresh_minutes`. `todo`/`ready`/`blocked` — очередь, а не
    работа: висящая карточка как раз и есть самый важный симптом смотрителя, и
    запрещать из-за неё обход значило бы терять находку. Считаются только карточки
    с ключом `planloop:`; `running` без heartbeat окно не включает.

    Борды нет или схема чужая — `(False, "")`: обход продолжается, а причину назовёт
    сканер источником `board`.
    """
    moment = time.time() if now is None else float(now)
    db = Path(cfg.kanban_db)
    if not db.is_file():
        return False, ""
    try:
        con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
        try:
            cards = keeper_scan._read_cards(con)
            have = {row[1] for row in con.execute("PRAGMA table_info(tasks)")}
            keys: dict[str, str] = {}
            if "idempotency_key" in have:
                for card_id, key in con.execute("SELECT id, idempotency_key FROM tasks"):
                    keys[str(card_id)] = str(key or "")
        finally:
            con.close()
    except sqlite3.Error:
        return False, ""
    window = fresh_window(cfg)
    for card in cards:
        card_id = str(card.get("id") or "")
        if not keys.get(card_id, "").startswith(PLANLOOP_KEY_PREFIX):
            continue
        if str(card.get("status") or "") != "running":
            continue
        heartbeat = _stamp(card.get("last_heartbeat_at")) or _stamp(card.get("last_event"))
        if heartbeat <= 0 or moment - heartbeat > window:
            continue
        age = max(0, int(moment - heartbeat))
        return True, f"{card_id} (running, heartbeat {age} с назад)"
    return False, ""


# ───────────────────────────── свой замок обхода ─────────────────────────────

def keeper_lock_holder(state_dir) -> dict | None:
    """Полезная нагрузка чужого `keeper.lock` (для строки пропуска)."""
    try:
        data = json.loads((Path(state_dir) / KEEPER_LOCK_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _keeper_payload() -> dict:
    """Нагрузка своего замка — та же форма, что пишет `state_io`."""
    return {"pid": os.getpid(), "host": socket.gethostname(),
            "at": time.strftime("%d.%m.%Y %H:%M:%S"), "at_ts": time.time(),
            "what": "keeper"}


def _keeper_lock_stale(payload: dict | None, now: float) -> bool:
    """Замок протух: возраст, чужой хост или мёртвый PID (как `state_io._stale`)."""
    if not payload:
        return True
    at_ts = _stamp(payload.get("at_ts"))
    if at_ts <= 0 or now - at_ts > KEEPER_LOCK_STALE:
        return True
    if str(payload.get("host") or "") != socket.gethostname():
        return True
    return not keeper_scan._alive(_stamp(payload.get("pid")))


def take_keeper_lock(state_dir, now: float | None = None) -> Path | None:
    """Взять свой замок обхода `keeper.lock`; `None` — держит живой чужой (D11).

    Примитив тот же, что у тика (`os.open` с `O_CREAT | O_EXCL`, без `msvcrt` и
    `fcntl`). Гонку за протухший замок проигравшая сторона не ждёт: прогон просто
    пропускается, следующий будет через час.
    """
    path = Path(state_dir) / KEEPER_LOCK_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    moment = time.time() if now is None else float(now)
    payload = _keeper_payload()
    for attempt in (1, 2):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            if attempt == 2 or not _keeper_lock_stale(keeper_lock_holder(state_dir), moment):
                return None
            try:
                path.unlink()
            except OSError:
                return None
            continue
        try:
            os.write(fd, json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"))
        except BaseException:
            os.close(fd)
            try:
                path.unlink()
            except OSError:
                pass
            raise
        os.close(fd)
        return path
    return None


def release_keeper_lock(path: Path | None) -> None:
    """Снять ТОЛЬКО свою запись: сверка pid и host, как у `state_io.release`."""
    if path is None:
        return
    path = Path(path)
    current = keeper_lock_holder(path.parent)
    if not current:
        return
    if _stamp(current.get("pid")) != os.getpid():
        return
    if str(current.get("host") or "") != socket.gethostname():
        return
    try:
        path.unlink()
    except OSError:
        pass


# ───────────────────────────── карточка смотрителя ─────────────────────────────

def _severity_counts(symptoms: list[dict]) -> dict:
    """Счёт по трём классам severity (схема §4.1)."""
    counts = {name: 0 for name in SEVERITY_ORDER}
    for item in symptoms:
        name = str(item.get("severity") or "")
        if name in counts:
            counts[name] += 1
    return counts


def card_title(cfg: config_mod.Config, symptoms: list[dict]) -> str:
    """Заголовок карточки: проект и счёт находок (одна строка борды)."""
    high = sum(1 for item in symptoms if str(item.get("severity")) == SEVERITY_HIGH)
    return f"[смотритель] {cfg.project}: симптомов {len(symptoms)} (high {high})"[:CARD_TITLE_LIMIT]


def card_key(cfg: config_mod.Config, symptoms: list[dict]) -> str:
    """Ключ идемпотентности: один и тот же набор находок не даёт вторую карточку.

    Хеш от набора `symptom_id`, а не от времени и не от `evidence` (D6): повторный
    обход того же простоя получает ту же карточку из борды, а растущий список
    изменённых путей новую не порождает.
    """
    ids = sorted(str(item.get("symptom_id") or "") for item in symptoms)
    digest = hashlib.sha256("|".join(ids).encode("utf-8")).hexdigest()[:16]
    return f"{KEEPER_KEY_PREFIX}{cfg.project}:{digest}"


def _heal_tail(item: dict) -> str:
    """Хвост строки результатов починки: причина и деталь."""
    reason = str(item.get("reason") or "")
    detail = str(item.get("detail") or "")
    head = f" (причина: {reason})" if reason else ""
    return f"{head}: {detail}" if detail else head


def card_body(cfg: config_mod.Config, healed: dict, symptoms: list[dict]) -> str:
    """Тело карточки: шапка, ПОЛНЫЙ JSON симптомов и строки результатов починки.

    JSON не усекается: карточка — единственный вход роли `pipeline-keeper`.
    """
    severity = _severity_counts(symptoms)
    lines = [
        "# Смотритель: находки часового обхода",
        "",
        f"- Проект: {cfg.project} (`{Path(cfg.repo).as_posix()}`)",
        f"- Режим починок: {healed.get('mode') or '—'}",
        f"- Находок: {len(symptoms)} (high {severity['high']},"
        f" normal {severity['normal']}, low {severity['low']})",
        f"- Журнал починок: `{healed.get('journal') or '—'}`",
        "",
        "Симптомы пришли из сканера: нет симптома — нет действия.",
        "",
        "## JSON симптомов",
        "",
        "```json",
        json.dumps(symptoms, ensure_ascii=False, indent=2),
        "```",
        "",
        "## Отчёт починок",
        "",
    ]
    results = healed.get("results") or []
    if not results:
        lines.append("- починок не было: симптомы вне белого списка или режим dry-run")
    for item in results:
        action = item.get("action") or "—"
        lines.append(f"- {item.get('kind')} {item.get('address')} → {action}:"
                     f" {item.get('status')}{_heal_tail(item)}")
    return "\n".join(lines) + "\n"


def create_card(cfg: config_mod.Config, healed: dict, symptoms: list[dict],
                state: HealState, profile: str) -> tuple[str, str]:
    """Одна карточка на прогон: `(id, пояснение)`.

    Отказ CLI и невидимая борде карточка не роняют обход: строка в stderr, пустой
    `card` в схеме, следующий час повторит с тем же ключом (идемпотентность CLI
    вернёт уже созданную карточку). Видимость проверяется один раз (D10).
    """
    key = card_key(cfg, symptoms)
    body_path = ""
    try:
        fd, body_path = tempfile.mkstemp(prefix="keeper-", suffix=".md")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(card_body(cfg, healed, symptoms))
        argv = [state.hermes, "kanban", "--board", cfg.board,
                "create", card_title(cfg, symptoms),
                "--body-file", body_path,
                "--assignee", profile, "--tenant", cfg.tenant,
                "--workspace", f"dir:{Path(cfg.repo).as_posix()}",
                "--idempotency-key", key,
                "--max-runtime", keeper_runtime(cfg),
                "--skill", KEEPER_SKILL]
        res = state.run(argv, RUN_TIMEOUT)
    except (OSError, subprocess.SubprocessError) as exc:
        tail = f"hermes kanban create не запустился: {str(exc)[:140]}"
        print(f"[card] не создано: {tail}", file=sys.stderr)
        return "", f"карточка не создана: {tail}"
    finally:
        if body_path:
            try:
                os.unlink(body_path)
            except OSError:
                pass
    out = ((res.stdout or "") + (res.stderr or "")).strip()
    match = re.search(r"t_[0-9a-f]{6,}", out)
    if res.returncode != 0 or not match:
        tail = " ".join(out.split())[:160] or "пустой вывод hermes"
        print(f"[card] не создано: {tail}", file=sys.stderr)
        return "", f"карточка не создана: {tail}"
    card_id = match.group(0)
    visible = card_in_board(cfg, card_id)
    if visible is False:
        print(f"[card] создано, но борды {cfg.board} её не знает: {card_id}", file=sys.stderr)
        return "", f"карточка {card_id} заказана, но борды {cfg.board} её не знает"
    detail = f"карточка {card_id} на профиле {profile}"
    if visible is None:
        detail += " (борда не читается — видимость не проверена)"
    return card_id, detail


# ───────────────────────────── сообщение владельцу ─────────────────────────────

def notify_owner(cfg: config_mod.Config, text: str) -> bool:
    """Единственный канал проекта (Ф5): ленивый `import gate` и `gate.notify`.

    Импорт ленивый: `gate` тянет полдвижка, а нужен он только при `high`. Отказ
    импорта и сбой доставки — строка в stderr и `False`, не исключение: обход не
    падает из-за канала, карточка на борде остаётся доказательством.
    """
    try:
        import gate as gate_mod  # noqa: PLC0415 - канал нужен только при high
    except Exception as exc:                        # noqa: BLE001 - канал не обязателен
        print(f"[notify] канал недоступен: {type(exc).__name__}: {exc}", file=sys.stderr)
        return False
    try:
        return bool(gate_mod.notify(cfg, text))
    except Exception as exc:                        # noqa: BLE001 - обход не падает
        print(f"[notify] сбой доставки: {type(exc).__name__}: {exc}", file=sys.stderr)
        return False


def escalation_text(cfg: config_mod.Config, findings: list[dict], card_id: str) -> str:
    """Одна строка владельцу: сколько high-симптомов, каких видов и где карточка."""
    kinds = [str(item.get("kind") or "") for item in findings]
    shown = ", ".join(kinds[:NOTIFY_HIGH_LIMIT])
    if len(kinds) > NOTIFY_HIGH_LIMIT:
        shown += f" и ещё {len(kinds) - NOTIFY_HIGH_LIMIT}"
    where = (f"карточка {card_id} (борда {cfg.board})" if card_id
             else "карточка не создана")
    text = f"смотритель {cfg.project}: high-симптомов {len(findings)} ({shown}), {where}"
    return text[:NOTIFY_TEXT_LIMIT]


def escalate(cfg: config_mod.Config, findings: list[dict], card_id: str,
             now: float) -> bool:
    """Одно сообщение владельцу за прогон и только вне окна паузы (D7).

    Пауза — про «не будить», а не про «не видеть»: карточка к этому моменту уже
    заведена, в лог уходит `[notify] отложено до ЧЧ:ММ`, `notified` остаётся ложным.
    """
    pause = config_mod.pause_state(cfg, now)
    if pause.get("paused"):
        print(f"[notify] отложено до {pause.get('until') or '—'}:"
              f" high-симптомов {len(findings)}", file=sys.stderr)
        return False
    return notify_owner(cfg, escalation_text(cfg, findings, card_id))


# ───────────────────────────── API часового обхода ─────────────────────────────

def _result_stub(cfg: config_mod.Config, moment: float) -> dict:
    """Каркас ответа по схеме §4.1: заказанные ключи на своих местах."""
    return {
        "project": str(cfg.project),
        "repo": Path(cfg.repo).resolve().as_posix(),
        "ran_at": datetime.fromtimestamp(moment).strftime("%d.%m.%Y %H:%M:%S"),
        "ran_ts": int(moment),
        "status": "skipped",
        "reason": "",
        "symptoms": 0,
        "severity": {"high": 0, "normal": 0, "low": 0},
        "card": "",
        "key": "",
        "profile": "",
        "mode": "",
        "heal": {},
        "notified": False,
        # Расширение схемы: готовая строка итога для истории cron (одна строка).
        "line": "",
        "detail": "",
    }


def _skip(result: dict, reason: str, detail: str) -> dict:
    """Пропуск прогона: причина окон, карточки нет, код 0."""
    result.update(status="skipped", reason=reason, detail=detail,
                  line=f"смотритель {result['project']}: пропуск — {detail}")
    return result


def _findings_line(cfg: config_mod.Config, result: dict, detail: str) -> str:
    """Строка итога при находках: счёт, режим и карточка (или причина отказа)."""
    severity = result.get("severity") or {}
    head = (f"смотритель {cfg.project}: симптомов {result.get('symptoms')}"
            f" (high {severity.get('high', 0)}), режим {result.get('mode') or '—'}")
    if result.get("card"):
        return f"{head}, карточка {result['card']} (ключ {result.get('key')})"
    return f"{head}, карточка не создана: {detail}"


def hourly(cfg: config_mod.Config | None = None, *, symptoms: list[dict] | None = None,
           plugin_dir=None, now: float | None = None, run=None, probe=None,
           hermes: str | None = None, dry_run: bool | None = None) -> dict:
    """Один часовой обход: окна → скан → починки белого списка → карточка → владелец.

    Правила шага, каждое проверяется своим тестом:

    * порядок до скана: свой замок `keeper.lock` → живой `tick.lock` → идущая
      карточка конвейера; пропуск — `status="skipped"`, причина в `reason`, код 0;
    * пустой скан — `status="idle"` и одна строка в лог: `heal` не зовётся вовсе,
      чтобы не создавать каталог состояния на целом проекте;
    * находки — `heal` теми же зависимостями, затем ОДНА карточка со скиллом
      `pipeline-keeper` на `profiles.keeper` (умолчание `profiles.driver`) и полным
      JSON симптомов; режим починок карточку не отменяет (`keeper.on` разрешает
      команды, а не извещение);
    * `severity=high` — не больше одного сообщения владельцу за прогон, вне окна
      паузы (`config.pause_state`).
    """
    if cfg is None:
        cfg = config_mod.load()
    moment = time.time() if now is None else float(now)
    package = keeper_scan._package_root(plugin_dir) if plugin_dir else None
    run_fn = keeper_scan._default_run if run is None else run
    probe_fn = keeper_scan._default_probe if probe is None else probe
    found = shutil.which("hermes") if hermes is None else (hermes or None)
    state_dir = Path(cfg.state_dir)
    result = _result_stub(cfg, moment)
    lock = take_keeper_lock(state_dir, moment)
    if lock is None:
        holder = keeper_lock_holder(state_dir) or {}
        return _skip(result, REASON_KEEPER_RUNNING,
                     f"другой смотритель уже идёт: {KEEPER_LOCK_NAME} держит"
                     f" живой pid {_stamp(holder.get('pid')) or '?'}")
    try:
        pid = live_tick_pid(state_dir)
        if pid:
            return _skip(result, REASON_TICK_RUNNING,
                         f"идёт тик стража: {state_io.LOCK_NAME} держит живой pid {pid}")
        busy, who = conveyor_busy(cfg, moment)
        if busy:
            return _skip(result, REASON_CONVEYOR_BUSY, f"идёт карточка конвейера {who}")
        result["profile"] = keeper_profile(cfg)
        state = HealState(cfg=cfg, state_dir=state_dir, plugin_dir=package, now=moment,
                          mode=mode_of(state_dir, dry_run), hermes=found or "hermes",
                          hermes_found=bool(found), run=run_fn, probe=probe_fn)
        if symptoms is None:
            symptoms = keeper_scan.scan(cfg, plugin_dir=package, now=moment, run=run_fn,
                                        probe=probe_fn, hermes=found)["symptoms"]
        symptoms = _order(list(symptoms or []))
        result["symptoms"] = len(symptoms)
        result["severity"] = _severity_counts(symptoms)
        if not symptoms:
            result.update(status="idle",
                          line=f"смотритель {cfg.project}: симптомов 0 — карточка не нужна")
            return result
        healed = heal(cfg, symptoms=symptoms, plugin_dir=package, now=moment,
                      run=run_fn, probe=probe_fn, hermes=found, dry_run=dry_run)
        result["heal"] = healed
        result["mode"] = str(healed.get("mode") or "")
        result["key"] = card_key(cfg, symptoms)
        card_id, detail = create_card(cfg, healed, symptoms, state, result["profile"])
        result.update(status="carded", card=card_id, detail=detail)
        result["line"] = _findings_line(cfg, result, detail)
        findings = [item for item in symptoms
                    if str(item.get("severity")) == SEVERITY_HIGH]
        if findings:
            result["notified"] = escalate(cfg, findings, card_id, moment)
        return result
    finally:
        release_keeper_lock(lock)


def format_hourly(result: dict) -> str:
    """Человеческий вывод обхода: ровно одна строка итога (история cron)."""
    line = str(result.get("line") or "").strip()
    return line or f"смотритель {result.get('project')}: {result.get('status')}"


def format_human(result: dict) -> str:
    """Человеческий вывод: шапка с режимом и счётчиками, строка на симптом."""
    counts = result.get("counts") or {}
    lines = [f"Проект {result.get('project')}: режим {result.get('mode')},"
             f" починок {counts.get('ok', 0)} (ok {counts.get('ok', 0)},"
             f" failed {counts.get('failed', 0)}, refused {counts.get('refused', 0)},"
             f" planned {counts.get('planned', 0)})"]
    for item in result.get("results") or []:
        action = item.get("action") or "—"
        lines.append(f"{item['status']}  {item['kind']}  {item['address']} → {action}:"
                     f" {item['detail']}")
    lines.append(f"журнал: {result.get('journal') or '—'}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """`--heal | --hourly [--dry-run] [--json] [--report PATH] [--plugin-dir] [--project]`.

    `--heal` — только починки белого списка (S3); `--hourly` — часовой обход
    целиком (S4): скан, починки, карточка, эскалация. Коды возврата как у сканера:
    `0` — прогон выполнен (отказы и сбои починок — строки отчёта, а не ошибка
    запуска), `2` — конфигурация не читается или аргументы противоречат друг другу,
    `1` — непойманный сбой.
    """
    parser = argparse.ArgumentParser(
        prog="keeper_heal.py", description="Починки смотрителя по белому списку")
    parser.add_argument("--heal", action="store_true",
                        help="выполнить починки белого списка (действие по умолчанию)")
    parser.add_argument("--hourly", action="store_true",
                        help="часовой обход: скан, починки, карточка, эскалация")
    parser.add_argument("--dry-run", action="store_true",
                        help="только показать, что было бы сделано")
    parser.add_argument("--json", action="store_true", help="машинный вывод")
    parser.add_argument("--report", default="", help="записать JSON отчёта в файл")
    parser.add_argument("--plugin-dir", default="", help="каталог пакета плагина")
    parser.add_argument("--project", default="", help="корень проекта")
    parser.add_argument("--hermes", default="", help="путь к CLI hermes")
    args = parser.parse_args(argv)
    if args.hourly and args.heal:
        print("--hourly и --heal взаимоисключающие: выбери один режим", file=sys.stderr)
        return 2
    if args.project:
        os.environ["PIPELINE_REPO"] = str(Path(args.project).expanduser().resolve())
    try:
        if args.hourly:
            result = hourly(plugin_dir=args.plugin_dir or None,
                            hermes=args.hermes or None,
                            dry_run=True if args.dry_run else None)
        else:
            result = heal(plugin_dir=args.plugin_dir or None, hermes=args.hermes or None,
                          dry_run=True if args.dry_run else None)
    except config_mod.ConfigError as exc:
        print(f"конфигурация не читается: {exc}", file=sys.stderr)
        return 2
    except Exception:                        # noqa: BLE001 - код 1: починки сломаны
        traceback.print_exc()
        return 1
    if args.report:
        state_io.atomic_write_text(Path(args.report).expanduser(),
                                   json.dumps(result, ensure_ascii=False, indent=2))
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(format_hourly(result) if args.hourly else format_human(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())

# -*- coding: utf-8 -*-
"""Починки смотрителя по белому списку, предохранители и часовой обход (план 10, S3–S4).

Модуль берёт симптомы сканера (`keeper_scan.py`) и применяет закрытую таблицу
«вид → действие» (`ACTIONS`): снять мёртвый `tick.lock`, перезапустить упавшую
панель, привести движок и роли к пакету через `hermes pipeline setup --upgrade`.
Всё, чего в таблице нет, остаётся эскалацией: починки «по смыслу» нет.

Часовой обход (`--hourly`, S4) — один вход для cron-джобы: скан, починки белого
списка, карточка со скиллом `pipeline-keeper` на профиль `profiles.keeper`
(умолчание — `profiles.driver`) с JSON симптомов и отчётом, сообщение владельцу
при находке `severity=high`. Пустой скан не заводит карточку вовсе — модель не
будится на целом проекте. Обход не идёт, пока держится живой `tick.lock` или на
борде есть нетерминальная карточка конвейера: две работы разом не запускаются.

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
import sqlite3
import ssl
import subprocess
import sys
import tempfile
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
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


# ─────────────────────── константы часового обхода (S4) ───────────────────────

#: Скилл карточки: вид работы различает он, а не профиль (S1).
KEEPER_SKILL = "pipeline-keeper"

#: Потолок карточки, если `runtimes.keeper` не задан: обход — не шаг плана.
DEFAULT_KEEPER_RUNTIME = "30m"

#: Ключ своих карточек: по нему обход отличает работу смотрителя от работы конвейера.
KEEPER_KEY_PREFIX = "keeper:"

#: Замок обхода: тот же примитив, что у тика (`state_io.acquire`), свой файл —
#: иначе обход, сняв мёртвый `tick.lock`, потерял бы собственный замок.
KEEPER_LOCK_NAME = "keeper.lock"

CARD_TITLE_LIMIT = 90       # заголовок карточки — одна строка борды
ESCALATION_LIMIT = 3        # сколько high-находок перечислять в сообщении владельцу

#: Каталог профилей Hermes; тесты подменяют его переменной, движку он нужен,
#: чтобы карточка не ушла несуществующему исполнителю.
PROFILES_ENV = "PIPELINE_PROFILES_HOME"

SEVERITY_HIGH = "high"

REASON_BUSY = "busy"
REASON_NO_PROFILE = "no_profile"
REASON_CARD = "card_failed"

#: Окно «работа идёт сейчас», если `watchdog.heartbeat_fresh_minutes` не задан:
#: то же умолчание, что в шаблоне `pipeline.json`.
DEFAULT_FRESH_MINUTES = 30


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

def profiles_home() -> Path:
    """Каталог профилей Hermes: `PIPELINE_PROFILES_HOME` → `HERMES_HOME` → LOCALAPPDATA."""
    explicit = (os.environ.get(PROFILES_ENV) or "").strip()
    if explicit:
        return Path(explicit)
    home = (os.environ.get("HERMES_HOME") or "").strip()
    if home:
        return Path(home) / "profiles"
    local = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(local) / "hermes" / "profiles"


def profile_exists(name: str) -> bool:
    """Профиль Hermes есть на диске: каталог `profiles/<имя>`.

    Цена ошибки — карточка с неизвестным исполнителем: канбан-диспетчер её молча
    не берёт, и она висит в `ready` навсегда. Поэтому проверка файловая, а не по
    `pipeline.json`: имя в конфиге может быть, а профиля в Hermes — нет.
    """
    if not name:
        return False
    return (profiles_home() / str(name)).is_dir()


def keeper_profile(cfg: config_mod.Config) -> str:
    """Исполнитель карточки смотрителя: `profiles.keeper` → `profiles.driver` (S1)."""
    raw = cfg.state if isinstance(cfg.state, dict) else {}
    profiles = raw.get("profiles") if isinstance(raw.get("profiles"), dict) else {}
    name = str((profiles or {}).get("keeper") or "").strip()
    return name or str(cfg.driver or "").strip()


def keeper_runtime(cfg: config_mod.Config) -> str:
    """Потолок карточки: `runtimes.keeper` → 30 минут."""
    raw = cfg.state if isinstance(cfg.state, dict) else {}
    runtimes = raw.get("runtimes") if isinstance(raw.get("runtimes"), dict) else {}
    name = str((runtimes or {}).get("keeper") or "").strip()
    return name or DEFAULT_KEEPER_RUNTIME


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


def fresh_window(cfg: config_mod.Config) -> float:
    """Окно «работа идёт сейчас» в секундах: `watchdog.heartbeat_fresh_minutes`."""
    data = cfg.watchdog if isinstance(cfg.watchdog, dict) else {}
    minutes = _int(data.get("heartbeat_fresh_minutes"))
    return (minutes if minutes > 0 else DEFAULT_FRESH_MINUTES) * 60


def live_pipeline_card(cfg: config_mod.Config, now: float) -> dict | None:
    """Карточка конвейера, по которой прямо сейчас идёт работа, или `None`.

    «Идёт работа» — событие карточки свежее окна `watchdog.heartbeat_fresh_minutes`:
    диспетчер только что её взял или исполнитель шлёт heartbeat. Просто
    нетерминальная карточка работой не считается: висящая карточка — как раз то,
    что смотритель обязан увидеть и назвать, а не повод молчать каждый час.

    Свои карточки (`idempotency_key` с префиксом `keeper:`) не считаются работой
    конвейера: иначе обход блокировал бы сам себя до закрытия своей же карточки.
    """
    db = Path(cfg.kanban_db)
    if not db.is_file():
        return None
    try:
        con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
        try:
            cards = keeper_scan._read_cards(con)
            try:
                keys = {str(cid): str(key or "") for cid, key in
                        con.execute("SELECT id, idempotency_key FROM tasks")}
            except sqlite3.Error:
                keys = {}                   # колонки нет — своих карточек не различить
        finally:
            con.close()
    except sqlite3.Error:
        return None
    window = fresh_window(cfg)
    for card in cards:
        if str(card.get("status") or "") in keeper_scan.TERMINAL_STATUSES:
            continue
        card_id = str(card.get("id"))
        if keys.get(card_id, "").startswith(KEEPER_KEY_PREFIX):
            continue
        last = max(_stamp(card.get("last_event")), _stamp(card.get("created_at")),
                   _stamp(card.get("started_at")), _stamp(card.get("last_heartbeat_at")))
        if last > 0 and now - last <= window:
            card["idempotency_key"] = keys.get(card_id, "")
            return card
    return None


def busy_reason(cfg: config_mod.Config, now: float) -> tuple[str, str] | None:
    """Почему обход сейчас не идёт: живой тик или живая карточка конвейера.

    Порядок один: сначала замок тика (он держится секунды), затем борда. Оба
    случая — «работа уже идёт», разница только в адресе для человека.
    """
    pid = live_tick_pid(cfg.state_dir)
    if pid:
        return REASON_BUSY, (f"идёт тик стража: {state_io.LOCK_NAME} держит живой"
                             f" pid {pid}")
    card = live_pipeline_card(cfg, now)
    if card is not None:
        title = " ".join(str(card.get("title") or "").split())[:60]
        return REASON_BUSY, (f"на борде {cfg.board} идёт карточка"
                             f" {card.get('id')} ({card.get('status')}): {title}")
    return None


def card_title(cfg: config_mod.Config, symptoms: list[dict]) -> str:
    """Заголовок карточки: сколько находок и в каком проекте (одна строка борды)."""
    high = sum(1 for item in symptoms if str(item.get("severity")) == SEVERITY_HIGH)
    tail = f", high {high}" if high else ""
    return (f"Смотритель: находок {len(symptoms)}{tail} — {cfg.project}")[:CARD_TITLE_LIMIT]


def card_key(cfg: config_mod.Config, symptoms: list[dict]) -> str:
    """Ключ идемпотентности: один и тот же набор находок не даёт вторую карточку.

    Хеш от набора `symptom_id`, а не от времени: повторный обход того же простоя
    получает ту же карточку из борды, а не соседнюю.
    """
    ids = sorted(str(item.get("symptom_id") or "") for item in symptoms)
    digest = hashlib.sha256("|".join(ids).encode("utf-8")).hexdigest()[:16]
    return f"{KEEPER_KEY_PREFIX}{cfg.project}:{digest}"


def card_body(cfg: config_mod.Config, result: dict, symptoms: list[dict]) -> str:
    """Тело карточки: JSON симптомов (его читает скилл) и отчёт починок."""
    counts = (result.get("heal") or {}).get("counts") or {}
    lines = [
        "# Смотритель: находки часового обхода",
        "",
        f"- Проект: {cfg.project} (`{Path(cfg.repo).as_posix()}`)",
        f"- Обход: {result.get('ran_at')}, режим {result.get('mode')}",
        f"- Находок: {len(symptoms)}; high:"
        f" {sum(1 for item in symptoms if str(item.get('severity')) == SEVERITY_HIGH)}",
        f"- Починки: ok {counts.get('ok', 0)}, failed {counts.get('failed', 0)},"
        f" refused {counts.get('refused', 0)}, planned {counts.get('planned', 0)}",
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
    for item in (result.get("heal") or {}).get("results") or []:
        action = item.get("action") or "—"
        lines.append(f"- {item.get('status')} {item.get('kind')}"
                     f" {item.get('address')} → {action}: {item.get('detail')}")
    lines += [
        "",
        "## Что дальше",
        "",
        "- Починка белого списка — `hermes pipeline keeper --heal`"
        " (журнал: `state/keeper.jsonl`).",
        "- Кодовый дефект движка — находка в план (`путь:строка`, доказательство),"
        " а не правка `engine/*.py`.",
        "- Шаг, закрытие шага и вердикт ревью — вне работы смотрителя.",
    ]
    return "\n".join(lines) + "\n"


def create_card(cfg: config_mod.Config, result: dict, symptoms: list[dict],
                state: HealState) -> tuple[str, str, str, str]:
    """Карточка смотрителя на борду: `(статус, пояснение, id, код причины)`.

    Исполнитель проверяется ДО вызова CLI: карточка с несуществующим профилем
    не заводится вовсе (диспетчер её молча не возьмёт), а причина уходит в отчёт
    и в сообщение владельцу — «отказ доставки» из стоп-условий шага.
    """
    profile = str(result.get("profile") or "")
    if not profile:
        return ("failed",
                "нет профиля: в конфигурации нет ни profiles.keeper, ни profiles.driver",
                "", REASON_NO_PROFILE)
    if not profile_exists(profile):
        return ("failed",
                f"профиль {profile} не найден в {profiles_home().as_posix()}:"
                f" карточка без исполнителя висела бы в ready — не завожу",
                "", REASON_NO_PROFILE)
    body_path = ""
    try:
        fd, body_path = tempfile.mkstemp(prefix="keeper-", suffix=".md")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(card_body(cfg, result, symptoms))
        argv = [state.hermes, "kanban", "--board", cfg.board,
                "create", card_title(cfg, symptoms),
                "--body-file", body_path,
                "--assignee", profile, "--tenant", cfg.tenant,
                "--workspace", f"dir:{Path(cfg.repo).as_posix()}",
                "--idempotency-key", card_key(cfg, symptoms),
                "--max-runtime", keeper_runtime(cfg),
                "--skill", KEEPER_SKILL]
        res = state.run(argv, RUN_TIMEOUT)
    except (OSError, subprocess.SubprocessError) as exc:
        return "failed", f"hermes kanban create не запустился: {str(exc)[:160]}", "", REASON_CARD
    finally:
        if body_path:
            try:
                os.unlink(body_path)
            except OSError:
                pass
    out = ((res.stdout or "") + (res.stderr or "")).strip()
    match = re.search(r"t_[0-9a-f]{6,}", out)
    if not match:
        return ("failed", f"карточку не создал: {out[:200] or 'пустой вывод hermes'}",
                "", REASON_CARD)
    card_id = match.group(0)
    exists = card_in_board(cfg, card_id)
    if exists is False:
        return ("failed",
                f"карточка {card_id} заказана, но борды {cfg.board} её не знает:"
                f" строки нет в {Path(cfg.kanban_db).name}", card_id, REASON_CARD)
    detail = f"карточка {card_id} на профиле {profile}"
    if exists is None:
        detail += " (борда не читается — видимость не проверена)"
    return "carded", detail, card_id, ""


def _unresolved_high(symptoms: list[dict], results: list[dict]) -> list[dict]:
    """High-находки, которые починка не сняла: повод для сообщения владельцу."""
    healed = {str(item.get("symptom_id") or "")
              for item in results if str(item.get("status")) == "ok"}
    return [item for item in symptoms
            if str(item.get("severity")) == SEVERITY_HIGH
            and str(item.get("symptom_id") or "") not in healed]


def escalation_text(cfg: config_mod.Config, findings: list[dict], card: dict) -> str:
    """Сообщение владельцу: вид, адрес и доказательство каждой high-находки."""
    parts = "; ".join(f"{item.get('kind')} {item.get('address')} —"
                      f" {str(item.get('evidence') or '')[:100]}" for item in findings[:ESCALATION_LIMIT])
    rest = len(findings) - ESCALATION_LIMIT
    if rest > 0:
        parts += f"; и ещё {rest}"
    where = (f"карточка {card.get('id')}" if card.get("id")
             else f"карточка не создана: {card.get('detail') or 'причина неизвестна'}")
    return (f"Смотритель {cfg.project}: находок high {len(findings)} — {parts}."
            f" Итог: {where}.")


#: Прокси Bot API — тот же набор переменных, что у стража (`gate.tg_api_base`).
TG_BASE_ENVS = ("STEP_PIPELINE_TELEGRAM_API_BASE", "TELEGRAM_API_BASE_URL")

#: Явные наборы корней: у хранилища ОС бывают просроченные CA (см. `gate.ca_bundle`).
CA_ENV_VARS = ("STEP_PIPELINE_CA_BUNDLE", "HERMES_CA_BUNDLE", "SSL_CERT_FILE",
               "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE")


def tg_api_base() -> str:
    """Хост Bot API: прокси из окружения, иначе прямой Telegram."""
    base = ""
    for name in TG_BASE_ENVS:
        base = (os.environ.get(name) or "").strip().rstrip("/")
        if base:
            break
    if not base:
        return "https://api.telegram.org"
    return base[: -len("/bot")] if base.endswith("/bot") else base


def _ssl_context() -> ssl.SSLContext:
    """Контекст TLS с явным набором корней, если он найден."""
    for name in CA_ENV_VARS:
        path = (os.environ.get(name) or "").strip()
        if path and os.path.isfile(path):
            return ssl.create_default_context(cafile=path)
    try:
        import certifi
    except ImportError:
        return ssl.create_default_context()
    return ssl.create_default_context(cafile=certifi.where())


def _default_notify(cfg: config_mod.Config, text: str) -> tuple[str, str]:
    """Сообщение владельцу в Telegram: `(статус, пояснение)`.

    Уведомления выключены — строка в stderr и исход `skipped`: cron с `--deliver
    local` покажет её в выводе, а карточка на борде остаётся доказательством.
    """
    notify_cfg = cfg.notify if isinstance(cfg.notify, dict) else {}
    prefix = str(notify_cfg.get("prefix") or "")
    message = f"{prefix}{text}" if prefix else text
    if not notify_cfg.get("enabled"):
        print(f"[notify] {message}", file=sys.stderr)
        return "skipped", "уведомления выключены (notify.enabled=false): строка в лог"
    token = os.environ.get(str(notify_cfg.get("token_env") or "PIPELINE_TG_TOKEN"))
    chat = os.environ.get(str(notify_cfg.get("chat_env") or "PIPELINE_TG_CHAT"))
    if not token or not chat:
        return "skipped", "нет токена или чата в окружении профиля: сообщение не отправлено"
    data = urllib.parse.urlencode({"chat_id": chat, "text": message,
                                   "disable_web_page_preview": "true"}).encode()
    request = urllib.request.Request(f"{tg_api_base()}/bot{token}/sendMessage", data=data)
    try:
        with urllib.request.urlopen(request, timeout=20, context=_ssl_context()) as resp:
            if 200 <= resp.status < 300:
                return "ok", "сообщение владельцу отправлено"
            return "failed", f"Bot API ответил {resp.status}"
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return "failed", f"сообщение не доставлено: {str(exc)[:160]}"


def notify_owner(cfg: config_mod.Config, text: str, notify=None) -> dict:
    """Эскалация владельцу через подменяемую функцию `(cfg, text) → (статус, деталь)`."""
    if not text:
        return {"status": "none", "detail": ""}
    fn = _default_notify if notify is None else notify
    try:
        outcome = fn(cfg, text)
    except Exception as exc:              # noqa: BLE001 - отказ доставки не роняет обход
        return {"status": "failed", "detail": f"сбой уведомления: {type(exc).__name__}: {exc}"}
    status, detail = (outcome if isinstance(outcome, tuple) else (str(outcome), ""))
    return {"status": str(status), "detail": str(detail)}


def run_hourly(cfg: config_mod.Config | None = None, symptoms: list[dict] | None = None, *,
               plugin_dir=None, now: float | None = None, run=None, probe=None,
               hermes: str | None = None, notify=None, dry_run: bool | None = None) -> dict:
    """Один часовой обход: скан → починки → карточка → эскалация.

    Правила шага, все проверяются тестами по отдельности:

    * маркер `state/keeper.off` — обхода нет вовсе, одна строка в лог;
    * занятый замок обхода (`keeper.lock`, тот же примитив, что у тика) — «другой
      смотритель уже идёт», пропуск: два обхода разом не запускаются;
    * живой `tick.lock` или живая карточка конвейера — пропуск: две работы разом
      не запускаются;
    * пустой скан — одна строка, ни карточки, ни сообщения: модель не будится;
    * находки — карточка со скиллом `pipeline-keeper` на `profiles.keeper`
      (умолчание `profiles.driver`), а несуществующий профиль — отказ без карточки;
    * `severity=high`, которую починка не сняла, — сообщение владельцу;
    * без `state/keeper.on` режим остаётся dry-run: ни команд, ни карточки, ни
      сообщения — только отчёт о том, что было бы сделано (S3/D5).
    """
    if cfg is None:
        cfg = config_mod.load()
    moment = time.time() if now is None else float(now)
    package = keeper_scan._package_root(plugin_dir) if plugin_dir else None
    run_fn = keeper_scan._default_run if run is None else run
    probe_fn = keeper_scan._default_probe if probe is None else probe
    found = shutil.which("hermes") if hermes is None else (hermes or None)
    state_dir = Path(cfg.state_dir)
    mode = mode_of(state_dir, dry_run)
    profile = keeper_profile(cfg)
    result: dict = {
        "project": str(cfg.project),
        "repo": Path(cfg.repo).resolve().as_posix(),
        "ran_at": datetime.fromtimestamp(moment).strftime("%d.%m.%Y %H:%M:%S"),
        "ran_ts": int(moment),
        "mode": mode,
        "dry_run": mode != "apply",
        "profile": profile,
        "status": "silent",
        "reason": "",
        "detail": "",
        "card": {"id": "", "profile": profile, "title": "", "key": "", "status": "",
                 "detail": ""},
        "escalated": {"status": "none", "detail": "", "text": ""},
        "symptoms": [],
        "heal": {},
        "lines": [],
    }
    if mode == "off":
        result.update(status="off", reason=REASON_OFF,
                      detail=f"kill-switch {OFF_MARKER}: смотритель выключен владельцем")
        result["lines"] = [f"смотритель {cfg.project}: выключен ({OFF_MARKER})"]
        return result
    lock = state_io.acquire(state_dir, what="keeper", wait=0, name=KEEPER_LOCK_NAME)
    if lock is None:
        payload = state_io.holder(state_dir, KEEPER_LOCK_NAME) or {}
        pid = _stamp(payload.get("pid"))
        result.update(status="skipped", reason=REASON_BUSY,
                      detail=(f"другой смотритель уже идёт: {KEEPER_LOCK_NAME} держит"
                              f" живой pid {pid or '?'}"))
        result["lines"] = [f"смотритель {cfg.project}: пропуск — {result['detail']}"]
        return result
    state = HealState(cfg=cfg, state_dir=state_dir, plugin_dir=package, now=moment,
                      mode=mode, hermes=found or "hermes", hermes_found=bool(found),
                      run=run_fn, probe=probe_fn)
    try:
        return _hourly_pass(state, symptoms, result, notify=notify, dry_run=dry_run)
    finally:
        state_io.release(lock)


def _hourly_pass(state: HealState, symptoms: list[dict] | None, result: dict, *,
                 notify, dry_run: bool | None) -> dict:
    """Тело обхода под взятым замком: окна, скан, починки, карточка, эскалация."""
    cfg = state.cfg
    moment = state.now
    mode = state.mode
    hermes = state.hermes if state.hermes_found else None
    blocked = busy_reason(cfg, moment)
    if blocked is not None:
        result.update(status="skipped", reason=blocked[0], detail=blocked[1])
        result["lines"] = [f"смотритель {cfg.project}: пропуск — {blocked[1]}"]
        return result

    if symptoms is None:
        scanned = keeper_scan.scan(cfg, plugin_dir=state.plugin_dir, now=moment,
                                   run=state.run, probe=state.probe, hermes=hermes)
        symptoms = scanned["symptoms"]
    symptoms = _order(list(symptoms or []))
    result["symptoms"] = symptoms
    if not symptoms:
        result["lines"] = [f"смотритель {cfg.project}: находок нет — чисто,"
                           f" карточки нет ({result['ran_at']})"]
        return result

    healed = heal(cfg, symptoms=symptoms, plugin_dir=state.plugin_dir, now=moment,
                  run=state.run, probe=state.probe, hermes=hermes, dry_run=dry_run)
    result["heal"] = {"mode": healed["mode"], "counts": healed["counts"],
                      "results": healed["results"], "journal": healed["journal"]}
    result["card"]["title"] = card_title(cfg, symptoms)
    result["card"]["key"] = card_key(cfg, symptoms)

    if mode != "apply":
        result.update(status="planned",
                      detail=f"dry-run: карточку на профиль {result['profile']} не завожу,"
                             f" внешних команд нет")
        result["card"].update(status="planned", detail=result["detail"])
    else:
        status, detail, card_id, reason = create_card(cfg, result, symptoms, state)
        result.update(status=status, detail=detail, reason=reason)
        result["card"].update(id=card_id, status=status, detail=detail)

    if mode == "apply":
        findings = _unresolved_high(symptoms, result["heal"]["results"])
        if findings:
            text = escalation_text(cfg, findings, result["card"])
            result["escalated"] = dict(notify_owner(cfg, text, notify), text=text)

    counts = result["heal"]["counts"]
    result["lines"] = [
        f"смотритель {cfg.project}: находок {len(symptoms)}, high"
        f" {sum(1 for item in symptoms if str(item.get('severity')) == SEVERITY_HIGH)},"
        f" починки ok {counts.get('ok', 0)}/failed {counts.get('failed', 0)}"
        f"/refused {counts.get('refused', 0)}; {result['status']}: {result['detail']}",
    ]
    for item in result["heal"]["results"]:
        result["lines"].append(f"  {item.get('status')} {item.get('kind')}"
                               f" {item.get('address')} → {item.get('action') or '—'}:"
                               f" {item.get('detail')}")
    if result["escalated"]["status"] != "none":
        result["lines"].append(f"  эскалация владельцу: {result['escalated']['status']}"
                               f" — {result['escalated']['detail']}")
    return result


def format_hourly(result: dict) -> str:
    """Человеческий вывод обхода: строки отчёта, по строке на находку."""
    lines = list(result.get("lines") or [])
    if not lines:
        lines = [f"смотритель {result.get('project')}: {result.get('status')}"]
    return "\n".join(lines)


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
    запуска), `2` — конфигурация не читается, `1` — непойманный сбой.
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
    if args.project:
        os.environ["PIPELINE_REPO"] = str(Path(args.project).expanduser().resolve())
    try:
        if args.hourly:
            result = run_hourly(plugin_dir=args.plugin_dir or None,
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

# -*- coding: utf-8 -*-
"""Починки смотрителя по белому списку и предохранители (план 10, S3).

Модуль берёт симптомы сканера (`keeper_scan.py`) и применяет закрытую таблицу
«вид → действие» (`ACTIONS`): снять мёртвый `tick.lock`, перезапустить упавшую
панель, привести движок и роли к пакету через `hermes pipeline setup --upgrade`.
Всё, чего в таблице нет, остаётся эскалацией: починки «по смыслу» нет.

Запускают его cron-часовой (S4) командой `hermes pipeline keeper --heal` и роль
`pipeline-keeper`, которая читает журнал. Своих решений о шагах, планах и карточках
модуль не принимает: вердикт и журнал шага — не работа смотрителя.

Что модуль пишет: только журнал `keeper.jsonl` и (по команде человека) маркеры
`keeper.on`/`keeper.off` в каталоге состояния проекта. Единственный файл, который он
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
import json
import os
import shutil
import subprocess
import sys
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
    """`--heal [--dry-run] [--json] [--plugin-dir PATH] [--project PATH] [--hermes PATH]`.

    Коды возврата как у сканера: `0` — прогон выполнен (отказы и сбои починок — строки
    отчёта, а не ошибка запуска), `2` — конфигурация не читается, `1` — непойманный сбой.
    """
    parser = argparse.ArgumentParser(
        prog="keeper_heal.py", description="Починки смотрителя по белому списку")
    parser.add_argument("--heal", action="store_true",
                        help="выполнить починки белого списка (действие по умолчанию)")
    parser.add_argument("--dry-run", action="store_true",
                        help="только показать, что было бы сделано")
    parser.add_argument("--json", action="store_true", help="машинный вывод")
    parser.add_argument("--plugin-dir", default="", help="каталог пакета плагина")
    parser.add_argument("--project", default="", help="корень проекта")
    parser.add_argument("--hermes", default="", help="путь к CLI hermes")
    args = parser.parse_args(argv)
    if args.project:
        os.environ["PIPELINE_REPO"] = str(Path(args.project).expanduser().resolve())
    try:
        result = heal(plugin_dir=args.plugin_dir or None, hermes=args.hermes or None,
                      dry_run=True if args.dry_run else None)
    except config_mod.ConfigError as exc:
        print(f"конфигурация не читается: {exc}", file=sys.stderr)
        return 2
    except Exception:                        # noqa: BLE001 - код 1: починки сломаны
        traceback.print_exc()
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2) if args.json else format_human(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())

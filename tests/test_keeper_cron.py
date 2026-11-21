# -*- coding: utf-8 -*-
"""Часовой обход смотрителя и cron-джоба (план 10, S4).

Доказательство приёмки шага: пустой скан не создаёт карточку, находка создаёт ровно
одну, пересечение с тиком стража и идущей карточкой конвейера блокирует запуск, а
карточка получает существующего исполнителя, скилл `pipeline-keeper` и полный JSON
симптомов. Внешние команды, живость панели и отправка сообщения подменяются: ни один
кейс не запускает настоящий `hermes` и не пишет в живой профиль.
"""
from __future__ import annotations

import json
import os
import socket
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from helpers import install_project, stub_cards  # noqa: F401 - фикстура `project`
from step_pipeline import setup_flow as setup_flow_mod
from step_pipeline.setup_flow import KEEPER_CRON, KEEPER_JOB, KEEPER_SCRIPT

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / "step_pipeline" / "engine"
sys.path.insert(0, str(ENGINE))

import config as config_mod  # noqa: E402 - движок на sys.path
import keeper_heal  # noqa: E402
import keeper_scan  # noqa: E402

#: Фиксированный момент замера: 20.11.2026, 12:00 MSK. Часы берутся из `now`, а не из
#: `time.time()`: тест не флакует на границе минуты и суток.
NOW = 1795164000.0

#: Схема «живой» борды (как `test_keeper_scan.BOARD_SCHEMA`): окну D5 нужен
#: `last_heartbeat_at`, а поддельной борде `helpers.KANBAN_SCHEMA` этой колонки нет.
BOARD_SCHEMA = """
CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT, status TEXT, assignee TEXT,
                    created_at INTEGER, started_at INTEGER, completed_at INTEGER,
                    last_heartbeat_at INTEGER, idempotency_key TEXT, tenant TEXT);
CREATE TABLE task_events (task_id TEXT, run_id TEXT, kind TEXT, payload TEXT,
                          created_at INTEGER);
CREATE TABLE task_runs (task_id TEXT, status TEXT, last_heartbeat_at INTEGER,
                        started_at INTEGER, ended_at INTEGER);
"""


# ───────────────────────────── помощники ─────────────────────────────

class FakeRun:
    """Подмена внешних команд: пишет вызовы, читает тело карточки, отдаёт id."""

    def __init__(self, create_out: str = "t_abc12345", rc: int = 0, on_call=None) -> None:
        self.calls: list[list[str]] = []
        self.bodies: list[str] = []
        self.create_out = create_out
        self.rc = rc
        self.on_call = on_call

    def __call__(self, cmd, timeout):
        cmd = list(cmd)
        self.calls.append(cmd)
        if "--body-file" in cmd:
            path = Path(cmd[cmd.index("--body-file") + 1])
            self.bodies.append(path.read_text(encoding="utf-8") if path.is_file() else "")
        if self.on_call is not None:
            self.on_call(cmd)
        out = ""
        if "create" in cmd:
            out = self.create_out
        elif "rev-parse" in cmd:
            out = "deadbeefcafe"
        return SimpleNamespace(returncode=self.rc, stdout=out, stderr="")

    def creates(self) -> list[list[str]]:
        return [call for call in self.calls if "create" in call]


class Result:
    """Ответ поддельного `hermes` для мастера установки."""

    def __init__(self, stdout: str = "", returncode: int = 0) -> None:
        self.stdout = stdout
        self.stderr = ""
        self.returncode = returncode


def _stub_setup(monkeypatch, tmp_path: Path, listing: str = ""):
    """Подмена профилей и CLI: мастер пишет скрипты во временный каталог."""
    home = tmp_path / "home"
    monkeypatch.setattr(setup_flow_mod.proj, "profile_dir", lambda p: home / "profiles" / p)
    calls: list[tuple] = []

    def run(*argv, timeout=180):
        calls.append(argv)
        return Result(stdout=listing if argv[-1] == "list" else "")

    monkeypatch.setattr(setup_flow_mod.proj, "run_hermes", run)
    return home, calls


#: Переменные, которых у cron-джобы быть не должно: они увели бы прогон в чужой проект.
_PROBE_DROP = ("PIPELINE_REPO", "PIPELINE_STATE_DIR", "PIPELINE_CONFIG", "PIPELINE_BOARD",
               "PIPELINE_ENV", "PIPELINE_PROFILES_HOME", "TELEGRAM_API_BASE_URL",
               "TELEGRAM_API_BASE_FILE_URL")


def _cron_env(**extra: str) -> dict[str, str]:
    """Окружение как у cron-джобы: без переменных конвейера и маркеров профиля."""
    env = {key: value for key, value in os.environ.items()
           if key not in _PROBE_DROP
           and not key.startswith("STEP_PIPELINE_TELEGRAM_")}
    env.update(extra)
    return env


def _install_script(tmp_path: Path, driver: str = "demo") -> Path:
    """Текст `KEEPER_SCRIPT` в структуре `profiles/<профиль>/scripts/`."""
    scripts = tmp_path / "profiles" / driver / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    target = scripts / "pipeline_keeper.py"
    target.write_text(KEEPER_SCRIPT, encoding="utf-8")
    return target


def _run_script(script: Path, cwd: Path, **extra: str) -> subprocess.CompletedProcess:
    """Запуск установленного скрипта отдельным процессом, как это делает cron."""
    return subprocess.run([sys.executable, str(script)], cwd=str(cwd), env=_cron_env(**extra),
                          capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=300)


def make_symptom(project: str, kind: str, address: str = "addr",
                 evidence: str = "доказательство") -> dict:
    """Симптом по схеме сканера: severity и healable берутся из `KIND_RULES`."""
    severity, healable = keeper_scan.KIND_RULES[kind]
    return {
        "project": str(project),
        "symptom_id": keeper_scan.symptom_id(str(project), kind, address),
        "kind": kind,
        "severity": severity,
        "address": address,
        "evidence": evidence,
        "healable": bool(healable),
    }


def _hourly(project: Path, symptoms: list[dict], run=None, **kw) -> dict:
    """Один обход с подменёнными внешними зависимостями и фиксированным `now`."""
    cfg = config_mod.load(project)
    return keeper_heal.hourly(cfg, symptoms=symptoms, now=NOW,
                              run=run if run is not None else FakeRun(),
                              probe=lambda host, port: True, hermes="/fake/hermes", **kw)


def _edit_config(project: Path, **updates) -> None:
    """Дописать ключи верхнего уровня в `pipeline.json` проекта."""
    path = project / "pipeline.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data.update(updates)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _set_profiles(project: Path, **profiles) -> None:
    """Дописать `profiles` в `pipeline.json` проекта."""
    path = project / "pipeline.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data.setdefault("profiles", {}).update(profiles)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


@pytest.fixture()
def board(tmp_path, monkeypatch) -> Path:
    """Борда «живой» схемы: `last_heartbeat_at` и таблицы событий на месте."""
    db = tmp_path / "board.db"
    monkeypatch.setenv("PIPELINE_KANBAN_DB", str(db))
    return db


def write_board(db: Path, *cards: dict) -> Path:
    """Поддельная борда живой схемы: карточка — словарь полей `tasks`."""
    con = sqlite3.connect(db)
    try:
        con.executescript(BOARD_SCHEMA)
        for card in cards:
            row = {"id": card["id"], "title": card.get("title", "карточка"),
                   "status": card.get("status", "todo"), "assignee": card.get("assignee", "demo"),
                   "created_at": card.get("created_at", int(NOW)),
                   "started_at": card.get("started_at", 0), "completed_at": 0,
                   "last_heartbeat_at": card.get("last_heartbeat_at", 0),
                   "idempotency_key": card.get("key", ""), "tenant": "plan"}
            con.execute("INSERT INTO tasks (" + ", ".join(row) + ") VALUES ("
                        + ", ".join("?" * len(row)) + ")", tuple(row.values()))
        con.commit()
    finally:
        con.close()
    return db


def write_lock(project: Path, name: str, payload: dict) -> Path:
    """Положить файл блокировки в каталог состояния проекта."""
    path = project / ".pipeline" / "state" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


# ───────────────────────────── джоба и скрипт (мастер) ─────────────────────────────

def test_keeper_cron_writes_script_and_creates_job(tmp_path: Path, monkeypatch):
    """Т1: скрипт лежит в профиле, джоба одна — `5 * * * *`, no-agent, доставка local."""
    home, calls = _stub_setup(monkeypatch, tmp_path)
    project = tmp_path / "shop"
    project.mkdir()

    setup_flow_mod.ensure_keeper_cron("shop", project, setup_flow_mod.Log(), skip=False)

    script = home / "profiles" / "shop" / "scripts" / "pipeline_keeper.py"
    assert script.is_file()
    text = script.read_text(encoding="utf-8")
    assert "keeper_heal.py" in text and "--hourly" in text and "load_profile_env" in text

    created = [call for call in calls if "create" in call]
    assert len(created) == 1, created
    cron = created[0]
    assert KEEPER_CRON in cron
    assert cron[cron.index("--name") + 1] == KEEPER_JOB
    assert "pipeline_keeper.py" in cron and "--no-agent" in cron
    assert cron[cron.index("--deliver") + 1] == "local"
    assert cron[cron.index("--workdir") + 1] == str(project)


def test_keeper_cron_is_idempotent_and_rewrites_script(tmp_path: Path, monkeypatch):
    """Т2: второй прогон не заводит джобу, но перезаписывает испорченный скрипт."""
    home, calls = _stub_setup(monkeypatch, tmp_path, listing=f"{KEEPER_JOB}   5 * * * *   local")
    project = tmp_path / "shop"
    project.mkdir()
    scripts = home / "profiles" / "shop" / "scripts"
    scripts.mkdir(parents=True)
    installed = scripts / "pipeline_keeper.py"
    installed.write_text("# старая рукописная копия\n", encoding="utf-8")

    setup_flow_mod.ensure_keeper_cron("shop", project, setup_flow_mod.Log(), skip=False)

    assert installed.read_text(encoding="utf-8") == KEEPER_SCRIPT
    assert not [call for call in calls if "create" in call], calls


def test_keeper_cron_respects_no_cron(tmp_path: Path, monkeypatch):
    """Т3: `--no-cron` — обещание не заводить джобы: ни вызовов, ни файла."""
    home, calls = _stub_setup(monkeypatch, tmp_path)
    project = tmp_path / "shop"
    project.mkdir()

    setup_flow_mod.ensure_keeper_cron("shop", project, setup_flow_mod.Log(), skip=True)

    assert calls == []
    assert not (home / "profiles" / "shop" / "scripts" / "pipeline_keeper.py").exists()


def test_cron_all_includes_keeper(tmp_path: Path, monkeypatch):
    """Т4: мастер заводит обход вместе со стражем; при `--no-cron` — метка `skipped`."""
    _home, calls = _stub_setup(monkeypatch, tmp_path)
    project = install_project(tmp_path / "shop")

    setup_flow_mod.ensure_cron_all("shop", project, setup_flow_mod.Log(), skip=False)

    names = [call[call.index("--name") + 1] for call in calls if "create" in call]
    assert KEEPER_JOB in names, names

    silent_calls: list[tuple] = []
    monkeypatch.setattr(setup_flow_mod.proj, "run_hermes",
                        lambda *argv, timeout=180: silent_calls.append(argv) or Result())
    other = install_project(tmp_path / "quiet")
    setup_flow_mod.ensure_cron_all("shop", other, setup_flow_mod.Log(), skip=True)

    assert silent_calls == []
    marker = json.loads((other / ".pipeline" / "cron.json").read_text(encoding="utf-8"))
    assert marker.get("skipped") is True, marker


# ───────────────────────────── установленный скрипт (субпроцесс) ─────────────────────────────

def test_hourly_empty_scan_creates_no_card(project: Path, hermes_stub: Path, tmp_path: Path):
    """Т5: чистый проект — код 0, ни одной карточки, строка «симптомов 0»."""
    script = _install_script(tmp_path)

    done = _run_script(script, project)

    assert done.returncode == 0, done.stderr
    assert stub_cards(hermes_stub) == []
    assert "симптомов 0" in done.stdout, done.stdout + done.stderr


def test_keeper_script_reports_missing_engine(tmp_path: Path):
    """Т6: проекта нет — код 1 и причина в stderr, а не зелёный «ok» на мёртвом обходе."""
    loner = tmp_path / "probe" / "scripts" / "pipeline_keeper.py"
    loner.parent.mkdir(parents=True)
    loner.write_text(KEEPER_SCRIPT, encoding="utf-8")
    empty = tmp_path / "empty"
    empty.mkdir()

    done = _run_script(loner, empty)

    assert done.returncode == 1, done.stdout + done.stderr
    assert "keeper_heal.py не найден" in done.stderr, done.stderr


# ───────────────────────────── обход (движок напрямую) ─────────────────────────────

def test_hourly_finding_creates_exactly_one_card(project: Path, hermes_stub: Path,
                                                 tmp_path: Path):
    """Т7: мёртвый `tick.lock` — ровно одна карточка на драйвера, `30m`, ключ `keeper:demo:`."""
    script = _install_script(tmp_path)
    write_lock(project, "tick.lock", {"pid": 999999, "host": socket.gethostname(),
                                      "at_ts": time.time()})

    done = _run_script(script, project)

    assert done.returncode == 0, done.stdout + done.stderr
    cards = stub_cards(hermes_stub)
    assert len(cards) == 1, cards
    assert cards[0]["assignee"] == "demo"
    assert str(cards[0]["key"]).startswith("keeper:demo:")
    assert cards[0]["max_runtime"] == "30m"
    assert "симптомов 1" in done.stdout, done.stdout + done.stderr


def test_hourly_card_carries_skill_and_symptom_json(project: Path):
    """Т8: в команде `create` есть скилл, исполнитель и ключ, в теле — JSON симптома."""
    run = FakeRun()
    symptom = make_symptom("demo", "card_stuck", address="t_be793ad3",
                           evidence="карточка висит 152.0 ч")

    result = _hourly(project, [symptom], run=run)

    assert result["status"] == "carded"
    create = run.creates()[0]
    assert create[create.index("--skill") + 1] == keeper_heal.KEEPER_SKILL
    assert create[create.index("--assignee") + 1] == "demo"
    assert create[create.index("--idempotency-key") + 1] == result["key"]
    assert str(result["key"]).startswith("keeper:demo:")
    body = run.bodies[0]
    assert symptom["symptom_id"] in body and '"kind"' in body


def test_hourly_skips_while_conveyor_card_runs(project: Path, board: Path):
    """Т9: идущая карточка конвейера (`running` со свежим heartbeat) — пропуск."""
    write_board(board, {"id": "t_run111", "status": "running", "key": "planloop:1:S1:step",
                        "last_heartbeat_at": int(NOW)})
    run = FakeRun()

    result = _hourly(project, [make_symptom("demo", "card_stuck")], run=run)

    assert result["status"] == "skipped"
    assert result["reason"] == "conveyor_busy"
    assert "t_run111" in result["line"]
    assert run.creates() == []


def test_hourly_runs_when_running_card_is_stale(project: Path, board: Path):
    """Т10: heartbeat старше окна и очередь `todo` работой не считаются (граница D5)."""
    write_board(board,
                {"id": "t_stale1", "status": "running", "key": "planloop:1:S1:step",
                 "last_heartbeat_at": int(NOW - 5 * 3600)},
                {"id": "t_todo01", "status": "todo", "key": "planloop:1:S2:step",
                 "created_at": int(NOW)})
    run = FakeRun()

    result = _hourly(project, [make_symptom("demo", "card_stuck")], run=run)

    assert result["status"] == "carded"
    assert len(run.creates()) == 1


def test_hourly_skips_on_live_tick_lock(project: Path):
    """Т11: живой держатель `tick.lock` — пропуск с причиной `tick_running`."""
    write_lock(project, "tick.lock", {"pid": os.getpid(), "host": socket.gethostname(),
                                      "at_ts": NOW, "what": "run"})
    run = FakeRun()

    result = _hourly(project, [make_symptom("demo", "card_stuck")], run=run)

    assert result["status"] == "skipped"
    assert result["reason"] == "tick_running"
    assert run.calls == []


def test_hourly_key_is_stable_and_symptom_dependent(project: Path):
    """Т12: ключ — от набора `symptom_id`: тот же набор тот же ключ, иной — другой."""
    symptom = make_symptom("demo", "card_stuck", address="t_be793ad3")

    first = _hourly(project, [symptom])
    second = _hourly(project, [dict(symptom)])
    changed = dict(symptom, symptom_id="0" * 16)
    third = _hourly(project, [changed])

    assert first["key"] == second["key"]
    assert third["key"] != first["key"]


def test_keeper_profile_falls_back_to_driver(project: Path, capsys):
    """Т13: неизвестное имя из `profiles.keeper` — драйвер плюс строка в stderr."""
    _set_profiles(project, keeper="demo")
    assert keeper_heal.keeper_profile(config_mod.load(project)) == "demo"

    _set_profiles(project, keeper="ghost")
    cfg = config_mod.load(project)
    assert keeper_heal.keeper_profile(cfg) == cfg.driver
    assert "[keeper]" in capsys.readouterr().err

    _set_profiles(project, keeper="")
    assert keeper_heal.keeper_profile(config_mod.load(project)) == cfg.driver


def test_high_severity_notifies_once_and_respects_pause(project: Path, monkeypatch, capsys):
    """Т14: один вызов владельцу на прогон; в окне паузы — ни одного (`отложено до`)."""
    calls: list[str] = []
    monkeypatch.setattr(keeper_heal, "notify_owner",
                        lambda cfg, text: calls.append(text) or True)
    symptom = make_symptom("demo", "tick_lock_dead", address="state/tick.lock")
    high = make_symptom("demo", "card_stuck", address="t_be793ad3")

    result = _hourly(project, [symptom, high])

    assert result["status"] == "carded"
    assert result["notified"] is True
    assert len(calls) == 1
    assert str(result["card"]) in calls[0]

    _edit_config(project, quiet_hours={"enabled": True,
                                       "windows": [{"from": "00:00", "to": "23:59"}]})
    calls.clear()
    capsys.readouterr()

    quiet = _hourly(project, [symptom])

    assert quiet["notified"] is False
    assert calls == []
    assert "отложено до" in capsys.readouterr().err


def test_hourly_skips_on_second_keeper(project: Path):
    """Т15: свой замок обхода держит второй прогон; мёртвый замок перезаписывается."""
    write_lock(project, keeper_heal.KEEPER_LOCK_NAME,
               {"pid": os.getpid(), "host": socket.gethostname(), "at_ts": NOW,
                "what": "keeper"})
    run = FakeRun()

    blocked = _hourly(project, [make_symptom("demo", "card_stuck")], run=run)

    assert blocked["status"] == "skipped"
    assert blocked["reason"] == "keeper_running"
    assert run.creates() == []

    lock_path = write_lock(project, keeper_heal.KEEPER_LOCK_NAME,
                           {"pid": 999999, "host": socket.gethostname(), "at_ts": NOW,
                            "what": "keeper"})
    seen: list[dict] = []

    def read_lock(_cmd):
        seen.append(json.loads(lock_path.read_text(encoding="utf-8")))

    passed = _hourly(project, [make_symptom("demo", "card_stuck")], run=FakeRun(on_call=read_lock))

    assert passed["status"] == "carded"
    assert seen and seen[0]["pid"] == os.getpid()
    assert not lock_path.exists(), "свой замок обязан сниматься в finally"

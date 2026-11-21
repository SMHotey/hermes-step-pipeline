# -*- coding: utf-8 -*-
"""Часовой обход смотрителя и cron-джоба (план 10, S4).

Проверяется контракт шага, а не факт наличия функций: пустой скан не заводит
карточку, находка заводит ровно одну, живой тик и карточка конвейера блокируют
запуск, карточка уходит существующему профилю (иначе канбан-диспетчер её молча
не возьмёт) и получает скилл `pipeline-keeper`, high-находка даёт сообщение
владельцу. Внешние команды, живость панели и отправка сообщения подменяются:
тесты не пишут в живую борду и не ходят в сеть.
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

from helpers import install_project  # noqa: F401 - фикстура `project` из conftest
from step_pipeline import setup_flow as setup_flow_mod
from step_pipeline.setup_flow import KEEPER_CRON, KEEPER_JOB

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / "step_pipeline" / "engine"
sys.path.insert(0, str(ENGINE))

import config as config_mod  # noqa: E402 - движок на sys.path
import keeper_heal  # noqa: E402
import keeper_scan  # noqa: E402

#: Фиксированный момент замера: 20.11.2026, 12:00 MSK.
NOW = 1795164000.0

#: Схема борды: смотрителю нужны `id`, `status`, `title` и ключ идемпотентности.
BOARD_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY, title TEXT, status TEXT, assignee TEXT,
    idempotency_key TEXT, created_at INTEGER
);
"""


# ───────────────────────────── помощники ─────────────────────────────

class FakeRun:
    """Подмена внешних команд: пишет вызовы, читает тело карточки, отдаёт id."""

    def __init__(self, create_out: str = "t_abc12345", rc: int = 0) -> None:
        self.calls: list[list[str]] = []
        self.bodies: list[str] = []
        self.create_out = create_out
        self.rc = rc

    def __call__(self, cmd, timeout):
        cmd = list(cmd)
        self.calls.append(cmd)
        if "--body-file" in cmd:
            path = Path(cmd[cmd.index("--body-file") + 1])
            self.bodies.append(path.read_text(encoding="utf-8") if path.is_file() else "")
        out = ""
        if "create" in cmd:
            out = self.create_out
        elif "rev-parse" in cmd:
            out = "deadbeefcafe"
        return SimpleNamespace(returncode=self.rc, stdout=out, stderr="")

    def creates(self) -> list[list[str]]:
        return [call for call in self.calls if "create" in call]


class Notifier:
    """Подмена сообщения владельцу: запоминает текст, исход задаёт тест."""

    def __init__(self, outcome: tuple[str, str] = ("ok", "отправлено")) -> None:
        self.texts: list[str] = []
        self.outcome = outcome

    def __call__(self, cfg, text):
        self.texts.append(text)
        return self.outcome


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


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch, board, profiles):
    """Своё окружение: каталог профилей, борда и никаких PIPELINE-переменных.

    `keeper_heal.main` (как и CLI движка) пишет `PIPELINE_REPO` прямо в `os.environ`, а
    снятая `monkeypatch.delenv` переменная при восстановлении такого значения не
    откатывает: следующий тест уехал бы в чужой проект, а его `gate.py` — в чужой
    каталог состояния (наблюдалось на прогоне 21.11.2026 в `test_lock.py`).
    """
    names = ("PIPELINE_REPO", "PIPELINE_STATE_DIR", "PIPELINE_CONFIG", "PIPELINE_BOARD")
    before = {name: os.environ.get(name) for name in names}
    for name in names:
        monkeypatch.delenv(name, raising=False)
    yield tmp_path
    for name, value in before.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value


@pytest.fixture()
def board(tmp_path, monkeypatch) -> Path:
    db = tmp_path / "kanban.db"
    monkeypatch.setenv("PIPELINE_KANBAN_DB", str(db))
    return db


@pytest.fixture()
def profiles(tmp_path, monkeypatch) -> Path:
    """Профиль исполнителя существует: карточка на него имеет право уйти."""
    home = tmp_path / "profiles"
    (home / "demo").mkdir(parents=True)
    monkeypatch.setenv(keeper_heal.PROFILES_ENV, str(home))
    return home


@pytest.fixture()
def apply_mode(project: Path) -> Path:
    """Подтверждение владельца: без `keeper.on` обход — только наблюдение."""
    marker = project / ".pipeline" / "state" / keeper_heal.ON_MARKER
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("", encoding="utf-8")
    return marker


def write_board(db: Path, *cards: tuple[str, str, str], created_at: float | None = None) -> Path:
    """Поддельная борда: `(id, статус, ключ идемпотентности)` и время создания."""
    moment = int(NOW if created_at is None else created_at)
    con = sqlite3.connect(db)
    try:
        con.executescript(BOARD_SCHEMA)
        for card_id, status, key in cards:
            con.execute("INSERT INTO tasks (id, title, status, idempotency_key, created_at)"
                        " VALUES (?, ?, ?, ?, ?)",
                        (card_id, "карточка " + card_id, status, key, moment))
        con.commit()
    finally:
        con.close()
    return db


def hourly(project: Path, symptoms: list[dict], run=None, probe=True, notify=None, **kw) -> dict:
    """Один обход с подменёнными внешними зависимостями."""
    cfg = config_mod.load(project)
    return keeper_heal.run_hourly(
        cfg, symptoms, now=NOW, run=run if run is not None else FakeRun(),
        probe=lambda host, port: probe, hermes="/fake/hermes",
        notify=notify if notify is not None else Notifier(), **kw)


def set_profiles(project: Path, **profiles) -> None:
    """Дописать `profiles` в `pipeline.json` проекта (без остальных ключей)."""
    path = project / "pipeline.json"
    cfg = json.loads(path.read_text(encoding="utf-8"))
    cfg.setdefault("profiles", {}).update(profiles)
    path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


# ───────────────────────────── cron-джоба ─────────────────────────────

class Result:
    """Ответ поддельного `hermes` для мастера установки."""

    returncode = 0
    stdout = ""
    stderr = ""


def _stub_setup(monkeypatch, tmp_path):
    """Подмена профилей и CLI: мастер пишет скрипты во временный каталог."""
    home = tmp_path / "home"
    monkeypatch.setattr(setup_flow_mod.proj, "profile_dir", lambda p: home / "profiles" / p)
    calls: list[tuple] = []

    def run(*argv, timeout=180):
        calls.append(argv)
        return Result()

    monkeypatch.setattr(setup_flow_mod.proj, "run_hermes", run)
    return home, calls


def test_keeper_cron_outside_tick(monkeypatch, tmp_path):
    """Обход — своя джоба раз в час в :05, а не в минуты тика стража `*/10`."""
    home, calls = _stub_setup(monkeypatch, tmp_path)
    project = tmp_path / "shop"
    project.mkdir()
    setup_flow_mod.ensure_keeper_cron("shop", project, setup_flow_mod.Log(), skip=False)

    script = home / "profiles" / "shop" / "scripts" / "pipeline_keeper.py"
    assert script.is_file()
    text = script.read_text(encoding="utf-8")
    assert "--hourly" in text and "keeper_heal.py" in text

    created = [call for call in calls if "create" in call]
    assert len(created) == 1
    cron = created[0]
    assert KEEPER_CRON in cron
    assert "pipeline_keeper.py" in cron and "--no-agent" in cron and "local" in cron
    assert cron[cron.index("--name") + 1] == KEEPER_JOB
    minute = int(KEEPER_CRON.split()[0])
    assert minute % 10 != 0, "минута обхода обязана быть вне тика стража (*/10)"


def test_keeper_cron_respects_no_cron(monkeypatch, tmp_path):
    """`--no-cron` — обещание не заводить джобы: смотритель тоже не заводится."""
    home, calls = _stub_setup(monkeypatch, tmp_path)
    project = tmp_path / "shop"
    project.mkdir()
    setup_flow_mod.ensure_keeper_cron("shop", project, setup_flow_mod.Log(), skip=True)
    assert calls == []
    assert not (home / "profiles" / "shop" / "scripts" / "pipeline_keeper.py").exists()


def test_cron_all_includes_keeper(monkeypatch, tmp_path):
    """Мастер установки заводит обход вместе со стражем: иначе он «пока панель открыта»."""
    _home, calls = _stub_setup(monkeypatch, tmp_path)
    project = tmp_path / "shop"
    project.mkdir()
    setup_flow_mod.ensure_cron_all("shop", project, setup_flow_mod.Log(), skip=False, enabled=False)
    names = [call[call.index("--name") + 1] for call in calls if "create" in call]
    assert KEEPER_JOB in names


# ───────────────────────────── пустой скан ─────────────────────────────

def test_empty_scan_creates_no_card(project, apply_mode):
    """Чистый проект — одна строка в лог: модель не будится, команд нет."""
    run = FakeRun()
    result = hourly(project, [], run=run)
    assert result["status"] == "silent"
    assert result["card"]["id"] == ""
    assert run.calls == []
    assert len(result["lines"]) == 1


def test_off_marker_stops_the_pass(project, profiles):
    """`keeper.off` сильнее находок: ни скана, ни починок, ни карточки."""
    (project / ".pipeline" / "state" / keeper_heal.OFF_MARKER).write_text("", encoding="utf-8")
    run = FakeRun()
    result = hourly(project, [make_symptom("demo", "card_stuck")], run=run)
    assert result["status"] == "off"
    assert run.calls == []
    assert keeper_heal.OFF_MARKER in result["lines"][0]


def test_dry_run_without_confirmation_changes_nothing(project, profiles):
    """Без `keeper.on` — наблюдение: карточки нет, команд нет, профиль назван."""
    run = FakeRun()
    result = hourly(project, [make_symptom("demo", "card_stuck")], run=run)
    assert result["status"] == "planned"
    assert result["mode"] == "dry-run"
    assert result["card"]["status"] == "planned"
    assert result["card"]["profile"] == "demo"
    assert run.calls == []


# ───────────────────────────── находка и карточка ─────────────────────────────

def test_finding_creates_exactly_one_card(project, apply_mode):
    run = FakeRun()
    result = hourly(project, [make_symptom("demo", "card_stuck")], run=run)
    assert len(run.creates()) == 1
    assert result["status"] == "carded"
    assert result["card"]["id"] == "t_abc12345"
    assert result["card"]["key"].startswith(keeper_heal.KEEPER_KEY_PREFIX)


def test_card_gets_profile_skill_and_json(project, apply_mode):
    run = FakeRun()
    symptom = make_symptom("demo", "card_stuck", address="t_be793ad3",
                           evidence="карточка висит 152.0 ч")
    result = hourly(project, [symptom], run=run)
    create = run.creates()[0]
    assert create[create.index("--assignee") + 1] == "demo"
    assert create[create.index("--skill") + 1] == keeper_heal.KEEPER_SKILL
    assert create[create.index("--max-runtime") + 1] == keeper_heal.DEFAULT_KEEPER_RUNTIME
    assert create[create.index("--idempotency-key") + 1] == result["card"]["key"]
    body = run.bodies[0]
    assert symptom["symptom_id"] in body and "```json" in body
    assert "## Отчёт починок" in body


def test_profiles_keeper_wins_over_driver(project, apply_mode, profiles):
    """`profiles.keeper` сильнее умолчания: вид работы различает скилл, не профиль."""
    (profiles / "keeper-role").mkdir()
    set_profiles(project, keeper="keeper-role")
    run = FakeRun()
    result = hourly(project, [make_symptom("demo", "card_stuck")], run=run)
    assert result["card"]["profile"] == "keeper-role"
    create = run.creates()[0]
    assert create[create.index("--assignee") + 1] == "keeper-role"


def test_missing_profile_refuses_card(project, apply_mode, profiles, tmp_path):
    """Несуществующий профиль — отказ без карточки: в `ready` она висела бы навсегда."""
    set_profiles(project, keeper="ghost")
    notifier = Notifier()
    run = FakeRun()
    result = hourly(project, [make_symptom("demo", "card_stuck")], run=run, notify=notifier)
    assert result["status"] == "failed"
    assert result["reason"] == keeper_heal.REASON_NO_PROFILE
    assert "ghost" in result["detail"]
    assert run.creates() == []
    assert notifier.texts and "ghost" in notifier.texts[0]


def test_card_not_on_board_is_failure(project, apply_mode, board):
    """Id из вывода CLI — не карточка: борда её не знает, значит не создана."""
    write_board(board)
    run = FakeRun()
    result = hourly(project, [make_symptom("demo", "card_stuck")], run=run)
    assert result["status"] == "failed"
    assert "не знает" in result["detail"]


def test_card_visible_on_board_is_success(project, apply_mode, board):
    write_board(board, ("t_abc12345", "ready", "keeper:demo:x"))
    run = FakeRun()
    result = hourly(project, [make_symptom("demo", "card_stuck")], run=run)
    assert result["status"] == "carded"
    assert "видимость" not in result["detail"]


# ───────────────────────────── окна «не запускать» ─────────────────────────────

def test_live_tick_blocks_the_pass(project, apply_mode):
    """Тот же замок, что у тика: обход поверх идущего тика не идёт."""
    state_dir = project / ".pipeline" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    payload = {"pid": os.getpid(), "host": socket.gethostname(),
               "at": "20.11.2026 12:00:00", "at_ts": NOW}
    (state_dir / "tick.lock").write_text(json.dumps(payload), encoding="utf-8")
    run = FakeRun()
    result = hourly(project, [make_symptom("demo", "card_stuck")], run=run)
    assert result["status"] == "skipped"
    assert result["reason"] == keeper_heal.REASON_BUSY
    assert "тик" in result["detail"]
    assert run.calls == []


def test_keeper_lock_blocks_second_pass(project, apply_mode):
    """Два обхода разом не запускаются: замок `keeper.lock` держит первый."""
    state_dir = project / ".pipeline" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    payload = {"pid": os.getpid(), "host": socket.gethostname(),
               "at": "сейчас", "at_ts": time.time(), "what": "keeper"}
    (state_dir / keeper_heal.KEEPER_LOCK_NAME).write_text(json.dumps(payload),
                                                          encoding="utf-8")
    run = FakeRun()
    result = hourly(project, [make_symptom("demo", "card_stuck")], run=run)
    assert result["status"] == "skipped"
    assert result["reason"] == keeper_heal.REASON_BUSY
    assert "смотритель" in result["detail"]
    assert run.calls == []


def test_keeper_lock_is_released(project, apply_mode):
    """Замок обхода снят после прогона: иначе следующий час молчал бы впустую."""
    hourly(project, [make_symptom("demo", "card_stuck")])
    assert not (project / ".pipeline" / "state" / keeper_heal.KEEPER_LOCK_NAME).exists()


def test_pipeline_card_blocks_the_pass(project, apply_mode, board):
    """Идёт карточка конвейера — вторая работа не запускается."""
    write_board(board, ("t_111111", "running", "plan1:S1"))
    run = FakeRun()
    result = hourly(project, [make_symptom("demo", "card_stuck")], run=run)
    assert result["status"] == "skipped"
    assert "t_111111" in result["detail"]
    assert run.calls == []


def test_own_card_does_not_block(project, apply_mode, board):
    """Своя карточка — не работа конвейера: иначе обход блокировал бы сам себя."""
    write_board(board, ("t_abc12345", "todo", keeper_heal.KEEPER_KEY_PREFIX + "demo:abc"))
    run = FakeRun()
    result = hourly(project, [make_symptom("demo", "card_stuck")], run=run)
    assert result["status"] == "carded"
    assert len(run.creates()) == 1


def test_hanging_card_does_not_block_the_pass(project, apply_mode, board):
    """Висящая карточка работой не считается: молчать из-за неё — потерять находку.

    Живая работа — событие карточки свежее окна `watchdog.heartbeat_fresh_minutes`;
    карточка, по которой два дня ничего не происходило, — как раз то, что смотритель
    обязан назвать карточкой-находкой.
    """
    write_board(board, ("t_333333", "todo", "plan1:S1"),
                ("t_abc12345", "ready", keeper_heal.KEEPER_KEY_PREFIX + "demo:old"),
                created_at=NOW - 48 * 3600)
    run = FakeRun()
    result = hourly(project, [make_symptom("demo", "card_stuck")], run=run)
    assert result["status"] == "carded"
    assert run.creates()


# ───────────────────────────── эскалация владельцу ─────────────────────────────

def test_high_finding_escalates_to_owner(project, apply_mode):
    notifier = Notifier()
    symptom = make_symptom("demo", "card_stuck", address="t_be793ad3",
                           evidence="висит 152.0 ч")
    result = hourly(project, [symptom], notify=notifier)
    assert result["escalated"]["status"] == "ok"
    assert len(notifier.texts) == 1
    assert "t_be793ad3" in notifier.texts[0] and "high" in notifier.texts[0]
    assert result["card"]["id"] in notifier.texts[0]


def test_healed_high_finding_is_not_escalated(project, apply_mode):
    """Починка белого списка сняла high — владельца не будим.

    Симптом `tick_lock_dead` берётся вместе с его причиной (замок с мёртвым pid 0),
    иначе починка нечего снимать и она честно возвращает `failed`.
    """
    state_dir = project / ".pipeline" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "tick.lock").write_text(json.dumps({"pid": 0, "host": socket.gethostname()}),
                                         encoding="utf-8")
    notifier = Notifier()
    result = hourly(project, [make_symptom("demo", "tick_lock_dead")], notify=notifier)
    assert result["heal"]["counts"]["ok"] == 1
    assert result["escalated"]["status"] == "none"
    assert notifier.texts == []


def test_low_finding_is_not_escalated(project, apply_mode):
    """Low-находка чинится молча: панель поднялась — сообщения нет."""
    notifier = Notifier()
    result = hourly(project, [make_symptom("demo", "panel_down")], notify=notifier)
    assert result["heal"]["counts"]["ok"] == 1
    assert result["escalated"]["status"] == "none"
    assert notifier.texts == []


def test_failed_delivery_is_reported(project, apply_mode):
    """Отказ доставки не роняет обход, но виден в отчёте и в логе."""
    notifier = Notifier(outcome=("failed", "не доставлено: 401"))
    result = hourly(project, [make_symptom("demo", "card_stuck")], notify=notifier)
    assert result["escalated"]["status"] == "failed"
    assert any("эскалация владельцу: failed" in line for line in result["lines"])


# ───────────────────────────── точка входа cron ─────────────────────────────

def test_main_hourly_json_dry_run(project, board, capsys):
    """Скрипт cron запускается как `--hourly --json` и не трогает живой проект."""
    code = keeper_heal.main(["--hourly", "--json", "--project", str(project)])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["project"] == "demo"
    assert payload["mode"] == "dry-run"
    assert payload["card"]["id"] == ""


@pytest.mark.slow
def test_keeper_script_runs_the_engine_copy(monkeypatch, tmp_path, project):
    """Cron-скрипт из профиля действительно запускает движок проекта, а не копию кода.

    Проверяется переход «скрипт профиля → `.pipeline/keeper_heal.py`»: без него
    джоба выглядела бы зелёной при мёртвом обходе (прецедент двух копий движка 1-S17).
    """
    home, _calls = _stub_setup(monkeypatch, tmp_path)
    setup_flow_mod.ensure_keeper_cron("demo", project, setup_flow_mod.Log(), skip=False)
    script = home / "profiles" / "demo" / "scripts" / "pipeline_keeper.py"
    env = dict(os.environ, PIPELINE_REPO=str(project))
    res = subprocess.run([sys.executable, str(script)], cwd=str(project),
                         capture_output=True, text=True, encoding="utf-8",
                         errors="replace", env=env)
    assert res.returncode == 0, res.stderr
    assert "смотритель demo" in res.stdout
    assert "находок нет" in res.stdout

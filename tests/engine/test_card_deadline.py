# -*- coding: utf-8 -*-
"""Предел ожидания висящей карточки (план 10, S2A).

Прецедент: карточка провисела в `todo` шесть дней, а страж всё это время молчал — на
каждом тике одна и та же строка «в работе: карточка». Шаг меняет **решение**, а не сбор:
возраст карточки считается по данным борды (последнее событие), предел приходит из
`watchdog.card_deadline_hours`, а по истечении страж останавливается владельческим стопом
с готовыми командами.

Часы подменяются (`gate._now`), борда — поддельная sqlite: тест не ждёт 13 часов и не
трогает живую борду. Внутри файла два уровня: `gate.stuck_cards` (правило) и `gate.tick`
(решение вместе с записью стоп-флага и отметки в состоянии).
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime
from pathlib import Path

import pytest

import config as config_mod
import gate
from stub_hermes import SCHEMA as STUB_SCHEMA

#: Момент «сейчас»: 20.11.2026 12:00 локального времени. Один на весь файл — иначе
#: «13 ч» в одном тесте и «13 ч» в другом означали бы разные окна.
NOW = datetime(2026, 11, 20, 12, 0).timestamp()
HOUR = 3600

#: Живая схема борды: те же таблицы и колонки, что у настоящей (`tasks`, `task_events`,
#: `task_runs`, `comments`). `stub_hermes.SCHEMA` — урезанная: в ней нет ни `started_at`,
#: ни `last_heartbeat_at`, ни таблиц событий, и правило обязано работать на обеих.
BOARD_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY, title TEXT, status TEXT DEFAULT 'ready', assignee TEXT,
    idempotency_key TEXT, completed_at INTEGER, result TEXT, last_failure_error TEXT,
    created_at INTEGER, started_at INTEGER, last_heartbeat_at INTEGER, tenant TEXT,
    workspace TEXT, max_runtime TEXT
);
CREATE TABLE IF NOT EXISTS task_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, created_at INTEGER);
CREATE TABLE IF NOT EXISTS task_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, last_heartbeat_at INTEGER,
    ended_at INTEGER);
CREATE TABLE IF NOT EXISTS comments (
    id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, body TEXT, created_at INTEGER);
"""

STEP = {"id": "S1", "epoch": "", "title": "Таблица счетов", "days": 1.0, "owner": "",
        "goal": "g", "files": [], "gates": [], "dod": "d", "read": [], "notes": ""}


# ─────────────────────────────── стенд ───────────────────────────────

def add_card(db: Path, card_id: str = "t_stale", *, status: str = "todo",
             key: str = "planloop:1:S1:step", age_hours: float = 13.0,
             title: str = "шаг S1", heartbeat_hours: float | None = None) -> None:
    """Карточка в живой схеме борды: возраст — от `created_at`, heartbeat — отдельной меткой."""
    con = sqlite3.connect(db)
    try:
        con.executescript(BOARD_SCHEMA)
        created = int(NOW - age_hours * HOUR)
        heartbeat = int(NOW - heartbeat_hours * HOUR) if heartbeat_hours is not None else 0
        con.execute("INSERT OR REPLACE INTO tasks (id, title, status, assignee,"
                    " idempotency_key, created_at, started_at, last_heartbeat_at)"
                    " VALUES (?,?,?,?,?,?,?,?)",
                    (card_id, title, status, "demo", key, created, created, heartbeat))
        con.commit()
    finally:
        con.close()


def add_plain_card(db: Path, card_id: str = "t_plain", *, status: str = "todo",
                   key: str = "planloop:1:S1:step", age_hours: float = 13.0) -> None:
    """Карточка в урезанной схеме `stub_hermes`: только те колонки, что есть там."""
    con = sqlite3.connect(db)
    try:
        con.executescript(STUB_SCHEMA)
        con.execute("INSERT OR REPLACE INTO tasks (id, title, status, assignee,"
                    " idempotency_key, created_at) VALUES (?,?,?,?,?,?)",
                    (card_id, "шаг S1", status, "demo", key, int(NOW - age_hours * HOUR)))
        con.commit()
    finally:
        con.close()


def add_event(db: Path, card_id: str, table: str, column: str, hours_ago: float) -> None:
    """Живое событие карточки: `task_events.created_at`, `task_runs.*` или `comments`."""
    con = sqlite3.connect(db)
    try:
        con.executescript(BOARD_SCHEMA)
        con.execute(f"INSERT INTO {table} (task_id, {column}) VALUES (?,?)",
                    (card_id, int(NOW - hours_ago * HOUR)))
        con.commit()
    finally:
        con.close()


def load_cfg(project: Path, monkeypatch, watchdog: dict | None = None) -> config_mod.Config:
    """Конфиг проекта; *watchdog* заменяет раздел порога целиком (как правка владельца).

    `require_prompt = false`: шаг с карточкой не должен сначала заказывать постановку —
    иначе тик приносит карточку промптеру, а решение о висящей карточке даже не
    выносится (оно заменяет ожидание, а не заказ работы).
    """
    for name in ("PIPELINE_CONFIG", "PIPELINE_PLANS", "PIPELINE_STATE_DIR", "PIPELINE_BOARD"):
        monkeypatch.delenv(name, raising=False)
    # collector() запускает next_step.py, а тот берёт корень из PIPELINE_REPO.
    monkeypatch.setenv("PIPELINE_REPO", str(project))
    path = project / "pipeline.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["require_prompt"] = False
    if watchdog is not None:
        raw["watchdog"] = watchdog
    path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    return config_mod.load(project)


def stop_file(project: Path) -> Path:
    return project / ".pipeline" / "state" / "plan_1_stop.json"


def read_state(project: Path) -> dict:
    path = project / ".pipeline" / "state" / "plan_1_state.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}


@pytest.fixture()
def clock(monkeypatch) -> float:
    """Часы движка: `gate._now` отдаёт фиксированный момент (единственная точка подмены)."""
    monkeypatch.setattr(gate, "_now", lambda: NOW)
    return NOW


@pytest.fixture()
def db(hermes_stub: Path) -> Path:
    """Путь поддельной борды: `hermes_stub` уже выставил `PIPELINE_KANBAN_DB`."""
    return Path(os.environ["PIPELINE_KANBAN_DB"])


def ctx(**kw) -> dict:
    """Синтетическое состояние решения — как у `test_gate_decide.py`."""
    base = {"next": dict(STEP), "cards": [], "prev_cards": [], "closed": [],
            "prev_closed": None, "prev_verdict": None, "stop": None, "hold": [],
            "verdict": None, "journal_closed": False, "plan_id": "1"}
    base.update(kw)
    return base


def step_card(status: str = "todo", card_id: str = "t_1") -> dict:
    return {"id": card_id, "status": status, "key": "planloop:1:S1:step",
            "result": "", "error": ""}


# ───────────────────── правило: gate.stuck_cards (З2) ─────────────────────

class TestStuckCards:
    def test_old_card_is_stuck_with_own_prefix(self, db: Path):
        add_card(db, "t_stale", age_hours=13)
        found = gate.stuck_cards(db, deadline_hours=12, fresh_minutes=30, now=NOW,
                                 key_prefix="planloop:1:")
        assert [item["id"] for item in found["stuck"]] == ["t_stale"]
        assert found["stuck"][0]["age"] == 13 * HOUR          # целые секунды
        assert found["stuck"][0]["status"] == "todo"
        assert found["nearest"] is None

    def test_fresh_card_is_nearest_not_stuck(self, db: Path):
        add_card(db, "t_fresh", age_hours=1)
        found = gate.stuck_cards(db, deadline_hours=12, fresh_minutes=30, now=NOW)
        assert found["stuck"] == []
        assert found["nearest"]["id"] == "t_fresh"
        assert found["nearest"]["remaining"] == 11 * HOUR

    def test_running_with_fresh_heartbeat_is_not_counted(self, db: Path):
        """Живой шаг на 270 минут: возраст 20 ч, но heartbeat минуту назад."""
        add_card(db, "t_live", status="running", age_hours=20, heartbeat_hours=1 / 60)
        found = gate.stuck_cards(db, deadline_hours=12, fresh_minutes=30, now=NOW)
        assert found == {"stuck": [], "nearest": None}

    def test_running_with_dead_heartbeat_is_stuck(self, db: Path):
        """Мёртвый heartbeat не спасает: последнее событие — 13 ч назад, предел 12 ч."""
        add_card(db, "t_dead", status="running", age_hours=30, heartbeat_hours=13)
        found = gate.stuck_cards(db, deadline_hours=12, fresh_minutes=30, now=NOW)
        assert [item["id"] for item in found["stuck"]] == ["t_dead"]

    def test_event_after_creation_keeps_card_alive(self, db: Path):
        add_card(db, "t_event", age_hours=30)
        add_event(db, "t_event", "task_events", "created_at", hours_ago=1 / 6)
        found = gate.stuck_cards(db, deadline_hours=12, fresh_minutes=30, now=NOW)
        assert found["stuck"] == []

    def test_comment_after_creation_keeps_card_alive(self, db: Path):
        """Свежий комментарий — признак жизни (D3): сканер его не читает, страж читает."""
        add_card(db, "t_comment", age_hours=30)
        add_event(db, "t_comment", "comments", "created_at", hours_ago=1 / 6)
        found = gate.stuck_cards(db, deadline_hours=12, fresh_minutes=30, now=NOW)
        assert found["stuck"] == []
        assert found["nearest"]["id"] == "t_comment"

    def test_terminal_and_foreign_cards_are_ignored(self, db: Path):
        add_card(db, "t_done", status="done", age_hours=30)
        add_card(db, "t_blocked", status="blocked", age_hours=30)
        add_card(db, "t_foreign", key="planloop:2:S1:step", age_hours=30)
        found = gate.stuck_cards(db, deadline_hours=12, fresh_minutes=30, now=NOW,
                                 key_prefix="planloop:1:")
        assert found == {"stuck": [], "nearest": None}
        # Вся борда (вход doctor'а) видит чужой план: это его страж и остановит.
        everything = gate.stuck_cards(db, deadline_hours=12, fresh_minutes=30, now=NOW)
        assert [item["id"] for item in everything["stuck"]] == ["t_foreign"]

    def test_oldest_card_goes_first(self, db: Path):
        add_card(db, "t_young", age_hours=13)
        add_card(db, "t_oldest", age_hours=30)
        found = gate.stuck_cards(db, deadline_hours=12, fresh_minutes=30, now=NOW)
        assert [item["id"] for item in found["stuck"]] == ["t_oldest", "t_young"]

    def test_archived_card_is_neither_stuck_nor_nearest(self, db: Path):
        """Архив — закрытая карточка, а не висящая (F1): её не видит и `read_cards`."""
        add_card(db, "t_arch_old", status="archived", age_hours=30)
        add_card(db, "t_arch_new", status="archived", age_hours=1)
        found = gate.stuck_cards(db, deadline_hours=12, fresh_minutes=30, now=NOW)
        assert found == {"stuck": [], "nearest": None}

    def test_archived_card_ignored_on_reduced_board(self, db: Path):
        """Урезанная схема (`stub_hermes`): хватает статуса, событий для этого не нужно."""
        add_plain_card(db, "t_plain_arch", status="archived", age_hours=30)
        assert gate.stuck_cards(db, deadline_hours=12, fresh_minutes=30,
                                now=NOW) == {"stuck": [], "nearest": None}

    def test_live_card_next_to_archived_is_still_stuck(self, db: Path):
        """Архив пропущен, живая карточка того же плана — нет: фильтр не глушит проверку."""
        add_card(db, "t_arch", status="archived", age_hours=30)
        add_card(db, "t_stale", age_hours=13)
        found = gate.stuck_cards(db, deadline_hours=12, fresh_minutes=30, now=NOW,
                                 key_prefix="planloop:1:")
        assert [item["id"] for item in found["stuck"]] == ["t_stale"]

    def test_reduced_board_counts_age_by_created_at(self, db: Path):
        """Урезанная борда (`stub_hermes`): ни событий, ни heartbeat — считаем по созданию."""
        add_plain_card(db, "t_plain", age_hours=20)
        found = gate.stuck_cards(db, deadline_hours=12, fresh_minutes=30, now=NOW,
                                 key_prefix="planloop:1:")
        assert found["stuck"][0]["age"] == 20 * HOUR

    def test_zero_limit_disables_check(self, db: Path):
        add_card(db, "t_stale", age_hours=300)
        assert gate.stuck_cards(db, deadline_hours=0, fresh_minutes=30,
                                now=NOW) == {"stuck": [], "nearest": None}

    def test_missing_board_and_no_timestamps_are_silent(self, tmp_path: Path, db: Path):
        """Нет базы, пустая база, карточка без меток времени — пусто, а не исключение."""
        assert gate.stuck_cards(tmp_path / "нет.db", deadline_hours=12, fresh_minutes=30,
                                now=NOW) == {"stuck": [], "nearest": None}
        empty = tmp_path / "empty.db"
        empty.write_text("", encoding="utf-8")
        assert gate.stuck_cards(empty, deadline_hours=12, fresh_minutes=30,
                                now=NOW) == {"stuck": [], "nearest": None}
        add_card(db, "t_zero", age_hours=0)
        con = sqlite3.connect(db)
        try:
            con.execute("UPDATE tasks SET created_at = 0, started_at = 0,"
                        " last_heartbeat_at = 0 WHERE id = 't_zero'")
            con.commit()
        finally:
            con.close()
        assert gate.stuck_cards(db, deadline_hours=12, fresh_minutes=30,
                                now=NOW) == {"stuck": [], "nearest": None}


# ──────────────── порог из конфига: gate.watchdog_limits (З1) ────────────────

class TestWatchdogLimits:
    def test_defaults_and_zero(self):
        assert gate.watchdog_limits({}) == {"heartbeat_fresh_minutes": 30,
                                            "card_deadline_hours": 12}
        assert gate.watchdog_limits({"watchdog": {"card_deadline_hours": 0}}) == {
            "heartbeat_fresh_minutes": 30, "card_deadline_hours": 0}

    def test_equals_loaded_config(self, project: Path, monkeypatch):
        """Ровно то, что видит страж: у диагностики и у тика один порог, а не два."""
        load_cfg(project, monkeypatch, {"heartbeat_fresh_minutes": "15",
                                        "card_deadline_hours": "6"})
        loaded = config_mod.load(project)
        assert loaded.watchdog == gate.watchdog_limits(loaded.state) == {
            "heartbeat_fresh_minutes": 15, "card_deadline_hours": 6}

    def test_fractional_hours_truncate_with_one_warning(self, project: Path, monkeypatch,
                                                        capsys):
        """`12.5` — двенадцать часов и одна строка WARNING: порог не дробный (З1)."""
        cfg = load_cfg(project, monkeypatch, {"card_deadline_hours": "12.5"})
        assert cfg.watchdog["card_deadline_hours"] == 12
        warnings = [line for line in capsys.readouterr().err.splitlines()
                    if line.startswith("WARNING")]
        assert len(warnings) == 1 and "12.5" in warnings[0]

    def test_negative_hours_take_default_with_warning(self, project: Path, monkeypatch,
                                                      capsys):
        """`-1` — не «выключено», а мусор: умолчание 12 ч и WARNING, проверка остаётся."""
        cfg = load_cfg(project, monkeypatch, {"card_deadline_hours": "-1"})
        assert cfg.watchdog["card_deadline_hours"] == 12
        warnings = [line for line in capsys.readouterr().err.splitlines()
                    if line.startswith("WARNING")]
        assert len(warnings) == 1 and "-1" in warnings[0]

    def test_garbage_gives_default_and_one_warning(self, project: Path, monkeypatch,
                                                   capsys):
        load_cfg(project, monkeypatch, {"card_deadline_hours": "abc", "мусор": 1})
        err = capsys.readouterr().err
        warnings = [line for line in err.splitlines() if line.startswith("WARNING")]
        assert len(warnings) == 2                   # одно значение и один неизвестный ключ
        assert "abc" in warnings[0] or "abc" in warnings[1]

    def test_null_takes_default(self, project: Path, monkeypatch):
        """`null` — умолчание без WARNING: значение не отвергнуто, оно просто пустое."""
        cfg = load_cfg(project, monkeypatch, {"card_deadline_hours": None})
        assert cfg.watchdog["card_deadline_hours"] == 12


# ──────────────────── решение: decide на синтетическом ctx (З3) ────────────────────

class TestDecide:
    def test_stale_card_stops_with_owner_kind(self):
        decision = gate.decide(ctx(cards=[step_card()], card_deadline_reported="",
                                   watchdog={"card_deadline_hours": 12},
                                   stuck_cards=[{"id": "t_stale", "status": "todo",
                                                 "title": "шаг S1", "age": 13 * HOUR}]))
        assert decision["action"] == "stop"
        assert decision["kind"] == "owner"
        assert decision["card_deadline"] == "t_stale"
        assert "висит 13 ч" in decision["reason"]
        for command in ("complete", "block", "reclaim"):
            assert f"hermes kanban --board <борда> {command}" in decision["details"]

    def test_reported_card_does_not_stop_again(self):
        """Отметка гасит только свой id: после `--resume` второго стопа нет."""
        decision = gate.decide(ctx(cards=[step_card()],
                                   card_deadline_reported="t_stale",
                                   stuck_cards=[{"id": "t_stale", "status": "todo",
                                                 "title": "шаг S1", "age": 13 * HOUR}]))
        assert decision["action"] == "idle"
        assert "в работе" in decision["reason"]

    def test_other_card_after_resume_stops_again(self):
        decision = gate.decide(ctx(cards=[step_card()],
                                   card_deadline_reported="t_old",
                                   stuck_cards=[{"id": "t_new", "status": "todo",
                                                 "title": "шаг S1", "age": 20 * HOUR}]))
        assert decision["action"] == "stop"
        assert decision["card_deadline"] == "t_new"

    def test_without_key_keeps_previous_decision(self):
        """Синтетический `ctx` без ключа `stuck_cards` решается как до шага (З7)."""
        decision = gate.decide(ctx(cards=[step_card()]))
        assert decision["action"] == "idle"
        assert "в работе" in decision["reason"]

    def test_orderable_decision_is_not_intercepted(self):
        """Стоп заменяет ожидание, а не заказ: решение-действие остаётся прежним."""
        decision = gate.decide(ctx(cards=[], stuck_cards=[{"id": "t_stale", "status": "todo",
                                                           "title": "шаг", "age": HOUR * 20}]))
        assert decision["action"] == "create_step"

    def test_closed_plan_is_not_stopped_by_stale_card(self):
        """F2: у закрытого плана работы нет по определению — `idle` с `plan_done`."""
        decision = gate.decide(ctx(next=None,
                                   stuck_cards=[{"id": "t_arch", "status": "todo",
                                                 "title": "шаг", "age": 356 * HOUR}]))
        assert decision["action"] == "idle"
        assert decision.get("plan_done") is True
        assert "kind" not in decision                # владельческого стопа нет

    def test_approval_wait_is_not_stopped_by_stale_card(self):
        """F2: неутверждённый план ждёт владельца — ограда та же, что у предзаказа."""
        decision = gate.decide(ctx(cards=[],
                                   approval={"ok": False, "started": False,
                                             "block_kind": None,
                                             "reason": "план не утверждён"},
                                   stuck_cards=[{"id": "t_stale", "status": "todo",
                                                 "title": "шаг", "age": 13 * HOUR}]))
        assert decision["action"] == "idle"
        assert decision.get("approval_blocked") is True
        assert "kind" not in decision

    def test_question_wait_is_not_stopped_by_stale_card(self):
        """F2: шаг ждёт ответа на вопрос — ожидание по делу, а не простой."""
        decision = gate.decide(ctx(cards=[],
                                   step_questions=[{"id": "Q1", "text": "какой путь?",
                                                    "options": "A / B", "recommend": "A"}],
                                   stuck_cards=[{"id": "t_stale", "status": "todo",
                                                 "title": "шаг", "age": 13 * HOUR}]))
        assert decision["action"] == "idle"
        assert decision.get("question") == "Q1"
        assert "kind" not in decision


# ────────────────── тик целиком: build_ctx + run_gate (З3/З4) ──────────────────

def plan_decision(cfg: config_mod.Config) -> dict:
    """Решение по плану 1 ровно как у тика (`build_ctx` + `decide`), без побочных эффектов.

    `gate.tick` возвращает решение только применённого действия: на простое он отдаёт
    общий ответ «во всех планах нет работы», и ожидание конкретного плана в нём не видно.
    Поэтому применение проверяется тиком, а ожидание — этой парой вызовов.
    """
    return gate.decide(gate.build_ctx(gate.resolve_paths(cfg, "1")))


class TestTick:
    def test_stale_card_writes_owner_stop_once(self, project: Path, db: Path,
                                               monkeypatch, clock):
        add_card(db, "t_stale", age_hours=13)
        cfg = load_cfg(project, monkeypatch)
        result = gate.tick(cfg)
        decision = result["decision"]
        assert decision["action"] == "stop"
        assert decision["kind"] == "owner"
        stop = json.loads(stop_file(project).read_text(encoding="utf-8"))
        assert stop["kind"] == "owner"
        assert "t_stale" in stop["reason"] and "висит" in stop["reason"]
        assert read_state(project)["card_deadline_reported"] == "t_stale"
        # Второй тик стоп не повторяет: стоп-флаг владельца держит конвейер сам.
        again = plan_decision(cfg)
        assert again["action"] == "idle"
        assert "стоп-флаг владельца" in again["reason"]

    def test_fresh_card_leaves_previous_idle(self, project: Path, db: Path,
                                             monkeypatch, clock):
        add_card(db, "t_fresh", age_hours=1)
        decision = plan_decision(load_cfg(project, monkeypatch))
        assert decision["action"] == "idle"
        assert "в работе" in decision["reason"]
        assert not stop_file(project).is_file()

    def test_running_with_fresh_heartbeat_is_idle(self, project: Path, db: Path,
                                                  monkeypatch, clock):
        add_card(db, "t_live", status="running", age_hours=20, heartbeat_hours=0.05)
        decision = plan_decision(load_cfg(project, monkeypatch))
        assert decision["action"] == "idle"
        assert not stop_file(project).is_file()

    def test_resume_returns_idle_not_new_stop(self, project: Path, db: Path,
                                              monkeypatch, clock):
        add_card(db, "t_stale", age_hours=13)
        cfg = load_cfg(project, monkeypatch)
        gate.tick(cfg)
        assert gate.clear_stop(gate.resolve_paths(cfg, "1"))
        decision = plan_decision(cfg)
        assert decision["action"] == "idle"
        assert "в работе" in decision["reason"]
        assert not stop_file(project).is_file()

    def test_terminal_card_does_not_clear_owner_stop(self, project: Path, db: Path,
                                                     monkeypatch, clock):
        """Владельческий стоп терминальный статус карточки не снимает (п.5 конституции)."""
        add_card(db, "t_stale", age_hours=13)
        cfg = load_cfg(project, monkeypatch)
        gate.tick(cfg)
        con = sqlite3.connect(db)
        try:
            con.execute("UPDATE tasks SET status = 'done', completed_at = ?"
                        " WHERE id = 't_stale'", (int(NOW),))
            con.commit()
        finally:
            con.close()
        decision = plan_decision(cfg)
        assert decision["action"] == "idle"
        assert "стоп-флаг владельца" in decision["reason"]
        assert stop_file(project).is_file()

    def test_other_plan_card_does_not_stop(self, project: Path, db: Path,
                                           monkeypatch, clock):
        add_card(db, "t_foreign", key="planloop:2:S1:step", age_hours=30)
        decision = plan_decision(load_cfg(project, monkeypatch))
        assert decision["action"] != "stop"
        assert not stop_file(project).is_file()

    def test_zero_limit_keeps_behaviour(self, project: Path, db: Path, monkeypatch, clock):
        add_card(db, "t_stale", age_hours=300)
        cfg = load_cfg(project, monkeypatch, {"card_deadline_hours": 0})
        decision = plan_decision(cfg)
        assert decision["action"] == "idle"
        assert "в работе" in decision["reason"]
        assert not stop_file(project).is_file()

    def test_garbage_limit_stops_as_default(self, project: Path, db: Path,
                                            monkeypatch, clock, capsys):
        """Мусор в значении: умолчание 12 ч и строка WARNING, а не простой."""
        add_card(db, "t_stale", age_hours=13)
        cfg = load_cfg(project, monkeypatch, {"card_deadline_hours": "abc"})
        assert cfg.watchdog["card_deadline_hours"] == 12
        assert gate.tick(cfg)["decision"]["action"] == "stop"

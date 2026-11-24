# -*- coding: utf-8 -*-
"""Метрика попытки шага (план 17, S1): разбор `agent.log`, строка журнала, вызов из тика.

Зачем файл: шаг заказан из-за замера 20.11.2026 — попытка шла 91 минуту, 17 минут модель и
34 минуты инструменты, и без разбора лога этих чисел не видел никто. Тесты держат три вещи,
на которых метрика ломается молча: формат строк живого лога (фикстура — его копия), окно
попытки (одна попытка не считает время соседней) и идемпотентность записи (повторный тик
не переписывает журнал).

Числа ожиданий посчитаны по `tests/engine/fixtures/agent-log-sample.txt` вручную и записаны
в постановке шага: `model_ms` = 12500, `tools_ms` = {"terminal": 308320, "patch": 4000},
`tool_timeouts` = 1.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest

import config as config_mod
import gate
import step_metrics

FIXTURES = Path(__file__).resolve().parent / "fixtures"
SAMPLE = FIXTURES / "agent-log-sample.txt"

# Схема поддельной борды: у живой есть `profile`, `error` и `max_runtime_seconds`, и метрика
# обязана их читать; отдельный тест проверяет усечённую схему без них.
TASKS = ("CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, title TEXT,"
         " status TEXT, assignee TEXT, idempotency_key TEXT, completed_at INTEGER,"
         " result TEXT, last_failure_error TEXT, created_at INTEGER)")
TASK_RUNS = ("CREATE TABLE IF NOT EXISTS task_runs (id INTEGER PRIMARY KEY AUTOINCREMENT,"
             " task_id TEXT NOT NULL, profile TEXT, status TEXT, outcome TEXT,"
             " summary TEXT, error TEXT, started_at INTEGER, ended_at INTEGER,"
             " max_runtime_seconds INTEGER)")
#: Отметка «аргумент не передан»: `ended=None` в помощнике значит «прогон ещё идёт».
_OPEN = object()


def at(stamp: str) -> int:
    """Момент из фикстуры (`10:00:20`) в epoch — та же локальная зона, что у борды."""
    return int(time.mktime(time.strptime("2026-11-20 " + stamp, "%Y-%m-%d %H:%M:%S")))


def sample_text() -> str:
    return SAMPLE.read_text(encoding="utf-8")


def row_of(**kwargs) -> dict:
    """Строка метрики по фикстуре: прогон — из ключей, лог — фикстура по умолчанию."""
    run = kwargs.pop("run", None)
    log_path = kwargs.pop("log_path", SAMPLE)
    return step_metrics.attempt_row("1", "S1", {"id": "t_1", "assignee": "demo"},
                                    run, log_path=log_path, now=at("12:00:00"))


@pytest.fixture()
def no_logs(tmp_path: Path, monkeypatch) -> Path:
    """Пустой каталог Hermes: `agent_log` не находит живых логов профилей хоста.

    `HERMES_HOME` уже подменён автофикстурой плагина (`tests/conftest.py`); здесь тот же
    каталог берётся как корень, а `LOCALAPPDATA` уводится в tmp — иначе запасной путь
    движка нашёл бы логи настоящего хоста и числа теста зависели бы от машины.
    """
    home = tmp_path / "hermes-home"
    home.mkdir(parents=True, exist_ok=True)
    local = tmp_path / "local"
    local.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    return home


@pytest.fixture()
def stand(project: Path, hermes_stub: Path, monkeypatch, no_logs: Path) -> Path:
    """Стенд тика: проект с движком, поддельная борда и пустые логи профилей."""
    for name in ("PIPELINE_CONFIG", "PIPELINE_PLANS", "PIPELINE_STATE_DIR", "PIPELINE_BOARD"):
        monkeypatch.delenv(name, raising=False)
    # Сборщик шагов (`next_step.py`) запускается подпроцессом и берёт проект из
    # `PIPELINE_REPO`: без него он читал бы конфигурацию плагина, а не стенда.
    monkeypatch.setenv("PIPELINE_REPO", str(project))
    raw = json.loads((project / "pipeline.json").read_text(encoding="utf-8"))
    raw["require_prompt"] = False
    raw["notify"] = {"enabled": False}
    (project / "pipeline.json").write_text(json.dumps(raw, ensure_ascii=False),
                                           encoding="utf-8")
    return project


def seed_card(db: Path, card_id: str = "t_step01", key: str = "planloop:1:S1:step",
              status: str = "done", assignee: str = "demo") -> None:
    con = sqlite3.connect(db)
    try:
        con.executescript(TASKS)
        con.executescript(TASK_RUNS)
        con.execute("INSERT OR REPLACE INTO tasks (id, title, status, assignee,"
                    " idempotency_key, created_at) VALUES (?,?,?,?,?,?)",
                    (card_id, "шаг", status, assignee, key, at("09:00:00")))
        con.commit()
    finally:
        con.close()


def add_run(db: Path, card_id: str = "t_step01", profile: str = "demo",
            outcome: str = "completed", started: int | None = None,
            ended=_OPEN, error: str = "", max_runtime: int | None = None) -> int:
    """Прогон карточки в борде; `ended=None` — прогон ещё идёт (строки быть не должно)."""
    con = sqlite3.connect(db)
    try:
        con.executescript(TASK_RUNS)
        cur = con.execute(
            "INSERT INTO task_runs (task_id, profile, status, outcome, error, started_at,"
            " ended_at, max_runtime_seconds) VALUES (?,?,?,?,?,?,?,?)",
            (card_id, profile, "done", outcome, error,
             at("10:00:00") if started is None else started,
             at("10:00:30") if ended is _OPEN else ended, max_runtime))
        con.commit()
        return int(cur.lastrowid)
    finally:
        con.close()


def metrics_rows(cfg) -> list[dict]:
    path = step_metrics.metrics_path(cfg)
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]


# ───────────────────────────── П1: разбор лога ─────────────────────────────

def test_events_from_fixture():
    """Фикстура даёт 2 вызова модели, 5 строк инструментов и 1 строку лимита."""
    events = step_metrics.parse_events(sample_text())
    assert [item["kind"] for item in events].count("api") == 2
    assert [item["kind"] for item in events].count("tool") == 5
    assert [item["kind"] for item in events].count("limit") == 1
    terminal = [item for item in events if item["name"] == "terminal"]
    assert [item["ms"] for item in terminal] == [5000, 303180, 140]
    # Таймаут — только у упавшей строки инструмента; `[deadline]` не считается (Д3).
    assert [item["timeout"] for item in terminal] == [False, True, False]
    patch = [item for item in events if item["name"] == "patch"]
    assert [item["timeout"] for item in patch] == [False, False]
    limit = [item for item in events if item["kind"] == "limit"][0]
    assert (limit["elapsed_s"], limit["limit_s"]) == (5491, 3600)


# ───────────────────────── П2: строка метрики попытки ─────────────────────────

def test_attempt_row_sums_by_phase():
    """Время складывается по фазам: модель, инструменты по именам, таймауты, вызовы."""
    row = row_of(run={"id": 7, "profile": "demo", "started_at": at("10:00:00"),
                      "ended_at": at("10:01:15"), "outcome": "completed"})
    assert row["model_ms"] == 12500
    assert row["tools_ms"] == {"terminal": 308320, "patch": 4000}
    assert row["tools_total_ms"] == 312320
    assert row["tool_timeouts"] == 1
    assert row["calls"] == 2
    assert (row["in"], row["out"]) == (3000, 300)
    assert row["model"] == "deepseek-v4.1-flash"
    assert row["source"] == "agent.log" and row["log"] == str(SAMPLE)
    assert row["wall_ms"] == 75000 and row["role"] == "demo" and row["run_id"] == "7"


def test_attempt_window_excludes_other_attempts():
    """В окно попытки (10:00:19–10:00:31) попадает один вызов модели, а не два (Д4)."""
    row = row_of(run={"id": 8, "started_at": at("10:00:21"), "ended_at": at("10:00:29")})
    assert row["calls"] == 1
    assert row["model_ms"] == 2500
    # Патч с ошибкой и таймаут терминала — этой попытки, а первый `terminal` — прошлой.
    assert row["tools_ms"] == {"patch": 4000, "terminal": 303320}
    assert row["tool_timeouts"] == 1


def test_killed_by_from_limit_line():
    """Без записи прогона лимит берётся из строки лога — запасной путь Д2."""
    row = row_of()
    assert row["killed_by"] == "runtime"
    assert row["wall_ms"] == 5491000
    assert row["limit_s"] == 3600


def test_killed_by_from_run_record():
    """Запись прогона важнее лога: исход `timed_out`, длительность по меткам борды."""
    row = row_of(run={"id": 9, "outcome": "timed_out", "error": "elapsed 4264s > limit 4080s",
                      "started_at": 1795178483, "ended_at": 1795182748,
                      "max_runtime_seconds": 4080}, log_path=None)
    assert row["killed_by"] == "runtime"
    assert row["wall_ms"] == 4265000
    assert row["limit_s"] == 4080
    assert row["source"] == "none" and row["log"] == ""


def test_empty_log_gives_zero_row(tmp_path: Path):
    """Пустой лог — строка с нулями и `source: none`, а не исключение."""
    log = tmp_path / "agent.log"
    log.write_text("2026-11-20 10:00:00,000 INFO ничего полезного\n", encoding="utf-8")
    row = row_of(run={"id": 11, "started_at": at("10:00:00"), "ended_at": at("10:00:30")},
                 log_path=log)
    assert row["model_ms"] == 0 and row["tools_ms"] == {} and row["calls"] == 0
    assert row["tool_timeouts"] == 0 and row["model"] == ""
    assert row["source"] == "none" and row["killed_by"] == "none"
    assert row["wall_ms"] == 30000      # метки прогона есть — длительность из них


# ───────────────────────── П4: дозапись и идемпотентность ─────────────────────────

def test_record_appends_one_row_per_run(tmp_path: Path, project: Path, hermes_stub: Path,
                                        no_logs: Path):
    """Две карточки, три завершённых прогона — три строки с заполненными полями."""
    cfg = config_mod.load(project)
    db = tmp_path / "board.db"
    seed_card(db, "t_step01")
    seed_card(db, "t_step02")
    first = add_run(db, "t_step01", started=at("10:00:00"), ended=at("10:00:30"))
    second = add_run(db, "t_step01", started=at("11:00:00"), ended=at("11:00:10"))
    third = add_run(db, "t_step02", started=at("12:00:00"), ended=at("12:00:20"))
    add_run(db, "t_step02", started=at("13:00:00"), ended=None)   # прогон идёт

    cards = [{"id": "t_step01", "assignee": "demo"}, {"id": "t_step02", "assignee": "demo"}]
    assert step_metrics.record(cfg, db, "1", "S1", cards, now=at("14:00:00")) == 3

    rows = metrics_rows(cfg)
    assert [(row["card"], row["attempt"], row["run_id"]) for row in rows] == [
        ("t_step01", 1, str(first)), ("t_step01", 2, str(second)),
        ("t_step02", 1, str(third))]
    assert {row["plan"] for row in rows} == {"1"} and {row["step"] for row in rows} == {"S1"}
    assert all(row["role"] == "demo" and row["source"] == "none" for row in rows)


def test_append_is_idempotent_by_run_id(tmp_path: Path, project: Path, hermes_stub: Path,
                                        no_logs: Path):
    """Повторный тик по тем же прогонам не пишет вторую строку (Д5)."""
    cfg = config_mod.load(project)
    db = tmp_path / "board.db"
    seed_card(db)
    add_run(db, "t_step01")
    cards = [{"id": "t_step01", "assignee": "demo"}]

    assert step_metrics.record(cfg, db, "1", "S1", cards) == 1
    path = step_metrics.metrics_path(cfg)
    before = path.read_bytes()
    assert step_metrics.record(cfg, db, "1", "S1", cards) == 0
    assert path.read_bytes() == before
    assert len(metrics_rows(cfg)) == 1


def test_record_survives_trimmed_board_schema(tmp_path: Path, project: Path,
                                              hermes_stub: Path, no_logs: Path):
    """Борда без `profile`, `error` и `max_runtime_seconds`: роль из карточки, не падаем."""
    cfg = config_mod.load(project)
    db = tmp_path / "board.db"
    con = sqlite3.connect(db)
    try:
        con.execute("CREATE TABLE task_runs (id INTEGER PRIMARY KEY AUTOINCREMENT,"
                    " task_id TEXT, status TEXT, outcome TEXT, summary TEXT,"
                    " started_at INTEGER, ended_at INTEGER)")
        con.execute("INSERT INTO task_runs (task_id, status, outcome, started_at, ended_at)"
                    " VALUES (?,?,?,?,?)",
                    ("t_step01", "done", "completed", at("10:00:00"), at("10:00:30")))
        con.commit()
    finally:
        con.close()

    assert step_metrics.record(cfg, db, "1", "S1",
                               [{"id": "t_step01", "assignee": "demo"}]) == 1
    row = metrics_rows(cfg)[0]
    assert row["role"] == "demo" and row["killed_by"] == "none" and row["limit_s"] == 0


# ───────────────────────────── П5: вызов из тика ─────────────────────────────

def test_tick_records_attempt(stand: Path, hermes_stub: Path):
    """Тик пишет строку метрики по завершённому прогону карточки шага и не дублирует её."""
    seed_card(hermes_stub)
    run_id = add_run(hermes_stub)
    cfg = config_mod.load(stand)
    p = gate.resolve_paths(cfg, "1")

    gate.run_gate(p, notify_fn=lambda text: None)
    rows = metrics_rows(cfg)
    assert [row["run_id"] for row in rows] == [str(run_id)]
    assert rows[0]["plan"] == "1" and rows[0]["step"] == "S1"
    assert rows[0]["card"] == "t_step01" and rows[0]["attempt"] == 1

    gate.run_gate(p, notify_fn=lambda text: None)
    assert len(metrics_rows(cfg)) == 1

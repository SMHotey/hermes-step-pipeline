# -*- coding: utf-8 -*-
"""Залипание инструмента (план 17, S2): правило порога, ключи строки, комментарий, правило метода.

Зачем файл: шаг заказан по замеру 24.11.2026 — попытка 2-S6 провела 23,5 минуты в `patch`,
который не работал (30 вызовов, средняя 47 с, три по 313–392 с, два таймаута по 420 с), и без
признака «залип» это видно только глазами по логу. Тесты держат то, что ломается молча:
границу порога, смысл «подряд» (Д4), таймаут без порога, выбор названного инструмента,
порог из конфигурации вместо литерала и повтор комментария по тому же прогону (Д7).
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

# Схема поддельной борды — как у метрики попытки (17-S1): карточки и их прогоны.
TASKS = ("CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, title TEXT,"
         " status TEXT, assignee TEXT, idempotency_key TEXT, completed_at INTEGER,"
         " result TEXT, last_failure_error TEXT, created_at INTEGER)")
TASK_RUNS = ("CREATE TABLE IF NOT EXISTS task_runs (id INTEGER PRIMARY KEY AUTOINCREMENT,"
             " task_id TEXT NOT NULL, profile TEXT, status TEXT, outcome TEXT,"
             " summary TEXT, error TEXT, started_at INTEGER, ended_at INTEGER,"
             " max_runtime_seconds INTEGER)")
#: Лог попытки собственными строками (не фикстурой S1: там другой расклад): две правки
#: по 320 и 371,25 с — залипание `patch`, 691 с суммарно.
STALL_LOG = (
    "2026-11-20 10:00:03,000 INFO [s1] agent.tool_executor:"
    " tool patch completed (320.00s, 10 chars)\n"
    "2026-11-20 10:00:04,000 WARNING [s1] agent.tool_executor:"
    " Tool patch returned error (371.25s): {\"success\": false}\n")


def ev(name: str, seconds: float, timeout: bool = False) -> dict:
    """Событие инструмента в форме `parse_events`: имя, миллисекунды, таймаут."""
    return {"kind": "tool", "name": name, "ms": int(round(seconds * 1000)),
            "timeout": timeout}


def at(stamp: str) -> int:
    """Момент из лога (`10:00:20`) в epoch — та же локальная зона, что у борды."""
    return int(time.mktime(time.strptime("2026-11-20 " + stamp, "%Y-%m-%d %H:%M:%S")))


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
            started: int | None = None, ended: int | None = None) -> int:
    """Завершённый прогон карточки; по умолчанию — окно 10:00:00–10:00:30 из `STALL_LOG`."""
    con = sqlite3.connect(db)
    try:
        con.executescript(TASK_RUNS)
        cur = con.execute(
            "INSERT INTO task_runs (task_id, profile, status, outcome, started_at, ended_at)"
            " VALUES (?,?,?,?,?,?)",
            (card_id, profile, "done", "completed",
             at("10:00:00") if started is None else started,
             at("10:00:30") if ended is None else ended))
        con.commit()
        return int(cur.lastrowid)
    finally:
        con.close()


def comments(db: Path) -> list[dict]:
    """Комментарии поддельной борды в порядке записи."""
    if not db.is_file():
        return []
    con = sqlite3.connect(db)
    try:
        rows = con.execute("SELECT task_id, body FROM comments"
                           " ORDER BY created_at").fetchall()
    finally:
        con.close()
    return [{"task_id": row[0], "body": row[1]} for row in rows]


def stall_comments(db: Path) -> list[dict]:
    return [item for item in comments(db) if "инструмент залип" in (item["body"] or "")]


def write_row(cfg, **fields) -> Path:
    """Положить строку журнала метрики в состояние проекта (как это делает `record`)."""
    path = step_metrics.metrics_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {"plan": "1", "step": "S1", "card": "t_step01", "card_kind": "step",
           "run_id": "1", "stall": False}
    row.update(fields)
    path.write_text(json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


#: Минимальный шаг для тела карточки: всё, что читает `step_card_body`, и ничего лишнего.
MIN_STEP = {"id": "S1", "title": "Таблица счетов", "goal": "таблица", "files": [],
            "gates": [], "dod": "3 теста"}


@pytest.fixture()
def stand(project: Path, hermes_stub: Path, monkeypatch, tmp_path: Path) -> Path:
    """Стенд тика: проект, поддельная борда и лог профиля `demo` с залипанием правки."""
    for name in ("PIPELINE_CONFIG", "PIPELINE_PLANS", "PIPELINE_STATE_DIR", "PIPELINE_BOARD"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PIPELINE_REPO", str(project))
    home = tmp_path / "hermes-home"
    logs = home / "profiles" / "demo" / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    (logs / "agent.log").write_text(STALL_LOG, encoding="utf-8")
    # `_hermes_dirs` смотрит ещё в `LOCALAPPDATA` и в домашний каталог: без подмены страж
    # прочитал бы логи настоящего хоста, и числа теста зависели бы от машины.
    monkeypatch.setenv("HERMES_HOME", str(home))
    local = tmp_path / "local"
    local.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    raw = json.loads((project / "pipeline.json").read_text(encoding="utf-8"))
    raw["require_prompt"] = False
    raw["notify"] = {"enabled": False}
    (project / "pipeline.json").write_text(json.dumps(raw, ensure_ascii=False),
                                           encoding="utf-8")
    return project


# ───────────────────────── П1: правило порога ─────────────────────────

def test_two_slow_calls_give_stall():
    """Два вызова правки подряд по 320 и 371,25 с — залип `patch`, 2 вызова, 691 с."""
    row = step_metrics.stall_of([ev("patch", 320), ev("patch", 371.25)])
    assert row["stall"] is True
    assert row["tool"] == "patch"
    assert row["calls"] == 2
    assert row["s"] == 691
    assert row["timeouts"] == 0


def test_single_slow_call_is_not_stall():
    """Один медленный вызов — ещё не метод (Д4): признака нет."""
    row = step_metrics.stall_of([ev("patch", 320)])
    assert row["stall"] is False
    assert row["tool"] == ""


def test_tool_timeout_is_stall_at_once():
    """Таймаут — залипание сразу, порог не нужен: одна правка в секунду с `timeout`."""
    row = step_metrics.stall_of([ev("patch", 1, timeout=True)])
    assert row["stall"] is True
    assert row["tool"] == "patch"
    assert row["timeouts"] == 1


def test_alternating_slow_tools_are_not_stall():
    """`patch` 320, `terminal` 400, `patch` 320: чередование серии не образует (Д4)."""
    row = step_metrics.stall_of([ev("patch", 320), ev("terminal", 400), ev("patch", 320)])
    assert row["stall"] is False
    assert row["tool"] == ""


def test_heaviest_stalled_tool_is_named():
    """Назван более тяжёлый из залипших; при равенстве — чей признак раньше в логе."""
    heavy = [ev("patch", 320), ev("patch", 330), ev("terminal", 500, True),
             ev("terminal", 600, True)]
    assert step_metrics.stall_of(heavy)["tool"] == "terminal"
    tie = [ev("patch", 300), ev("patch", 300), ev("terminal", 600, True)]
    assert step_metrics.stall_of(tie)["tool"] == "patch"


def test_several_segments_are_summed():
    """`calls`/`s`/`timeouts` — по всем вызовам названного инструмента в окне, не по серии."""
    events = [ev("patch", 320), ev("patch", 330), ev("terminal", 1),
              ev("patch", 340), ev("patch", 350)]
    row = step_metrics.stall_of(events)
    assert row["stall"] is True
    assert (row["tool"], row["calls"], row["s"]) == ("patch", 4, 1340)


# ───────────────────────── П2: поля строки метрики ─────────────────────────

def test_row_carries_stall_fields(tmp_path: Path):
    """Строка попытки несёт ключи залипания и вид карточки `step`."""
    log = tmp_path / "agent.log"
    log.write_text(STALL_LOG, encoding="utf-8")
    row = step_metrics.attempt_row(
        "17", "S2", {"id": "t_1", "assignee": "demo", "key": "planloop:17:S2:step"},
        {"id": 7, "profile": "demo", "started_at": at("10:00:00"),
         "ended_at": at("10:00:30"), "outcome": "completed"},
        log_path=log, now=at("12:00:00"))
    assert row["stall"] is True
    assert row["stall_tool"] == "patch"
    assert row["stall_calls"] == 2
    assert row["stall_s"] == 691
    assert row["stall_timeouts"] == 0
    assert row["card_kind"] == "step"


def test_card_kind_takes_view_not_round():
    """Вид карточки — сегмент сразу после шага: номер круга хвостом видом не является."""
    assert step_metrics.card_kind("planloop:17:S2:fix:1") == "fix"
    assert step_metrics.card_kind("planloop:17:S2:review:2") == "review"
    assert step_metrics.card_kind("planloop:17:S2:step") == "step"
    assert step_metrics.card_kind("") == ""
    assert step_metrics.card_kind("чужой-ключ") == ""
    row = step_metrics.attempt_row("17", "S2", {"id": "t_2", "assignee": "demo"}, None)
    assert row["card_kind"] == ""


# ───────────────────────── П3: ключи конфига ─────────────────────────

def test_config_rejects_bad_thresholds(project: Path):
    """Мусор в `tools` — `ConfigError` с именем поля, а не умолчание (Д3)."""
    path = project / "pipeline.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    for bad, field in (({"stall_seconds": "abc"}, "tools.stall_seconds"),
                       ({"stall_repeats": 0}, "tools.stall_repeats"),
                       ({"stall_timeout": 5}, "tools")):
        raw["tools"] = bad
        path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        with pytest.raises(config_mod.ConfigError) as exc:
            config_mod.load(project)
        assert field in str(exc.value)


def test_config_takes_values_from_file(project: Path):
    """Без ключа — умолчания 300/2; значения из файла читаются как есть (600 и 3)."""
    assert config_mod.load(project).tools == {"stall_seconds": 300, "stall_repeats": 2}
    path = project / "pipeline.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["tools"] = {"stall_seconds": 600, "stall_repeats": 3}
    path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    cfg = config_mod.load(project)
    assert cfg.tools == {"stall_seconds": 600, "stall_repeats": 3}
    assert step_metrics.stall_thresholds(cfg) == (600, 3)
    assert step_metrics.stall_thresholds() == (300, 2)


# ───────────────────────── П4: комментарий в карточку ─────────────────────────

def test_tick_comments_stall_once(stand: Path, hermes_stub: Path):
    """Тик пишет «инструмент залип» в карточку залипшего прогона и не повторяет его (Д7)."""
    seed_card(hermes_stub)
    run_id = add_run(hermes_stub)
    cfg = config_mod.load(stand)
    p = gate.resolve_paths(cfg, "1")

    gate.run_gate(p, notify_fn=lambda text: None)
    found = stall_comments(hermes_stub)
    assert len(found) == 1
    assert found[0]["task_id"] == "t_step01"
    assert found[0]["body"] == ("инструмент залип: patch, вызовов 2,"
                                " суммарно 691 с, таймаутов 0")
    state = json.loads(Path(p.state_file).read_text(encoding="utf-8"))
    assert state["stall_reported"] == [str(run_id)]

    gate.run_gate(p, notify_fn=lambda text: None)
    assert len(stall_comments(hermes_stub)) == 1
    state = json.loads(Path(p.state_file).read_text(encoding="utf-8"))
    assert state["stall_reported"] == [str(run_id)]


# ───────────────────────── П5: правило метода в теле карточки ─────────────────────────

def test_next_attempt_body_carries_rule(project: Path):
    """Строка с `stall: true` даёт блок «ПРАВИЛО МЕТОДА» до «ЖЁСТКИХ ПРАВИЛ»."""
    cfg = config_mod.load(project)
    p = gate.resolve_paths(cfg, "1")
    stall = step_metrics.stall_of([ev("patch", 320), ev("patch", 371.25)])
    write_row(cfg, ts=1000, stall=True, stall_tool=stall["tool"],
              stall_calls=stall["calls"], stall_s=stall["s"],
              stall_timeouts=stall["timeouts"])

    body = gate.step_card_body(p, MIN_STEP)
    assert body.index("ПРАВИЛО МЕТОДА") < body.index("ЖЁСТКИЕ ПРАВИЛА")
    assert "patch" in body and "целой записью" in body and "300 с" in body


def test_clean_attempt_hides_rule(project: Path):
    """Последняя строка чистая — правила нет; старой залипшей строке его не воскресить."""
    cfg = config_mod.load(project)
    p = gate.resolve_paths(cfg, "1")
    assert "ПРАВИЛО МЕТОДА" not in gate.step_card_body(p, MIN_STEP)

    write_row(cfg, ts=1000, stall=True, stall_tool="patch", stall_calls=2, stall_s=691,
              stall_timeouts=0)
    assert "ПРАВИЛО МЕТОДА" in gate.step_card_body(p, MIN_STEP)

    clean = {"plan": "1", "step": "S1", "card": "t_step01", "card_kind": "step",
             "run_id": "2", "stall": False, "ts": 2000}
    path = step_metrics.metrics_path(cfg)
    path.write_text(path.read_text(encoding="utf-8")
                    + json.dumps(clean, ensure_ascii=False) + "\n", encoding="utf-8")
    assert "ПРАВИЛО МЕТОДА" not in gate.step_card_body(p, MIN_STEP)

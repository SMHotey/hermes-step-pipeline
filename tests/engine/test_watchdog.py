# -*- coding: utf-8 -*-
"""Сторож простоя и дневная сводка: тик сам сообщает, что конвейер стоит.

Часы подменяются (`gate._now`), поэтому сутки ожидания проходят за один вызов tick().
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

import pytest

from stub_hermes import SCHEMA

import config as config_mod
import gate

T0 = datetime(2026, 11, 2, 3, 0).timestamp()      # локальные 03:00 — раньше digest_at


def state_path(root: Path) -> Path:
    """Файл состояния плана 1 (registry.py:72-73)."""
    return root / ".pipeline" / "state" / "plan_1_state.json"


def read_state(root: Path) -> dict:
    path = state_path(root)
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}


def write_state(root: Path, **fields) -> None:
    data = read_state(root)
    data.update(fields)
    state_path(root).parent.mkdir(parents=True, exist_ok=True)
    state_path(root).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def hold(root: Path, steps=("S1",)) -> None:
    """Ручной допуск: детерминированный idle без карточек и без борды."""
    write_state(root, hold_steps=list(steps))


def write_owner_stop(root: Path, reason: str) -> None:
    """Стоп владельца: причина попадает в idle и меняется без сброса ожидания."""
    path = root / ".pipeline" / "state" / "plan_1_stop.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"reason": reason, "source": "owner", "kind": "owner"},
                               ensure_ascii=False), encoding="utf-8")


def done_step_card(db: Path, card_id: str, step: str, done_at: float) -> None:
    """Готовая карточка шага в поддельной борде (колонки — stub_hermes.SCHEMA)."""
    con = sqlite3.connect(db)
    try:
        con.executescript(SCHEMA)
        con.execute("INSERT OR REPLACE INTO tasks (id, title, status, assignee,"
                    " idempotency_key, completed_at, result, last_failure_error, created_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?)",
                    (card_id, "шаг", "done", "demo", f"planloop:1:{step}:step",
                     int(done_at), "", "", 0))
        con.commit()
    finally:
        con.close()


def notices(capsys) -> list[str]:
    """Строки уведомлений из stderr (Telegram выключен — notify печатает туда)."""
    return [line for line in capsys.readouterr().err.splitlines() if "[notify]" in line]


def digest_text(capsys) -> str:
    """Перехваченный stderr целиком: сводка многострочная, а префикс `[notify]` — только
    у первой строки, поэтому блок ищем по началу сообщения, а не по префиксу."""
    return capsys.readouterr().err


class Clock:
    def __init__(self, start: float) -> None:
        self.now = float(start)

    def set(self, moment) -> None:
        self.now = moment.timestamp() if hasattr(moment, "timestamp") else float(moment)


@pytest.fixture()
def clock(monkeypatch) -> Clock:
    """Подмена часов движка: gate._now возвращает управляемое время (решение D3)."""
    holder = Clock(T0)
    monkeypatch.setattr(gate, "_now", lambda: holder.now)
    return holder


@pytest.fixture()
def cfg(project: Path, hermes_stub: Path, monkeypatch) -> config_mod.Config:
    """Конфиг временного проекта: уведомления в stderr, шаг без постановки не блокирует."""
    for name in ("PIPELINE_CONFIG", "PIPELINE_PLANS", "PIPELINE_STATE_DIR", "PIPELINE_BOARD"):
        monkeypatch.delenv(name, raising=False)
    # collector() запускает next_step.py, а тот берёт корень из PIPELINE_REPO (:396-401).
    monkeypatch.setenv("PIPELINE_REPO", str(project))
    raw = json.loads((project / "pipeline.json").read_text(encoding="utf-8"))
    raw["notify"] = {"enabled": False, "watchdog_hours": 3, "digest_at": "09:00"}
    raw["require_prompt"] = False          # иначе create_step заказывает постановку, а не шаг
    (project / "pipeline.json").write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    return config_mod.load(project)


def test_idle_records_waiting_since_and_reason(project: Path, cfg, clock, capsys):
    hold(project)
    gate.tick(cfg)
    state = read_state(project)
    assert state["waiting_since"] == T0
    assert "ручного допуска" in state["waiting_reason"]
    assert notices(capsys) == []
    clock.set(T0 + 600)
    gate.tick(cfg)
    assert read_state(project)["waiting_since"] == T0


def test_watchdog_notifies_once_per_hour(project: Path, cfg, clock, capsys):
    hold(project)
    gate.tick(cfg)
    clock.set(T0 + 4 * 3600)
    gate.tick(cfg)
    lines = notices(capsys)
    assert len(lines) == 1
    assert "простой" in lines[0] and "4 ч" in lines[0]
    clock.set(T0 + 4 * 3600 + 20 * 60)
    gate.tick(cfg)
    assert notices(capsys) == []
    clock.set(T0 + 5 * 3600 + 10 * 60)
    gate.tick(cfg)
    lines = notices(capsys)
    assert len(lines) == 1
    assert "простой" in lines[0]


def test_new_reason_notifies_without_waiting_an_hour(project: Path, cfg, clock, capsys):
    write_owner_stop(project, "жду ответа владельца")
    gate.tick(cfg)
    clock.set(T0 + 4 * 3600)
    gate.tick(cfg)
    lines = notices(capsys)
    assert len(lines) == 1
    assert "жду ответа владельца" in lines[0]
    write_owner_stop(project, "жду решения по релизу")
    clock.set(T0 + 4 * 3600 + 5 * 60)
    gate.tick(cfg)
    lines = notices(capsys)
    assert len(lines) == 1
    assert "жду решения по релизу" in lines[0]
    state = read_state(project)
    assert state["waiting_since"] == T0
    assert "жду решения по релизу" in state["waiting_reason"]


def test_digest_once_per_day_at_configured_time(project: Path, hermes_stub: Path, cfg, clock, capsys):
    hold(project)
    done_step_card(hermes_stub, "t_aaaaaa1", "S1", T0 - 3600)
    done_step_card(hermes_stub, "t_aaaaaa2", "S2", T0 - 30 * 3600)
    gate.tick(cfg)
    assert digest_text(capsys).count("📊 сводка за сутки") == 0
    clock.set(datetime(2026, 11, 2, 9, 0))
    gate.tick(cfg)
    err = digest_text(capsys)
    assert err.count("📊 сводка за сутки") == 1
    assert "закрыто за сутки: 1" in err
    assert "план 1:" in err
    assert "ждёт —" in err
    assert "стоп — нет" in err
    clock.set(datetime(2026, 11, 2, 15, 0))
    gate.tick(cfg)
    assert digest_text(capsys).count("📊 сводка за сутки") == 0
    clock.set(datetime(2026, 11, 3, 9, 0))
    gate.tick(cfg)
    err = digest_text(capsys)
    assert err.count("📊 сводка за сутки") == 1
    assert "03.11.2026" in err


def test_action_resets_waiting(project: Path, cfg, clock, capsys):
    hold(project)
    gate.tick(cfg)
    assert "waiting_since" in read_state(project)
    write_state(project, hold_steps=[])
    clock.set(T0 + 10 * 60)
    gate.tick(cfg)
    state = read_state(project)
    assert state["last_action"].startswith("create_step")
    assert "waiting_since" not in state
    assert "waiting_reason" not in state
    clock.set(T0 + 40 * 60)
    gate.tick(cfg)
    assert [line for line in notices(capsys) if "простой" in line] == []


def test_messages_go_to_stderr_when_telegram_off(project: Path, cfg, clock, capsys, monkeypatch):
    monkeypatch.delenv("PIPELINE_TG_TOKEN", raising=False)
    monkeypatch.delenv("PIPELINE_TG_CHAT", raising=False)
    hold(project)
    gate.tick(cfg)
    clock.set(datetime(2026, 11, 2, 9, 0))
    gate.tick(cfg)
    out = capsys.readouterr()
    assert "[notify]" in out.err
    assert "простой" in out.err
    assert "сводка" in out.err
    assert "простой" not in out.out
    assert "сводка" not in out.out

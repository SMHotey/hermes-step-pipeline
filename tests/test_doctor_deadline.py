# -*- coding: utf-8 -*-
"""Диагностика видит висящие карточки раньше стража (план 10, S2A).

Прецедент: карточка провисела в `todo` шесть дней, и узнать об этом можно было только
руками по `hermes kanban show`. `hermes pipeline doctor` обязан показать её сам — тем же
правилом (`gate.stuck_cards`) и тем же порогом (`watchdog.card_deadline_hours`), каким
судит страж: второй разбор дал бы владельцу два разных ответа на один вопрос.

Файл держит ровно пять тестов из постановки (З5): равенство порога с `config.load`,
пустая борда, скобка «ближайшая через …», счёт без скобки и выключенная проверка; шестой
кейс — «архивная карточка в счёт не идёт» (находка F1 круга правок).
Часы подменяются там же, где их читает диагностика: `doctor` держит модуль движка
(`gate_mod`), и `gate` ниже — он же, поэтому второй точки подмены времени не появляется.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from helpers import KANBAN_SCHEMA
from step_pipeline import doctor
from step_pipeline import project as proj
from step_pipeline import snapshot as snap

#: Модуль движка, который читает диагностика: подмена `gate._now` действует ровно тому
#: объекту, чьи `stuck_cards`/`_ago` зовёт `doctor.collect` (а не второй копии модуля).
gate = doctor.gate_mod
#: Разбор конфига того же движка — на нём проверяется равенство порогов (D1).
config_mod = gate.config_mod

NOW = datetime(2026, 11, 20, 12, 0).timestamp()
HOUR = 3600


@pytest.fixture()
def clock(monkeypatch) -> float:
    """Часы движка: единственная точка подмены времени у стража и у диагностики (З2)."""
    monkeypatch.setattr(gate, "_now", lambda: NOW)
    return NOW


@pytest.fixture()
def quiet(project: Path, monkeypatch, tmp_path: Path) -> Path:
    """Проект с погашенными внешними источниками (как `test_doctor_notify`/`_drift`)."""
    monkeypatch.setattr(proj, "profile_dir", lambda name: tmp_path / "profiles" / name)
    monkeypatch.setattr(snap, "snapshot", lambda *a, **k: {})
    monkeypatch.setattr(proj, "run_hermes",
                        lambda *a, **k: SimpleNamespace(returncode=0, stdout="", stderr=""))
    return project


@pytest.fixture()
def board(hermes_stub: Path) -> Path:
    """Пустая борда проекта: без файла диагностика говорит «нет sqlite борды»."""
    con = sqlite3.connect(hermes_stub)
    try:
        con.executescript(KANBAN_SCHEMA)
        con.commit()
    finally:
        con.close()
    return hermes_stub


def set_config(project: Path, *, runtimes_step: str | None = None, **watchdog) -> None:
    """Правка `pipeline.json`: раздел `watchdog` и, если задан, `runtimes.step`."""
    path = project / "pipeline.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    if runtimes_step is not None:
        runtimes = dict(raw.get("runtimes") or {})
        runtimes["step"] = runtimes_step
        raw["runtimes"] = runtimes
    raw["watchdog"] = watchdog
    path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")


def add_card(db: Path, card_id: str, *, age_seconds: int, status: str = "todo",
             key: str = "planloop:1:S1:step") -> None:
    """Карточка с меткой создания от `NOW`: иначе `_ago` даёт соседнюю минуту."""
    con = sqlite3.connect(db)
    try:
        con.executescript(KANBAN_SCHEMA)
        con.execute("INSERT OR REPLACE INTO tasks (id, title, status, assignee,"
                    " idempotency_key, created_at) VALUES (?,?,?,?,?,?)",
                    (card_id, f"шаг {card_id}", status, "demo", key, int(NOW - age_seconds)))
        con.commit()
    finally:
        con.close()


def cards_note(report: doctor.Report) -> list[str]:
    return [item for item in report.notes if item.startswith("висящие карточки")]


def limit_warn(report: doctor.Report) -> list[str]:
    """Только предупреждения о пределе: в `report.warnings` живут и чужие находки (cron)."""
    return [item for item in report.warnings if item.startswith("предел ожидания")]


class TestCardsRow:
    def test_threshold_matches_engine_config(self, quiet: Path, monkeypatch):
        """D1: порог диагностики — тот же, что у стража (`config.load(...).watchdog`)."""
        set_config(quiet, heartbeat_fresh_minutes="15", card_deadline_hours="12")
        raw = proj.read_config(quiet)
        assert gate.watchdog_limits(raw) == {"heartbeat_fresh_minutes": 15,
                                             "card_deadline_hours": 12}
        for name in ("PIPELINE_CONFIG", "PIPELINE_PLANS", "PIPELINE_STATE_DIR",
                     "PIPELINE_BOARD"):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv("PIPELINE_REPO", str(quiet))
        assert config_mod.load(quiet).watchdog == gate.watchdog_limits(raw)

    def test_empty_board_says_none(self, quiet: Path, board: Path, clock):
        report = doctor.collect(quiet, with_lint=False)
        assert cards_note(report) == ["висящие карточки: нет"]
        assert limit_warn(report) == []

    def test_nearest_is_named_with_bracket(self, quiet: Path, board: Path, clock):
        """13 ч за пределом, 9 ч 50 мин — ближайшая: остаток 2 ч 10 мин, шаг 14 ч."""
        set_config(quiet, card_deadline_hours=12, runtimes_step="14h")
        add_card(board, "t_old", age_seconds=13 * HOUR)
        add_card(board, "t_near", age_seconds=9 * HOUR + 50 * 60)
        report = doctor.collect(quiet, with_lint=False)
        assert cards_note(report) == ["висящие карточки: 1 (ближайшая через 2 ч 10 мин)"]
        assert len(limit_warn(report)) == 1
        assert "runtimes.step=14h" in limit_warn(report)[0]
        assert "card_deadline_hours=12" in limit_warn(report)[0]

    def test_all_expired_counts_without_bracket(self, quiet: Path, board: Path, clock):
        """Все нетерминальные карточки за пределом — скобки нет; шаг 3 ч короче предела."""
        set_config(quiet, card_deadline_hours=12, runtimes_step="3h")
        add_card(board, "t_old", age_seconds=13 * HOUR)
        add_card(board, "t_older", age_seconds=20 * HOUR)
        report = doctor.collect(quiet, with_lint=False)
        assert cards_note(report) == ["висящие карточки: 2"]
        assert limit_warn(report) == []

    def test_archived_card_is_not_counted(self, quiet: Path, board: Path, clock):
        """F1: архивная карточка — закрытая; считаем только действительно висящую."""
        set_config(quiet, card_deadline_hours=12, runtimes_step="14h")
        add_card(board, "t_archived", age_seconds=356 * HOUR, status="archived")
        add_card(board, "t_stale", age_seconds=13 * HOUR)
        report = doctor.collect(quiet, with_lint=False)
        assert cards_note(report) == ["висящие карточки: 1"]
        assert len(limit_warn(report)) == 1

    def test_zero_limit_says_check_is_off(self, quiet: Path, board: Path, clock):
        """Предел 0 и мусор в `runtimes.step`: проверка выключена, предупреждений нет."""
        set_config(quiet, card_deadline_hours=0, runtimes_step="три часа")
        add_card(board, "t_stale", age_seconds=300 * HOUR)
        report = doctor.collect(quiet, with_lint=False)
        assert cards_note(report) == ["висящие карточки: проверка выключена"]
        assert limit_warn(report) == []

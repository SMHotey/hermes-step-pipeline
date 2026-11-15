# -*- coding: utf-8 -*-
"""Пауза по расписанию: окна системного времени, в которые тик не работает (0.28.2).

Проверяется то, от чего зависит вся настройка: разбор `quiet_hours` (мусор — ошибка
конфигурации, а не молчаливое умолчание), попадание минуты в окно (включая окно через
полночь и невключаемый конец), ответ `--pause` для панели и главное — тик в окне не
трогает ни доску, ни состояние плана и выходит с кодом 0.
"""
from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime
from pathlib import Path

import pytest

import config as config_mod
import gate
from engine_kit import cards, make_project


def created_cards(db: Path) -> list[dict]:
    """Карточки поддельной борды; пустая база — карточек не заказывали вовсе."""
    if not db.exists():
        return []
    try:
        return cards(db)
    except sqlite3.OperationalError:      # таблицы ещё нет: ни одной карточки не создавали
        return []


def moment(hour: int, minute: int) -> float:
    """Epoch локального момента: 15.11.2026, ЧЧ:ММ — та же дата, что у остальных тестов."""
    return time.mktime(datetime(2026, 11, 15, hour, minute).timetuple())


def write_hours(root: Path, value) -> Path:
    """Положить `quiet_hours` в pipeline.json проекта (остальное не трогая)."""
    path = root / "pipeline.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["quiet_hours"] = value
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


@pytest.fixture()
def night_project(tmp_path: Path, hermes_stub: Path, monkeypatch):
    """Проект с ночным окном 23:00–07:00 и обеденным 13:00–14:00; карточки — по требованию."""
    root = make_project(tmp_path / "quiet", require_prompt=False)
    write_hours(root, {"enabled": True,
                       "windows": [{"from": "23:00", "to": "07:00"},
                                   {"from": "13:00", "to": "14:00"}]})
    monkeypatch.setenv("PIPELINE_REPO", str(root))
    return root


# ───────────────────────────── разбор конфигурации ─────────────────────────────

def test_defaults_have_pause_off(project: Path):
    """Умолчание — выключено: поведение проектов, не знающих о ключе, не меняется."""
    cfg = config_mod.load(project)
    assert cfg.quiet_hours == {"enabled": False, "windows": []}
    assert config_mod.pause_state(cfg, moment(23, 30))["paused"] is False


def test_windows_are_parsed_in_order(project: Path):
    write_hours(project, {"enabled": True,
                          "windows": [{"from": "23:00", "to": "07:00"},
                                      {"from": "13:00", "to": "14:00"}]})
    cfg = config_mod.load(project)
    assert cfg.quiet_hours["enabled"] is True
    assert cfg.quiet_hours["windows"] == [{"from": "23:00", "to": "07:00"},
                                          {"from": "13:00", "to": "14:00"}]


@pytest.mark.parametrize("value, expected", [
    ("полдень", "quiet_hours"),
    ({"enabled": "да", "windows": []}, "enabled"),
    ({"enabled": True, "windows": {"from": "23:00", "to": "07:00"}}, "windows"),
    ({"enabled": True, "windows": [{"from": "7:00", "to": "07:30"}]}, "windows[1].from"),
    ({"enabled": True, "windows": [{"from": "25:00", "to": "07:00"}]}, "windows[1].from"),
    ({"enabled": True, "windows": [{"from": "23:60", "to": "07:00"}]}, "windows[1].from"),
    ({"enabled": True, "windows": [{"from": "23:00"}]}, "windows[1].to"),
    ({"enabled": True, "windows": [{"from": "23:00", "to": "23:00"}]}, "совпадают"),
    ({"enabled": True, "windows": [{"from": "23:00", "to": "07:00"},
                                   {"from": "23:00", "to": "07:00"}]}, "уже задан"),
    ({"enabled": True, "windows": [{"from": "23:00", "to": "07:00"},
                                   {"from": "12:00", "to": "13:00"},
                                   {"from": "15:00", "to": "16:00"}]}, "не больше 2"),
    ({"enabled": True, "windows": ["23:00-07:00"]}, "windows[1]"),
])
def test_bad_value_is_config_error(project: Path, value, expected: str):
    """Мусор в паузе — ошибка с именем поля: молча тикать «не тогда» нельзя."""
    write_hours(project, value)
    with pytest.raises(config_mod.ConfigError) as exc:
        config_mod.load(project)
    assert expected in str(exc.value), str(exc.value)


def test_enabled_without_windows_is_allowed(project: Path):
    """Включённая пауза без окон законна: конвейер идёт круглосуточно, и это видно словами."""
    write_hours(project, {"enabled": True, "windows": []})
    cfg = config_mod.load(project)
    state = config_mod.pause_state(cfg, moment(23, 30))
    assert state["paused"] is False
    assert "интервалы не заданы" in state["reason"]


# ───────────────────────────── попадание в окно ─────────────────────────────

@pytest.mark.parametrize("hour, minute, paused", [
    (23, 0, True),        # начало окна включается
    (23, 30, True),
    (0, 0, True),         # окно через полночь
    (6, 59, True),
    (7, 0, False),        # конец окна не включается: в 07:00 тик уже работает
    (12, 59, False),
    (13, 0, True),        # второе окно
    (13, 59, True),
    (14, 0, False),
    (22, 59, False),
])
def test_window_boundaries(project: Path, hour: int, minute: int, paused: bool):
    write_hours(project, {"enabled": True,
                          "windows": [{"from": "23:00", "to": "07:00"},
                                      {"from": "13:00", "to": "14:00"}]})
    cfg = config_mod.load(project)
    state = config_mod.pause_state(cfg, moment(hour, minute))
    assert state["paused"] is paused, (hour, minute, state["reason"])


def test_paused_state_names_window_and_end(project: Path):
    write_hours(project, {"enabled": True,
                          "windows": [{"from": "23:00", "to": "07:00"}]})
    cfg = config_mod.load(project)
    state = config_mod.pause_state(cfg, moment(2, 5))
    assert state["paused"] is True
    assert state["window"] == {"from": "23:00", "to": "07:00"}
    assert state["until"] == "07:00"
    assert state["at"] == "02:05"
    assert "23:00–07:00" in state["reason"] and "до 07:00" in state["reason"]


def test_disabled_pause_never_pauses(project: Path):
    """Окна заданы, но пауза выключена: тики идут в любое время."""
    write_hours(project, {"enabled": False,
                          "windows": [{"from": "00:00", "to": "23:59"}]})
    cfg = config_mod.load(project)
    assert config_mod.pause_state(cfg, moment(3, 0))["paused"] is False


# ───────────────────────────── тик и --pause ─────────────────────────────

def test_pause_flag_answers_json(night_project: Path, capsys, monkeypatch):
    """`--pause --json` — та же форма, что читает панель: без плана и без блокировки."""
    monkeypatch.setattr(gate, "_now", lambda: moment(23, 30))
    assert gate.main(["--pause", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["paused"] is True and data["until"] == "07:00"
    assert data["windows"][0] == {"from": "23:00", "to": "07:00"}


def test_tick_inside_window_does_nothing(night_project: Path, hermes_stub: Path,
                                         capsys, monkeypatch):
    """Тик в окне: код 0, карточек нет, состояние плана не создано, в выводе — окно."""
    monkeypatch.setattr(gate, "_now", lambda: moment(23, 30))
    assert gate.main(["--plan", "1", "--run"]) == 0
    out = capsys.readouterr().out
    assert "пауза по расписанию" in out, out
    assert created_cards(hermes_stub) == [], "в окне паузы карточки не заказываются"
    state_dir = night_project / ".pipeline" / "state"
    assert not (state_dir / "plan_1_state.json").exists(), "состояние плана не тронуто"


def test_single_plan_branch_is_paused_too(night_project: Path, hermes_stub: Path,
                                          monkeypatch):
    """Ветка `--plan N --run` (её зовут панель и бот) паузу слушает так же, как `--run`."""
    monkeypatch.setattr(gate, "_now", lambda: moment(13, 30))
    assert gate.main(["--run"]) == 0
    assert created_cards(hermes_stub) == []


def test_tick_outside_window_works(night_project: Path, hermes_stub: Path,
                                   capsys, monkeypatch):
    """Вне окна тик работает как обычно: разница с паузой — не «тик вообще не ходит»."""
    monkeypatch.setattr(gate, "_now", lambda: moment(10, 0))
    assert gate.main(["--plan", "1", "--run"]) == 0
    assert [card["key"] for card in created_cards(hermes_stub)] == ["planloop:1:S1:step"]


def test_owner_stop_ignores_pause(night_project: Path, monkeypatch):
    """Стоп и допуск — команды владельца: в окне паузы они работают (иначе не снять стоп ночью)."""
    monkeypatch.setattr(gate, "_now", lambda: moment(23, 30))
    assert gate.main(["--plan", "1", "--stop", "остановка владельца"]) == 0
    stop = json.loads((night_project / ".pipeline" / "state" / "plan_1_stop.json")
                      .read_text(encoding="utf-8"))
    assert stop["reason"] == "остановка владельца"

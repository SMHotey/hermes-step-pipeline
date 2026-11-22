# -*- coding: utf-8 -*-
"""Тик по адресу слушается ведения: план вне `driven_plans` работу не получает.

План 14, шаг S1. Правило уже есть у безадресного тика (`tick()` обходит
`registry.driven_specs`), а адресный `--run --plan N` шёл мимо него: `resolve_paths`
подставлял план и ведение не сверял — так 15.11.2026 страж заказал постановку шага
плана, который конвейер не ведёт (прецедент 6-S8B).

Что держат кейсы: адрес ведомого плана тикает; адрес плана вне ведения получает код 2
без карточек, стоп-флага и состояния (и в `--json`); опечатка в номере остаётся
«план не найден» без совета про ведение; команды владельца на неведо́мом плане работают;
безадресный `--run` при `driven_plans: []` остаётся кодом 0 без действий; фолбэк
инцидентного стопа идёт к первому ведомому плану, а не к наименьшему по номеру.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import config as config_mod
import gate
from engine_kit import make_project, write_approval

PLAN2 = """# 2. Демо-план по адресу

Цель: второй план проекта — адрес тика.

## Шаги

### S1. Второй план, первый шаг (1 дн)

- Файл: `app/second.py`, `tests/test_second.py`
- Тесты/гейты: `python -m pytest tests/test_second.py -q` → 2 passed
- Готово = файл `app/second.py` создан, 2 теста зелёные

## Журнал выполнения

| Дата | Шаг | Коммиты | Прогоны и доказательства | Статус |
|---|---|---|---|---|
| — | S1 | — | — | открыт |
"""


def run_gate(root: Path, *args: str) -> subprocess.CompletedProcess:
    """Запустить `gate.py` проекта-приёмника как отдельный процесс."""
    return subprocess.run([sys.executable, str(root / ".pipeline" / "gate.py"), *args],
                          cwd=str(root), capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=120)


def board_cards(db: Path) -> list[dict]:
    """Карточки поддельной борды; базы или таблицы нет — карточек нет."""
    if not db.is_file():
        return []
    con = sqlite3.connect(db)
    try:
        rows = con.execute(
            "SELECT id, idempotency_key FROM tasks ORDER BY created_at").fetchall()
    except sqlite3.OperationalError:      # файл создан, таблицы ещё нет
        return []
    finally:
        con.close()
    return [{"id": r[0], "key": r[1]} for r in rows]


def state_dir(root: Path) -> Path:
    return root / ".pipeline" / "state"


def make_address_project(tmp_path: Path, name: str = "address") -> Path:
    """Проект, где ведётся только план 2, а план 1 лежит рядом вне ведения."""
    root = make_project(tmp_path / name, driven_plans=["2"])
    plan2 = root / "docs" / "plans" / "2-address.md"
    plan2.write_text(PLAN2, encoding="utf-8")
    write_approval(root, plan2)
    return root


def assert_plan_one_untouched(root: Path, plan_bytes: bytes) -> None:
    """Отказ не оставил следа: ни состояния, ни стопа, ни правки файла плана."""
    assert not (state_dir(root) / "plan_1_state.json").exists()
    assert not (state_dir(root) / "plan_1_stop.json").exists()
    assert (root / "docs" / "plans" / "1-demo.md").read_bytes() == plan_bytes


def test_addressed_tick_acts_on_driven_plan(tmp_path: Path, hermes_stub: Path):
    root = make_address_project(tmp_path)
    res = run_gate(root, "--plan", "2", "--run")
    assert res.returncode == 0, res.stderr
    keys = {card["key"] for card in board_cards(hermes_stub)}
    # Тик не «молча ничего»: план 2 утверждён, постановки нет — страж заказывает её промптеру.
    assert "planloop:2:S1:prompt" in keys
    assert not any(key.startswith("planloop:1:") for key in keys)


def test_addressed_tick_refuses_plan_outside_driven(tmp_path: Path, hermes_stub: Path):
    root = make_address_project(tmp_path, "refused")
    plan_bytes = (root / "docs" / "plans" / "1-demo.md").read_bytes()
    res = run_gate(root, "--plan", "1", "--run")
    assert res.returncode == 2, res.stderr
    assert res.stdout.strip() == ""
    # Одна формулировка на движок и панель: та же фраза, что в строке плана.
    assert gate.NOT_DRIVEN_REASON in res.stderr
    assert f"план 1 {gate.NOT_DRIVEN_REASON}" in res.stderr
    assert "hermes pipeline config --set driven_plans" in res.stderr
    # Живой список ведения плюс запрошенный номер — то, что владелец вставит командой.
    assert '"2", "1"' in res.stderr
    # Отказ — не действие: ни карточек, ни стопа, ни состояния, ни правки плана.
    assert not any(card["key"].startswith("planloop:1:") for card in board_cards(hermes_stub))
    assert_plan_one_untouched(root, plan_bytes)


def test_refusal_json_shape(tmp_path: Path, hermes_stub: Path):
    root = make_address_project(tmp_path, "refused-json")
    plan_bytes = (root / "docs" / "plans" / "1-demo.md").read_bytes()
    res = run_gate(root, "--plan", "1", "--run", "--json")
    assert res.returncode == 2, res.stderr
    payload = json.loads(res.stdout)
    assert payload["ok"] is False
    assert payload["driven"] is False
    assert payload["plan_id"] == "1"
    assert gate.NOT_DRIVEN_REASON in payload["error"]
    assert board_cards(hermes_stub) == []
    assert_plan_one_untouched(root, plan_bytes)


def test_owner_commands_work_outside_driven(tmp_path: Path, hermes_stub: Path):
    # Допущение 3: смотреть состояние и ставить/снимать стоп конвейер не запрещает.
    root = make_address_project(tmp_path, "owner")
    status = run_gate(root, "--status", "--plan", "1")
    assert status.returncode == 0, status.stderr
    assert "План 1" in status.stdout

    stopped = run_gate(root, "--stop", "остановка владельца ради проверки", "--plan", "1")
    assert stopped.returncode == 0, stopped.stderr
    payload = json.loads((state_dir(root) / "plan_1_stop.json").read_text(encoding="utf-8"))
    assert payload["kind"] == "owner"

    resumed = run_gate(root, "--resume", "--plan", "1")
    assert resumed.returncode == 0, resumed.stderr
    assert "стоп-флаг снят" in resumed.stdout
    assert not (state_dir(root) / "plan_1_stop.json").exists()


def test_unknown_addressed_plan_still_says_not_found(tmp_path: Path, hermes_stub: Path):
    # D3: опечатка в номере не получает совет «включить план в driven_plans».
    root = make_address_project(tmp_path, "unknown")
    res = run_gate(root, "--plan", "99", "--run")
    assert res.returncode == 2, res.stderr
    assert "не найден" in res.stderr
    assert "driven_plans" not in res.stderr


def test_empty_driven_addressless_tick_does_nothing(tmp_path: Path, hermes_stub: Path):
    # Безадресный тик не переведён на `driven_default_id`: вести некого — код 0, действий нет.
    root = make_project(tmp_path / "empty-driven", driven_plans=[])
    res = run_gate(root, "--run")
    assert res.returncode == 0, res.stderr
    assert res.stdout.strip() == ""
    assert board_cards(hermes_stub) == []
    assert not (state_dir(root) / "plan_1_state.json").exists()
    assert not (state_dir(root) / "plan_1_stop.json").exists()


def test_incident_stop_goes_to_driven_plan(tmp_path: Path, hermes_stub: Path):
    # Фолбэк инцидента не пишет стоп плану вне ведения: раньше брался наименьший номер.
    root = make_address_project(tmp_path, "incident")
    cfg = config_mod.load(root)
    args = argparse.Namespace(run=True, plan=None, json=False)
    # Голый `RuntimeError`: у `GateError` бывает свой `plan_id`, и тогда фолбэк не проверяется.
    assert gate.incident(cfg, args, RuntimeError("сбой движка")) == 1
    stop = state_dir(root) / "plan_2_stop.json"
    assert stop.is_file(), "инцидентный стоп должен лечь первому ведомому плану"
    payload = json.loads(stop.read_text(encoding="utf-8"))
    assert payload["kind"] == "incident"
    assert "сбой движка" in payload["reason"]
    assert not (state_dir(root) / "plan_1_stop.json").exists()

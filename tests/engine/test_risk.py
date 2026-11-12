# -*- coding: utf-8 -*-
"""Риск шага: пометки «Риск»/«Авто», автоматический ручной допуск и его снятие."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import config as config_mod
import gate
import next_step
from engine_kit import write_approval

PLAN_RISK = """# 1. Демо-план конвейера

Цель: проверить, что страж ведёт шаги.

## Шаги

### S1. Опасный шаг (1 дн)

- Файл: `app/danger.py`
- Тесты/гейты: `python -m pytest tests/test_danger.py -q` → 1 passed
- Риск: high
- Готово = опасный шаг сделан

### S2. Безопасный шаг (1 дн)

- Файл: `app/safe.py`
- Тесты/гейты: `python -m pytest tests/test_safe.py -q` → 1 passed
- Риск: low
- Авто: да
- Готово = безопасный шаг сделан

### S3. Обычный шаг (1 дн)

- Файл: `app/plain.py`
- Тесты/гейты: `python -m pytest tests/test_plain.py -q` → 1 passed
- Готово = обычный шаг сделан

### S4. Опечатка в риске (1 дн)

- Файл: `app/typo.py`
- Тесты/гейты: `python -m pytest tests/test_typo.py -q` → 1 passed
- Риск: когда-нибудь
- Готово = шаг с нераспознанной пометкой

## Журнал выполнения

| Дата | Шаг | Коммиты | Прогоны и доказательства | Статус |
|---|---|---|---|---|
| — | S1 | — | — | открыт |
| — | S2 | — | — | открыт |
| — | S3 | — | — | открыт |
| — | S4 | — | — | открыт |
"""


def write_risk_plan(root: Path) -> Path:
    """Переписать демо-план пометками риска и переутвердить его.

    Запись утверждения хранит sha256 файла плана (шаг 4-S7), поэтому после правки
    плана её надо обновить — иначе страж честно откажется вести «изменённый» план.
    """
    path = Path(root) / "docs" / "plans" / "1-demo.md"
    path.write_text(PLAN_RISK, encoding="utf-8")
    write_approval(root, path)
    return path


@pytest.fixture()
def stand(project: Path, hermes_stub: Path, monkeypatch):
    """Пути плана 1 на временном проекте; сборщик шагов смотрит в PIPELINE_REPO."""
    monkeypatch.setenv("PIPELINE_REPO", str(project))
    cfg = config_mod.load(project)
    return cfg, gate.resolve_paths(cfg, "1")


def fake_card(created: list[str]):
    """Подпись `_create_card` (gate.py:792-794), которая только запоминает ключи."""

    def create(p, title, body, assignee, key, skills, parent=None, max_runtime=None):
        created.append(key)
        return "t_fake0001"

    return create


def test_risk_parsed_into_contract(project: Path, stand):
    cfg, p = stand
    write_risk_plan(project)
    got = {s.id: (s.risk, s.auto) for s in next_step.plan_steps(cfg, p.plan_file)}
    assert got["S1"] == ("high", False)
    assert got["S2"] == ("low", True)
    assert got["S3"] == ("normal", False)
    assert got["S4"] == ("high", False)      # нераспознанная пометка — fail-safe
    pkg = next_step.build_package(cfg, "1")
    assert pkg["next"]["risk"] == "high"
    dumped = json.dumps(pkg, ensure_ascii=False)
    assert '"risk"' in dumped
    assert '"auto"' in dumped


def test_high_risk_step_gets_hold_not_card(project: Path, hermes_stub: Path, stand):
    cfg, p = stand
    write_risk_plan(project)
    created: list[str] = []
    messages: list[str] = []
    res = gate.run_gate(p, execute=True, create_card=fake_card(created),
                        notify_fn=messages.append)
    assert res["decision"]["action"] == "hold_risk", res["decision"]
    assert created == []                     # ни карточки шага, ни заказа постановки
    assert gate.load_state(p)["hold_steps"] == ["S1"]
    assert len(messages) == 1, messages
    assert "Риск: high" in messages[0]
    assert "§S1" in messages[0]
    assert "--unhold S1" in messages[0]


def test_unhold_releases_high_risk_step(project: Path, hermes_stub: Path, stand):
    cfg, p = stand
    write_risk_plan(project)
    created: list[str] = []
    first = gate.run_gate(p, execute=True, create_card=fake_card(created),
                          notify_fn=lambda text: None)
    assert first["decision"]["action"] == "hold_risk"
    assert gate.main(["--unhold", "S1"]) == 0
    state = gate.load_state(p)
    assert state["hold_steps"] == []
    assert state["hold_released"] == ["S1"]
    (project / "docs" / "prompts" / "2026-11-01_1-S1_prompt.md").write_text(
        "# Промт S1\n", encoding="utf-8")
    res = gate.run_gate(p, execute=True, create_card=fake_card(created),
                        notify_fn=lambda text: None)
    assert res["decision"]["action"] == "create_step", res["decision"]
    assert "planloop:1:S1:step" in created
    assert gate.load_state(p)["hold_steps"] == []      # hold не вернулся


def decision_ctx(risk: str, auto: bool) -> dict:
    step = {"id": "S1", "epoch": "", "title": "Таблица счетов", "days": 1.0, "owner": "",
            "goal": "g", "files": [], "gates": [], "dod": "d", "read": [], "notes": "",
            "risk": risk, "auto": auto}
    return {"next": step, "cards": [], "prev_cards": [], "closed": [],
            "prev_closed": None, "prev_verdict": None, "stop": None, "hold": [],
            "released": [], "verdict": None, "journal_closed": False, "plan_id": "1"}


def test_auto_does_not_bypass_high_and_normal_starts():
    assert gate.decide(decision_ctx("normal", False))["action"] == "create_step"
    assert gate.decide(decision_ctx("low", True))["action"] == "create_step"
    assert gate.decide(decision_ctx("high", True))["action"] == "hold_risk"


def test_hold_visible_in_status_and_snapshot_source(project: Path, hermes_stub: Path, stand):
    cfg, p = stand
    write_risk_plan(project)
    assert gate.main(["--hold", "S1"]) == 0
    ctx = gate.build_ctx(p)
    d = gate.decide(ctx)
    assert d["action"] == "idle"
    text = gate.status_text(ctx, d)
    assert "Ручной допуск (hold): S1" in text
    assert "Риск шага: high" in text
    row = gate.plans_rows(cfg)[0]
    assert row["hold"] == ["S1"]
    assert row["risk"] == "high"
    assert "hold: S1" in gate.plans_table(cfg)


def test_hold_refuses_step_absent_from_plan(project: Path, stand):
    """Опечатка в номере шага не оседает в состоянии плана.

    `--hold` шлёт и панель (номер из выбранной вкладки), и бот: устаревшая страница
    присылает шаг, которого в плане уже нет. До 0.25.9 такой номер записывался в
    `hold_released` навсегда — снять его командой было нечем, а страж держал шаг.
    """
    cfg, p = stand
    assert "S1" in gate.declared_steps(cfg, p), "фикстура должна разбирать шаги плана"
    answer = gate.handle_command(cfg, "/hold NOPE-99")
    assert "нет шага NOPE-99" in answer
    assert "S1" in answer                       # отказ называет допустимые шаги
    assert not Path(p.state_file).exists(), "отказ не должен создавать состояние"


def test_cli_hold_refuses_unknown_step(project: Path, stand, capsys):
    """Тот же отказ в CLI: код 2 и причина в stderr — её показывает панель."""
    for flag in ("--hold", "--unhold"):
        assert gate.main(["--plan", "1", flag, "NOPE-99"]) == 2
        assert "нет шага NOPE-99" in capsys.readouterr().err


def test_unhold_of_released_step_stays_allowed(project: Path, stand):
    """Снятие допуска идемпотентно: повторный `--unhold` не отказ.

    Шаг после снятия живёт в `hold_released`, а не в шагах плана — проверка номера
    обязана это учитывать, иначе владелец получал бы отказ на собственном повторе.
    """
    cfg, p = stand
    assert "S1" in gate.handle_command(cfg, "/hold S1")
    assert "—" in gate.handle_command(cfg, "/unhold S1")
    assert "—" in gate.handle_command(cfg, "/unhold S1")
    assert gate.load_state(p)["hold_released"] == ["S1"]


def test_risk_hold_is_idempotent(project: Path, hermes_stub: Path, stand):
    cfg, p = stand
    write_risk_plan(project)
    created: list[str] = []
    messages: list[str] = []
    first = gate.run_gate(p, execute=True, create_card=fake_card(created),
                          notify_fn=messages.append)
    second = gate.run_gate(p, execute=True, create_card=fake_card(created),
                           notify_fn=messages.append)
    assert first["decision"]["action"] == "hold_risk"
    assert first["applied"] is True
    assert second["decision"]["action"] == "idle"
    assert second["applied"] is False
    assert created == []
    assert gate.load_state(p)["hold_steps"] == ["S1"]     # одна запись, без дублей
    assert len(messages) == 1

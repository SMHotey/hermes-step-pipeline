# -*- coding: utf-8 -*-
"""Лимит кругов правок по риску шага и классы серий (план 12, S3).

Зачем файл: `max_fix_rounds` перестаёт быть ручной настройкой под конкретный шаг.
Проверяется, что ступень берётся из риска шага (`high` → `max_fix_rounds_high`,
`low`/`normal` → база), что стоп при исчерпании называет цифру и класс, что карточка
правок говорит «круг N из M», и что классификация серий не даёт пересборке по свежести
или повторному ревью съесть бюджет правок.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

import config as config_mod
import gate
from engine_kit import write_approval
from stub_hermes import SCHEMA

PLAN_RISK = """# 1. Демо-план конвейера

Цель: проверить лимит кругов правок по риску шага.

## Шаги

### S1. Шаг под проверкой (1 дн)

- Файл: `app/step.py`
- Тесты/гейты: `python -m pytest tests/test_step.py -q` → 1 passed
{risk_line}- Готово = шаг сделан

## Журнал выполнения

| Дата | Шаг | Коммиты | Прогоны и доказательства | Статус |
|---|---|---|---|---|
| — | S1 | — | — | открыт |
"""


def step_contract(risk: str) -> dict:
    """Контракт шага S1 в том виде, в каком его отдаёт сборщик шагов."""
    return {"id": "S1", "epoch": "", "title": "Шаг под проверкой", "days": 1.0,
            "owner": "", "goal": "g", "files": [], "gates": [], "dod": "d", "read": [],
            "notes": "", "risk": risk, "auto": False}


REPORT_REL = "docs/reviews/1-S1.md"
FINDINGS = """## ВЕРДИКТ: REQUEST_CHANGES

### Блокирующие

1. **[блокирующая]** `app/step.py:12` — проверка не покрыта тестом
"""


def write_risk_plan(root: Path, risk: str = "normal") -> Path:
    """Переписать демо-план пометкой риска и переутвердить его (запись хранит sha)."""
    path = Path(root) / "docs" / "plans" / "1-demo.md"
    risk_line = f"- Риск: {risk}\n" if risk else ""
    path.write_text(PLAN_RISK.format(risk_line=risk_line), encoding="utf-8")
    write_approval(root, path)
    return path


def insert_card(db: Path, card_id: str, key: str, status: str = "done",
                created_at: int = 0, result: str = "") -> None:
    """Карточка в поддельной борде (колонки — `stub_hermes.SCHEMA`)."""
    con = sqlite3.connect(db)
    try:
        con.executescript(SCHEMA)
        con.execute(
            "INSERT OR REPLACE INTO tasks (id, title, status, assignee, idempotency_key,"
            " completed_at, result, last_failure_error, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (card_id, "карточка", status, "demo", key, created_at, result, "", created_at))
        con.commit()
    finally:
        con.close()


def fix_state(root: Path, db: Path, rounds: int, *, step: str = "S1") -> None:
    """Состояние «ревью вернуло REQUEST_CHANGES после `rounds` кругов правок».

    Круги нумеруются так же, как их считает страж: карточка `:fix:{N}` и следующее за
    ней ревью `:review:{N+1}`. `rounds=0` — первый отказ, правок ещё не было.
    """
    folder = Path(root) / "docs" / "prompts"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"2026-11-15_1-{step}_prompt.md").write_text(
        f"# Постановка {step}\n", encoding="utf-8")
    insert_card(db, f"t_step_{step}", f"planloop:1:{step}:step", created_at=1)
    insert_card(db, f"t_rev_{step}_1", f"planloop:1:{step}:review:1", created_at=2)
    for number in range(1, rounds + 1):
        insert_card(db, f"t_fix_{step}_{number}", f"planloop:1:{step}:fix:{number}",
                    created_at=2 + number * 2)
        insert_card(db, f"t_rev_{step}_{number + 1}",
                    f"planloop:1:{step}:review:{number + 1}", created_at=3 + number * 2)
    report = Path(root) / REPORT_REL
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(FINDINGS, encoding="utf-8")


def captured_create(created: list[tuple[str, str]]):
    """Подпись `_create_card`, которая запоминает ключ и тело карточки."""

    def create(p, title, body, assignee, key, skills, parent=None, max_runtime=None):
        created.append((key, body))
        return "t_fake0001"

    return create


def quiet(_text: str) -> None:
    """Заглушка уведомлений."""


@pytest.fixture()
def stand(project: Path, hermes_stub: Path, monkeypatch):
    """Пути плана 1 на временном проекте; шаг S1 без пометки риска (класс `normal`)."""
    return _stand(project, hermes_stub, monkeypatch, risk="normal")


@pytest.fixture()
def stand_high(project: Path, hermes_stub: Path, monkeypatch):
    """Тот же стенд, но шаг S1 помечен «Риск: high»."""
    return _stand(project, hermes_stub, monkeypatch, risk="high")


def _stand(project: Path, hermes_stub: Path, monkeypatch, *, risk: str):
    """Проект с планом нужного риска; сборщик шагов смотрит в PIPELINE_REPO."""
    for name in ("PIPELINE_CONFIG", "PIPELINE_PLANS", "PIPELINE_STATE_DIR", "PIPELINE_BOARD"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PIPELINE_REPO", str(project))
    write_risk_plan(project, risk)
    cfg = config_mod.load(project)
    return project, gate.resolve_paths(cfg, "1"), hermes_stub


# ─────────────────────────── лестница по риску ───────────────────────────

def test_defaults_ladder_in_config(stand):
    """База 2 и ступень `high` 6 живут в умолчаниях; мусор — ошибка конфигурации."""
    _root, _p, _db = stand
    assert config_mod.DEFAULTS["max_fix_rounds"] == 2
    assert config_mod.DEFAULTS["max_fix_rounds_high"] == 6
    cfg = config_mod.load(_root)
    assert (cfg.max_fix_rounds, cfg.max_fix_rounds_high) == (2, 6)

    path = Path(_root) / "pipeline.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    for bad in ("три", 0, True):
        raw["max_fix_rounds_high"] = bad
        path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        with pytest.raises(config_mod.ConfigError):
            config_mod.load(_root)


def test_limit_by_risk():
    """`high` берёт ступень, `low`/`normal` и отсутствие пометки — базу."""
    ctx = {"max_fix_rounds": 2, "max_fix_rounds_high": 6}
    assert gate.step_fix_limit(ctx, {"risk": "high"}) == 6
    assert gate.step_fix_limit(ctx, {"risk": "normal"}) == 2
    assert gate.step_fix_limit(ctx, {"risk": "low"}) == 2
    assert gate.step_fix_limit(ctx, {}) == 2
    assert gate.step_fix_limit(ctx, None) == 2
    # Ключ конфигурации сильнее умолчания: владелец задаёт свои числа.
    assert gate.step_fix_limit({"max_fix_rounds": 3, "max_fix_rounds_high": 9},
                               {"risk": "high"}) == 9
    # Копия движка без нового ключа в конфиге живёт на умолчании ступени.
    assert gate.step_fix_limit({"max_fix_rounds": 2}, {"risk": "high"}) == 6


def test_normal_step_third_request_changes_stops(stand):
    """Обычный шаг: третий отказ после двух кругов — стоп с цифрой базы."""
    root, p, db = stand
    fix_state(root, db, 2)

    res = gate.run_gate(p, execute=True, create_card=captured_create([]),
                        notify_fn=quiet)

    decision = res["decision"]
    assert decision["action"] == "stop", decision
    assert "исчерпан лимит автоматических кругов правок (2)" in decision["reason"]
    assert "high" not in decision["reason"]
    assert "повышение не предусмотрено" not in decision.get("details", "")


def test_high_step_stops_at_six_and_names_class(stand_high):
    """`high`-шаг: пять кругов дают шестой, шестой отказ — стоп с классом."""
    root, p, db = stand_high
    fix_state(root, db, 5)

    created: list[tuple[str, str]] = []
    res = gate.run_gate(p, execute=True, create_card=captured_create(created),
                        notify_fn=quiet)
    assert res["decision"]["action"] == "create_fix", res["decision"]
    assert res["decision"]["round"] == 6
    assert created and created[-1][0].endswith(":fix:6")

    insert_card(db, "t_fix_S1_6", "planloop:1:S1:fix:6", created_at=14)
    insert_card(db, "t_rev_S1_7", "planloop:1:S1:review:7", created_at=15)
    res = gate.run_gate(p, execute=True, create_card=captured_create([]), notify_fn=quiet)
    decision = res["decision"]
    assert decision["action"] == "stop", decision
    assert "(6)" in decision["reason"]
    assert "класс риска: high" in decision["reason"]
    assert "повышение не предусмотрено" in decision.get("details", "")


def test_stale_rebuild_does_not_eat_fix_budget(stand):
    """Пересборка протухшей постановки — своя серия: бюджет правок не тратит."""
    root, p, db = stand
    fix_state(root, db, 1)
    insert_card(db, "t_stale_1", "planloop:1:S1:promptfix-stale:1", created_at=10)
    insert_card(db, "t_stale_2", "planloop:1:S1:promptfix-stale:2", created_at=11)

    res = gate.run_gate(p, execute=True, create_card=captured_create([]), notify_fn=quiet)

    assert res["decision"]["action"] == "create_fix", res["decision"]
    assert res["decision"]["round"] == 2


def test_rereview_does_not_move_budgets(stand):
    """Повторное ревью — не правка: второй отказ после двух кругов всё равно стоп."""
    root, p, db = stand
    fix_state(root, db, 2)
    insert_card(db, "t_rerev", "planloop:1:S1:rereview:1", created_at=12)

    res = gate.run_gate(p, execute=True, create_card=captured_create([]), notify_fn=quiet)

    assert res["decision"]["action"] == "stop", res["decision"]
    assert "(2)" in res["decision"]["reason"]


# ─────────────────────────── тексты карточек и история ───────────────────────────

def test_fix_card_names_round_of_limit(stand):
    """Карточка правок говорит «круг N из M», а не «единственный круг»."""
    _root, p, _db = stand
    step = step_contract("high")
    first = gate.fix_card_body(p, step, 1, REPORT_REL, max_rounds=6)
    assert "(круг 1 из 6)" in first
    assert "Осталось кругов: 5 из 6" in first
    assert "это последний автоматический круг" not in first

    last = gate.fix_card_body(p, step, 6, REPORT_REL, max_rounds=6)
    assert "(круг 6 из 6)" in last
    assert "это последний автоматический круг" in last
    assert "Осталось кругов" not in last

    # Без предела — прежний вид строки (копии движка и внешние вызовы).
    legacy = gate.fix_card_body(p, step, 1, REPORT_REL)
    assert "(круг 1)" in legacy
    assert "из 6" not in legacy


def test_high_step_card_says_six_rounds(stand_high):
    """Сквозной путь: на `high`-шаге карточка правок называет предел 6."""
    root, p, db = stand_high
    fix_state(root, db, 1)
    created: list[tuple[str, str]] = []

    res = gate.run_gate(p, execute=True, create_card=captured_create(created),
                        notify_fn=quiet)

    assert res["decision"]["action"] == "create_fix", res["decision"]
    body = created[-1][1]
    assert "(круг 2 из 6)" in body


def test_history_line_marks_stale_rebuild(stand):
    """История помечает пересборку полем `stale`: метрика отличает её от правки."""
    _root, p, _db = stand
    gate.remember_action(p, "1", {"action": "create_prompt_fix", "step": "S1",
                                  "stale": True}, "пересборка постановки: t1")
    gate.remember_action(p, "1", {"action": "create_fix", "step": "S1",
                                  "round": 1}, "карточка правок: t2")

    path = Path(p.cfg.state_dir) / "history.jsonl"
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    assert [row["stale"] for row in rows] == [True, False]

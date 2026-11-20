# -*- coding: utf-8 -*-
"""Автовыбор рекомендации по таймауту для простаивающего плана (S5, п.5).

Зачем: открытый вопрос держит шаг, решение стража выходит `idle`, а `run_gate` для `idle`
не зовётся — `sweep_questions` жил только в нём, поэтому просроченный вопрос оставался
открытым, пока владелец не ответит сам (20.11.2026: планы стояли на шагах S9 и S10 при
истёкших дедлайнах, автовыбор не срабатывал ни на одном тике).

Проверяется путь целиком: постановка с вопросом → ожидание → истёкший дедлайн → тик
`gate.tick` (обход всех ведомых планов, тот путь, что зовёт cron) принимает рекомендацию
и записывает «авто», а карточка шага заказывается следующим тиком.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

import config as config_mod
import gate
from engine_kit import KIT

sys.path.insert(0, str(KIT / "tests"))

from helpers import stub_cards  # noqa: E402  (мост CLI и борды: DB может быть пустой)

QUESTION_LINE = ("Q1: поднять ли лимит карточек до трёх — A) да, три (цена: больше карточек "
                 "в работе) / B) нет, две (цена: простой) — рекомендация A")


def questions_file(project: Path) -> Path:
    return project / "docs" / "reviews" / "plan-1-questions.md"


def rows_of(project: Path) -> list[dict]:
    return gate.load_questions(questions_file(project))


def write_prompt(project: Path) -> Path:
    path = project / "docs" / "prompts" / "2026-11-20_1-S1_prompt.md"
    path.write_text(f"# Постановка S1\n\n{QUESTION_LINE}\n", encoding="utf-8")
    return path


@pytest.fixture()
def stand(project: Path, hermes_stub: Path, monkeypatch):
    """Проект с одним ведомым планом и подменёнными часами движка."""
    monkeypatch.setenv("PIPELINE_REPO", str(project))
    raw = json.loads((project / "pipeline.json").read_text(encoding="utf-8"))
    raw["max_actions"] = 1
    (project / "pipeline.json").write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    now = [1_000_000]
    monkeypatch.setattr(gate, "_now", lambda: float(now[0]))
    sent: list[str] = []
    monkeypatch.setattr(gate, "notify", lambda cfg, text: sent.append(text) or True)
    return config_mod.load(project), project, now, sent


def test_tick_auto_answers_expired_question_of_idle_plan(stand, hermes_stub: Path):
    """Истёк дедлайн → тик сам принимает рекомендацию, план перестаёт стоять."""
    cfg, project, now, sent = stand
    write_prompt(project)

    # Тик заказывает карточку постановки и регистрирует вопрос: шаг встаёт на ожидании.
    gate.tick(cfg)
    assert [row["status"] for row in rows_of(project)] == ["открыт"]
    assert gate.tick(cfg)["applied"] is False      # стоим: ждём ответа

    now[0] += 3601                                  # дедлайн автовыбора — час
    res = gate.tick(cfg)

    assert res["applied"] is True, res
    assert res["decision"]["action"] == "question_auto", res["decision"]
    row = rows_of(project)[0]
    assert row["status"] == "закрыт"
    assert row["answer"] == "A — авто: рекомендация по таймауту 60 мин"
    assert any("Q-1 принят автоматически: A (рекомендация)" in text for text in sent), sent

    # «Один тик — одно действие»: карточка шага заказывается следующим тиком, не этим.
    keys = {str(card["key"]) for card in stub_cards(hermes_stub)}
    assert "planloop:1:S1:step" not in keys, keys

    gate.tick(cfg)
    keys = {str(card["key"]) for card in stub_cards(hermes_stub)}
    assert "planloop:1:S1:step" in keys, keys

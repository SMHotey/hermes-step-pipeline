# -*- coding: utf-8 -*-
"""Тревога по вопросу без рекомендации, у которого истёк срок ответа (21.11.2026).

Зачем: автовыбор по таймауту срабатывает только по букве рекомендации. Решение, записанное
как «рекомендации нет», поэтому не закрывалось никогда и ждало владельца молча: тик выходил
`silent`, ни тревоги, ни строки в выводе (план 34 DoorSystem — 3 ч простоя, впереди ночь).
Тревога не решает за человека: вопрос остаётся открытым, но простой перестаёт быть тихим.

Проверяется путь целиком: постановка с вопросом без рекомендации → тик → ожидание →
истёкший дедлайн → `gate.tick` поднимает тревогу один раз и пишет строку в stderr;
повторный тик тревогу не дублирует.
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

from helpers import stub_cards  # noqa: E402,F401  (мост CLI и борды: DB может быть пустой)

QUESTION_LINE = ("Q1: какой набор шрифтов берём — A) Inter (цена: правка макетов) / "
                 "B) системный (цена: вид проще) — рекомендации нет")


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
    """Проект с одним ведомым планом, подменёнными часами и перехваченным `notify`."""
    monkeypatch.setenv("PIPELINE_REPO", str(project))
    raw = json.loads((project / "pipeline.json").read_text(encoding="utf-8"))
    raw["max_actions"] = 1
    (project / "pipeline.json").write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    now = [1_000_000]
    monkeypatch.setattr(gate, "_now", lambda: float(now[0]))
    sent: list[str] = []
    monkeypatch.setattr(gate, "notify", lambda cfg, text: sent.append(text) or True)
    return config_mod.load(project), project, now, sent


def test_expired_question_without_recommendation_raises_alarm(stand, hermes_stub: Path):
    """Дедлайн истёк, рекомендации нет → тревога владельцу, вопрос остаётся открытым."""
    cfg, project, now, sent = stand
    write_prompt(project)

    gate.tick(cfg)                                  # постановка + отметка asked_at у вопроса
    assert [row["status"] for row in rows_of(project)] == ["открыт"]

    now[0] += 3601                                  # дедлайн ответа — час
    gate.tick(cfg)

    assert [row["status"] for row in rows_of(project)] == ["открыт"], \
        "без рекомендации вопрос не закрывается автовыбором"
    alarms = [text for text in sent if "без рекомендации" in text]
    assert alarms, f"тревоги нет, уведомления: {sent}"
    assert "Q-1" in alarms[0]
    assert "решает владелец" in alarms[0] or "закрывает владелец" in alarms[0]

    # Повторный тик: тревога не дублируется (QUESTION_ALARM_GAP), вопрос всё ещё открыт.
    gate.tick(cfg)
    assert len([text for text in sent if "без рекомендации" in text]) == 1, sent


def test_question_without_recommendation_waits_quietly_before_deadline(stand,
                                                                      hermes_stub: Path):
    """До дедлайна тревоги нет: вопрос ждёт владельца, а не будит его сразу."""
    cfg, project, now, sent = stand
    write_prompt(project)
    gate.tick(cfg)
    now[0] += 60
    gate.tick(cfg)
    assert not [text for text in sent if "без рекомендации" in text], sent

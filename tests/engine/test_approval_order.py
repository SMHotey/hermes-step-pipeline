# -*- coding: utf-8 -*-
"""Зачем: §S6 плана 5 — утверждение владельцем последний шаг цикла, а не первый.

Проверяется привязка вердикта ревью плана к версии плана меткой `plan_sha256`
(`approval.review_status`): правка плана после APPROVE даёт состояние `stale` и требует
повторного ревью, отчёт без метки вердиктом не считается (`unbound`), нераспознанный
вердикт — `invalid`, план старого формата ревью не получает (`skipped`). Отдельно
проверяется, что отсутствие записи утверждения по-прежнему блокирует план раньше ревью
и что повторное ревью после правки плана заказывает ровно одну карточку своего круга.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import config as config_mod
import gate
import registry
from engine_kit import cards, complete, make_project, plan_sha256, write_approval

# План действующего формата: строка `Конституция:`, оформленная разведка (P1-P3) и
# журнал, где все строки открыты: закрытый шаг сделал бы план «идущим» и отключил гейт.
PLAN_READY = """# 1. Демо-план

Конституция: docs/CONSTITUTION.md

## Разведка

Артефакт: `docs/research/1-prior-art.md`

| Кандидат | Ссылка | Вердикт |
|---|---|---|
| demo/spec-kit | https://example.com/spec-kit | берём |

Решение: go — берём практики demo/spec-kit
Правки по разведке: 1

## Шаги

### S1. Старт

- Цель: заложить цикл.

### S2. Интеграция demo/spec-kit

- Цель: подключить готовое.

## Журнал выполнения

| Дата | Шаг | Коммиты | Прогоны и доказательства | Статус |
|---|---|---|---|---|
| — | S1 | — | — | открыт |
| — | S2 | — | — | открыт |
"""


def plan_path(root: Path) -> Path:
    return Path(root) / "docs" / "plans" / "1-demo.md"


def approve_report(plan: Path, verdict: str = "APPROVE") -> str:
    """Отчёт ревью плана с меткой версии и ссылкой на конституцию (§5a, шаг 5-S9).

    Без метки вердикт непривязан (`unbound`), без строки `Конституция:` —
    `unreferenced`; оба состояния значат «шаги не открываются», поэтому фикстура несёт
    обе строки, а отчёты специальных тестов собираются своим текстом.
    """
    return (f"# Ревью плана 1\n\n- находок нет\n\n"
            f"Конституция: п.1, п.7 — нарушений нет\n"
            f"plan_sha256: {plan_sha256(plan)}\n\n## ВЕРДИКТ: {verdict}\n")


def write_plan(root: Path, text: str = PLAN_READY) -> Path:
    """План проекта и его утверждение: sha файла меняется после записи."""
    path = plan_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    write_approval(root, path)
    return path


def write_plate(root: Path) -> None:
    """Разведка пройдена, постановка шага на месте: гейты разведки и постановки молчат."""
    research = Path(root) / "docs" / "research"
    research.mkdir(parents=True, exist_ok=True)
    (research / "1-prior-art.md").write_text("Итог: берём demo/spec-kit\n", encoding="utf-8")
    prompts = Path(root) / "docs" / "prompts"
    prompts.mkdir(parents=True, exist_ok=True)
    (prompts / "2026-11-07_1-S1_prompt.md").write_text(
        "# Постановка S1\n\n## Что сделать\n\nвзять цикл.\n", encoding="utf-8")


def write_report(root: Path, text: str) -> Path:
    """Отчёт ревью плана: `docs/reviews/plan-1.md`."""
    path = Path(root) / "docs" / "reviews" / "plan-1.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def review_keys(db: Path) -> list[str]:
    """Ключи карточек ревью плана (любого круга); борды ещё нет — пусто."""
    if not Path(db).is_file():
        return []
    try:
        return [c["key"] for c in cards(db)
                if (c["key"] or "").startswith("planloop:1:planreview")]
    except sqlite3.Error:                  # файл создан, таблиц ещё нет
        return []


def quiet(_text: str) -> None:
    """Заглушка уведомлений: тексты проверяются там, где они обязательны."""


@pytest.fixture()
def stand(project: Path, hermes_stub: Path, monkeypatch):
    """Стенд плана 1: план нового формата, утверждение, разведка и постановка S1."""
    write_plan(project)
    write_plate(project)
    monkeypatch.setenv("PIPELINE_REPO", str(project))
    return project, gate.resolve_paths(config_mod.load(project), "1"), hermes_stub


def test_plan_edited_after_approve_requires_new_review(stand):
    """Правка плана после APPROVE оставляет утверждение, но сбрасывает ревью (S6)."""
    root, p, _db = stand
    path = plan_path(root)
    write_report(root, approve_report(path))
    path.write_text(path.read_text(encoding="utf-8")
                    + "\nПримечание: шаг S1 уточнён.\n", encoding="utf-8")
    write_approval(root, path)

    status = registry.approval_status(p.cfg, registry.get(p.cfg, "1"))
    assert status["state"] == "approved"        # запись утверждения переписана владельцем
    assert status["ok"] is False
    assert status["block_kind"] == "review"
    assert status["review"]["state"] == "stale"
    label, fact = status["review"]["label_sha"], status["review"]["file_sha"]
    assert label and fact and label != fact
    assert label[:12] in status["reason"]
    assert fact[:12] in status["reason"]
    assert "повторное ревью" in status["reason"]


def test_approve_without_plan_sha_line_is_unbound(stand):
    """Отчёт без метки версии вердиктом не считается: причина называет метку и файл."""
    root, p, _db = stand
    write_report(root, "# Ревью плана 1\n\n- находок нет\n\n## ВЕРДИКТ: APPROVE\n")

    status = registry.approval_status(p.cfg, registry.get(p.cfg, "1"))
    assert status["state"] == "approved"
    assert status["ok"] is False
    assert status["block_kind"] == "review"
    assert status["review"]["state"] == "unbound"
    assert status["review"]["verdict"] == "APPROVE"     # вердикт распознан, но не привязан
    assert "plan_sha256" in status["reason"]
    assert "plan-1.md" in status["reason"]


def test_legacy_plan_needs_no_plan_review(tmp_path):
    """План старого формата (без строки `Конституция:`) сверяется только по sha."""
    root = make_project(tmp_path / "legacy")
    cfg = config_mod.load(root)
    status = registry.approval_status(cfg, registry.get(cfg, "1"))

    assert status["state"] == "approved"
    assert status["ok"] is True
    assert status["block_kind"] == ""
    assert status["review"]["state"] == "skipped"
    assert status["reason"] == ""
    assert status["record"]["sha256"] == status["sha256"]


def test_stale_review_orders_second_review_round(stand):
    """Устаревший вердикт заказывает второй круг ревью, а не `approval_blocked` (S6)."""
    root, p, db = stand
    first = gate.run_gate(p, notify_fn=quiet)
    assert first["decision"]["action"] == "create_plan_review", first["decision"]
    complete(db, ":planreview")
    write_report(root, approve_report(plan_path(root)))
    assert gate.decide(gate.build_ctx(p))["action"] == "create_step"

    path = plan_path(root)
    path.write_text(path.read_text(encoding="utf-8").rstrip("\n")
                    + "\n\nПримечание: уточнение шага S1.\n", encoding="utf-8")
    write_approval(root, path)

    decision = gate.decide(gate.build_ctx(p))
    assert decision["action"] == "create_plan_review", decision
    assert decision["round"] == 2
    assert "approval_blocked" not in decision
    assert "повторное ревью" in decision["reason"]

    gate.run_gate(p, notify_fn=quiet)
    keys = review_keys(db)
    assert keys.count("planloop:1:planreview:2") == 1
    assert len(keys) == 2                       # первый круг и повторный — по одной карточке

    again = gate.decide(gate.build_ctx(p))
    assert again["action"] == "idle", again
    assert len(review_keys(db)) == 2            # дубль не заказывается


def test_missing_approval_still_blocks_before_review(stand):
    """Нет записи утверждения — блок раньше ревью и никаких карточек плана (S6, S7 плана 4)."""
    root, p, db = stand
    write_report(root, approve_report(plan_path(root)))
    registry.approval_file_for(p.cfg, "1").unlink()

    status = registry.approval_status(p.cfg, registry.get(p.cfg, "1"))
    assert status["state"] == "missing"
    assert status["ok"] is False
    assert status["block_kind"] == "approval"

    decision = gate.decide(gate.build_ctx(p))
    assert decision["action"] == "idle"
    assert decision["approval_blocked"] is True
    assert decision["approval"] == "missing"
    assert "--accept 1" in decision["reason"]
    assert "block_kind" not in decision
    assert review_keys(db) == []

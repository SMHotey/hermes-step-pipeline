# -*- coding: utf-8 -*-
"""Разведка как гейт плана: карточка scout, ожидание и валидный исход «готового нет».

Восемь тестов шага S2 плана 5: одна карточка роли на план действующего формата, повторный
тик дубля не делает, закрытая карточка возвращает план к обычному порядку, «готового нет» —
валидный исход, провал без этой фразы — инцидентный стоп, наличие артефакта и отсутствие
строки `Конституция:` разведку отменяют, а профиль/скиллы/лимит читаются из конфига.
Тесты идут через `gate.run_gate` на временном проекте с поддельной бордой (`hermes_stub`),
карточки читаются из sqlite.
"""
from __future__ import annotations

from pathlib import Path

import pytest

import config as config_mod
import gate
from engine_kit import PLAN, cards, complete, make_project, write_approval

CONSTITUTION = "Конституция: docs/CONSTITUTION.md"


def write_plan(root: Path, constitution: bool = True) -> Path:
    """План проекта (со строкой `Конституция:` второй или без неё) и его утверждение.

    `PLAN` из `engine_kit` строки не содержит, поэтому без перезаписи первый тик ушёл бы
    в `create_step`, а не в `create_scout`. Утверждение переписывается: sha файла меняется.
    """
    lines = PLAN.splitlines()
    if constitution:
        lines.insert(1, "")
        lines.insert(2, CONSTITUTION)
    path = Path(root) / "docs" / "plans" / "1-demo.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    write_approval(root, path)
    return path


def write_prompt(root: Path, step: str) -> Path:
    """Постановка шага без отметки `recon_commit` — как в соседнем наборе тестов.

    Без отметки ревью постановки не заказывается (`_prompt_review_needed`), а
    `require_prompt` из шаблона проекта не уводит тик в заказ постановки.
    """
    folder = Path(root) / "docs" / "prompts"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"2026-11-06_1-{step}_prompt.md"
    path.write_text(f"# Постановка {step}\n\n## Что сделать\n\nминимальный текст.\n",
                    encoding="utf-8")
    return path


def keys_of(db: Path) -> set[str]:
    """Ключи идемпотентности всех карточек поддельной борды."""
    return {c["key"] for c in cards(db)}


def quiet(_text: str) -> None:
    """Заглушка уведомлений: тексты проверяются там, где они обязательны."""


def scout_cards(db: Path) -> list[dict]:
    """Карточки разведки в борде."""
    return [c for c in cards(db) if c["key"] == "planloop:1:scout"]


@pytest.fixture()
def stand(project: Path, hermes_stub: Path, monkeypatch):
    """Пути плана 1 на временном проекте; сборщик шагов смотрит в `PIPELINE_REPO`."""
    monkeypatch.setenv("PIPELINE_REPO", str(project))
    return project, gate.resolve_paths(config_mod.load(project), "1"), hermes_stub


def test_creates_one_scout_card_for_plan_without_research(stand):
    """План действующего формата без артефакта получает ровно одну карточку роли (C1, C5)."""
    root, p, db = stand
    write_plan(root, constitution=True)

    first = gate.run_gate(p, execute=True, notify_fn=quiet)
    assert first["decision"]["action"] == "create_scout", first["decision"]
    assert first["applied"] is True

    created = scout_cards(db)
    assert len(created) == 1
    card = created[0]
    assert card["assignee"] == "scout"
    assert card["max_runtime"] == "30m"
    assert card["status"] == "ready"
    assert card["title"].startswith("[разведка 1]")

    body = gate.scout_card_body(p, gate.collector(p)["steps"])
    for anchor in ("1-demo.md", "ВОПРОС РАЗВЕДКИ", "30 минут", "S1", "S2"):
        assert anchor in body, anchor


def test_second_tick_waits_and_does_not_duplicate(stand):
    """Пока карточка не закрыта, решение — ожидание с её id, второй карточки нет (C5, C7)."""
    root, p, db = stand
    write_plan(root, constitution=True)
    assert gate.run_gate(p, execute=True, notify_fn=quiet)["decision"]["action"] == "create_scout"

    second = gate.run_gate(p, execute=True, notify_fn=quiet)
    decision = second["decision"]
    assert decision["action"] == "idle", decision
    assert second["applied"] is False
    assert "ждёт разведку" in decision["reason"]
    assert scout_cards(db)[0]["id"] in decision["reason"]
    assert len(scout_cards(db)) == 1
    assert "planloop:1:S1:step" not in keys_of(db)


def test_done_scout_lets_plan_continue(stand):
    """Карточка разведки закрыта — план идёт обычным порядком к карточке шага (C8)."""
    root, p, db = stand
    write_plan(root, constitution=True)
    write_prompt(root, "S1")
    assert gate.run_gate(p, execute=True, notify_fn=quiet)["decision"]["action"] == "create_scout"

    complete(db, ":scout")
    second = gate.run_gate(p, execute=True, notify_fn=quiet)
    assert second["decision"]["action"] == "create_step", second["decision"]
    assert "planloop:1:S1:step" in keys_of(db)
    assert len(scout_cards(db)) == 1


def test_no_ready_solution_is_valid_not_stop(stand):
    """«Готового нет» в результате — валидный исход: стопа нет, владелец видит пояснение (C9)."""
    root, p, db = stand
    write_plan(root, constitution=True)
    write_prompt(root, "S1")
    assert gate.run_gate(p, execute=True, notify_fn=quiet)["decision"]["action"] == "create_scout"

    complete(db, ":scout", result="Итог: Готового нет. Ближайшие соседи не подходят.")
    messages: list[str] = []
    second = gate.run_gate(p, execute=True, notify_fn=messages.append)
    assert second["decision"]["action"] == "create_step", second["decision"]
    assert gate.load_stop(p) is None
    assert any("готового нет" in text.lower() for text in messages), messages


def test_failed_scout_stops_with_card_id(stand):
    """Провал карточки без маркера — инцидентный стоп с её id и статусом в причине (D7)."""
    root, p, db = stand
    write_plan(root, constitution=True)
    assert gate.run_gate(p, execute=True, notify_fn=quiet)["decision"]["action"] == "create_scout"

    complete(db, ":scout", status="crashed", result="упал: таймаут сети")
    second = gate.run_gate(p, execute=True, notify_fn=quiet)
    decision = second["decision"]
    assert decision["action"] == "stop", decision
    assert scout_cards(db)[0]["id"] in decision["reason"]
    assert "crashed" in decision["reason"]
    stop = gate.load_stop(p)
    assert stop and stop["kind"] == "incident"


def test_no_scout_when_research_artifact_exists(stand):
    """Артефакт разведки уже есть — карточка не заводится, шаг начинается сразу (D2)."""
    root, p, db = stand
    write_plan(root, constitution=True)
    research = root / "docs" / "research"
    research.mkdir(parents=True, exist_ok=True)
    (research / "1-prior-art.md").write_text("Итог: Готового нет\n", encoding="utf-8")
    write_prompt(root, "S1")

    first = gate.run_gate(p, execute=True, notify_fn=quiet)
    assert first["decision"]["action"] == "create_step", first["decision"]
    assert "planloop:1:scout" not in keys_of(db)


def test_no_scout_for_legacy_plan_without_constitution(stand):
    """План без строки `Конституция:` в шапке разведку не заказывает (D1)."""
    root, p, db = stand
    write_plan(root, constitution=False)
    write_prompt(root, "S1")

    first = gate.run_gate(p, execute=True, notify_fn=quiet)
    assert first["decision"]["action"] == "create_step", first["decision"]
    assert "planloop:1:scout" not in keys_of(db)


def test_scout_profile_skills_and_runtime_from_config(tmp_path, hermes_stub, monkeypatch):
    """Профиль, скиллы и лимит берутся из конфига, профиль доходит до причины (C2, D3, D4)."""
    root = make_project(tmp_path / "proj",
                        profiles={"driver": "demo", "prompter": "prompter",
                                  "reviewer": "reviewer", "scout": "scout-x"},
                        skills={"scout": ["pipeline-scout", "agent-prompt-authoring"]},
                        runtimes={"scout": "20m"})
    write_plan(root, constitution=True)          # перепись плана меняет sha — утверждение внутри
    monkeypatch.setenv("PIPELINE_REPO", str(root))
    p = gate.resolve_paths(config_mod.load(root), "1")

    calls: list[dict] = []

    def fake(_p, title, body, assignee, key, skills, parent=None, max_runtime=None):
        calls.append({"title": title, "assignee": assignee, "key": key,
                      "skills": tuple(skills), "max_runtime": max_runtime,
                      "parent": parent, "body": body})
        return "t_deadbeef"

    result = gate.run_gate(p, create_card=fake, notify_fn=quiet)
    assert result["decision"]["action"] == "create_scout", result["decision"]
    assert calls, "карточка не создавалась"
    call = calls[0]
    assert call["assignee"] == "scout-x"
    assert call["skills"] == ("pipeline-scout", "agent-prompt-authoring")
    assert call["max_runtime"] == "20m"
    assert call["key"] == "planloop:1:scout"
    assert call["parent"] is None                # карточка планового уровня
    assert "scout-x" in result["decision"]["reason"]

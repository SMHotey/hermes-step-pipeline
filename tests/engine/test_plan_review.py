# -*- coding: utf-8 -*-
"""Ревью плана как отдельный шаг: карточка, вердикт, один круг правок, эскалация.

Девять тестов шага S5 плана 5. Решение проверяется через `gate.run_gate` на временном
проекте с поддельной бордой (`hermes_stub`), карточки читаются из sqlite, отчёт ревью —
файлом `docs/reviews/plan-1.md`.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import config as config_mod
import gate
from engine_kit import cards, complete, make_project, write_approval

# План действующего формата: конституция, оформленная разведка (P1-P3 пройдены),
# шаг интеграции по вердикту «берём» и журнал, где ВСЕ строки открыты: закрытый шаг
# сделал бы план «идущим» (`registry.plan_started`) и отключил бы гейт (D2).
PLAN_READY = """# 1. Демо-план

Конституция: docs/CONSTITUTION.md

## Разведка

Артефакт: `docs/research/1-prior-art.md`

| Кандидат | Ссылка | Вердикт |
|---|---|---|
| demo/spec-kit | https://example.com/spec-kit | берём |
| demo/agent-factory | https://example.com/agent-factory | своё |

Решение: go — берём практики demo/spec-kit
Правки по разведке: 2

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

APPROVE_REPORT = "# Ревью плана 1\n\n- находок нет\n\n## ВЕРДИКТ: APPROVE\n"


def write_plan(root: Path, text: str = PLAN_READY) -> Path:
    """План проекта и его утверждение: sha файла меняется после записи."""
    path = Path(root) / "docs" / "plans" / "1-demo.md"
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
    """Отчёт ревью плана: `docs/reviews/plan-1.md` (D4)."""
    path = Path(root) / "docs" / "reviews" / "plan-1.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def keys_of(db: Path) -> set[str]:
    """Ключи идемпотентности всех карточек поддельной борды."""
    return {c["key"] for c in cards(db)}


def plan_review_cards(db: Path) -> list[dict]:
    """Карточки ревью плана (любого круга); борды ещё нет — пусто."""
    if not Path(db).is_file():
        return []
    try:
        return [c for c in cards(db) if (c["key"] or "").startswith("planloop:1:planreview")]
    except sqlite3.Error:                  # файл создан, таблиц ещё нет
        return []


def quiet(_text: str) -> None:
    """Заглушка уведомлений: тексты проверяются там, где они обязательны."""


@pytest.fixture()
def stand(project: Path, hermes_stub: Path, monkeypatch):
    """Стенд плана 1: план нового формата, утверждение, разведка, постановка."""
    write_plan(project)
    write_plate(project)
    monkeypatch.setenv("PIPELINE_REPO", str(project))
    return project, gate.resolve_paths(config_mod.load(project), "1"), hermes_stub


def test_card_requested_for_new_format_plan(stand):
    """Первый тик заказывает карточку ревью плана с профилем, лимитом и телом (C3, C4, C5)."""
    root, p, db = stand
    bodies: list[str] = []

    def spy(_p, title, body, assignee, key, skills, parent=None, max_runtime=None):
        bodies.append(body)
        return gate._create_card(_p, title, body, assignee, key, skills, parent, max_runtime)

    first = gate.run_gate(p, create_card=spy, notify_fn=quiet)
    assert first["decision"]["action"] == "create_plan_review", first["decision"]
    assert first["applied"] is True

    created = plan_review_cards(db)
    assert len(created) == 1
    card = created[0]
    assert card["key"] == "planloop:1:planreview"
    assert card["assignee"] == "reviewer"          # D3: умолчание — штатный ревьюер
    assert card["max_runtime"] == "45m"
    assert card["status"] == "ready"
    assert card["title"].startswith("[ревью плана 1]")

    body = bodies[0]
    for anchor in ("plan-1.md", "связность", "1.5", "APPROVE", "НЕ code review"):
        assert anchor in body, anchor


def test_old_format_plan_gets_no_plan_review(stand):
    """План без строки `Конституция:` ревью плана не получает (D1)."""
    root, p, db = stand
    write_plan(root, PLAN_READY.replace("Конституция: docs/CONSTITUTION.md\n\n", ""))

    first = gate.run_gate(p, notify_fn=quiet)
    assert first["decision"]["action"] == "create_step", first["decision"]
    assert "planloop:1:planreview" not in keys_of(db)


def test_running_plan_is_not_stopped(stand):
    """Идущий план не останавливаем: ревью плана ему не заказывается (D2)."""
    root, p, db = stand
    state = root / ".pipeline" / "state" / "plan_1_state.json"
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text('{"last_actions": []}', encoding="utf-8")

    first = gate.run_gate(p, notify_fn=quiet)
    assert first["decision"]["action"] == "create_step", first["decision"]
    assert plan_review_cards(db) == []


def test_second_tick_waits_and_does_not_duplicate(stand):
    """Пока карточка не закрыта — ожидание с её id, второй карточки и шага нет (C6)."""
    root, p, db = stand
    assert gate.run_gate(p, notify_fn=quiet)["decision"]["action"] == "create_plan_review"

    second = gate.run_gate(p, notify_fn=quiet)
    decision = second["decision"]
    assert decision["action"] == "idle", decision
    assert second["applied"] is False
    assert "ждёт ревью плана" in decision["reason"]
    assert plan_review_cards(db)[0]["id"] in decision["reason"]
    assert len(plan_review_cards(db)) == 1
    assert "planloop:1:S1:step" not in keys_of(db)


def test_unfinished_research_correction_blocks_review(stand):
    """План без отметки правок по разведке к ревью не идёт: находка P3 в причине (D5, C10)."""
    root, p, db = stand
    write_plan(root, PLAN_READY.replace("Правки по разведке: 2\n", ""))

    first = gate.run_gate(p, notify_fn=quiet)
    decision = first["decision"]
    assert decision["action"] == "idle", decision
    assert "правок по разведке" in decision["reason"]
    assert plan_review_cards(db) == []


def test_missing_verdict_stops_and_approve_unblocks_steps(stand):
    """Карточка закрыта без отчёта — стоп с путём; APPROVE возвращает план к шагу (C5, C9)."""
    root, p, db = stand
    assert gate.run_gate(p, notify_fn=quiet)["decision"]["action"] == "create_plan_review"

    complete(db, ":planreview")
    stopped = gate.run_gate(p, notify_fn=quiet)
    assert stopped["decision"]["action"] == "stop", stopped["decision"]
    assert "plan-1.md" in stopped["decision"]["reason"]
    assert "planloop:1:S1:step" not in keys_of(db)

    gate.clear_stop(p)
    write_report(root, APPROVE_REPORT)
    after = gate.run_gate(p, notify_fn=quiet)
    assert after["decision"]["action"] == "create_step", after["decision"]


def test_request_changes_creates_one_fix_round(stand):
    """REQUEST_CHANGES заказывает ровно одну карточку правок плана с находками (D7, C7)."""
    root, p, db = stand
    assert gate.run_gate(p, notify_fn=quiet)["decision"]["action"] == "create_plan_review"

    complete(db, ":planreview")
    write_report(root,
                 "# Ревью плана 1\n\n- шаг S2 не опирается на S1\n\n"
                 "## ВЕРДИКТ: REQUEST_CHANGES\n")

    bodies: list[str] = []

    def spy(_p, title, body, assignee, key, skills, parent=None, max_runtime=None):
        bodies.append(body)
        return gate._create_card(_p, title, body, assignee, key, skills, parent, max_runtime)

    second = gate.run_gate(p, create_card=spy, notify_fn=quiet)
    decision = second["decision"]
    assert decision["action"] == "create_plan_fix", decision
    fixes = [c for c in cards(db) if c["key"] == "planloop:1:planfix:1"]
    assert len(fixes) == 1
    assert fixes[0]["assignee"] == "demo"          # D3: `cfg.driver` в фикстуре
    assert "S2 не опирается на S1" in bodies[0]


def test_second_request_changes_escalates_to_owner(stand):
    """Второй REQUEST_CHANGES останавливает цикл стоп-флагом владельца с находками (D6, D7)."""
    root, p, db = stand
    assert gate.run_gate(p, notify_fn=quiet)["decision"]["action"] == "create_plan_review"
    complete(db, ":planreview")

    request = ("# Ревью плана 1\n\n- шаг S2 не опирается на S1\n\n"
               "## ВЕРДИКТ: REQUEST_CHANGES\n")
    write_report(root, request)

    assert gate.run_gate(p, notify_fn=quiet)["decision"]["action"] == "create_plan_fix"
    complete(db, ":planfix:1")

    repeat = gate.run_gate(p, notify_fn=quiet)
    assert repeat["decision"]["action"] == "create_plan_review", repeat["decision"]
    assert repeat["decision"]["round"] == 2
    complete(db, ":planreview:2")
    write_report(root, request)

    escalated = gate.run_gate(p, notify_fn=quiet)
    decision = escalated["decision"]
    assert decision["action"] == "stop", decision
    assert decision["kind"] == "owner"
    assert "второй REQUEST_CHANGES" in decision["reason"]
    assert "plan-1.md" in decision["reason"]
    assert "S2 не опирается на S1" in decision["details"]
    assert gate.load_stop(p)["kind"] == "owner"


def test_config_overrides_profile_skills_runtime(tmp_path, hermes_stub, monkeypatch):
    """Профиль, скиллы и лимит карточки ревью читаются из конфига (C4)."""
    root = make_project(tmp_path / "proj",
                        profiles={"driver": "demo", "prompter": "prompter",
                                  "reviewer": "reviewer", "plan_reviewer": "plan-checker"},
                        skills={"plan_review": ["pipeline-plan-reviewer",
                                                "agent-prompt-authoring"]},
                        runtimes={"plan_review": "25m"})
    write_plan(root)
    write_plate(root)
    monkeypatch.setenv("PIPELINE_REPO", str(root))
    p = gate.resolve_paths(config_mod.load(root), "1")

    calls: list[dict] = []

    def fake(_p, title, body, assignee, key, skills, parent=None, max_runtime=None):
        calls.append({"title": title, "assignee": assignee, "key": key,
                      "skills": tuple(skills), "max_runtime": max_runtime,
                      "parent": parent, "body": body})
        return "t_deadbeef"

    result = gate.run_gate(p, create_card=fake, notify_fn=quiet)
    assert result["decision"]["action"] == "create_plan_review", result["decision"]
    assert calls, "карточка не создавалась"
    call = calls[0]
    assert call["assignee"] == "plan-checker"
    assert call["skills"] == ("pipeline-plan-reviewer", "agent-prompt-authoring")
    assert call["max_runtime"] == "25m"
    assert call["key"] == "planloop:1:planreview"
    assert call["parent"] is None                  # карточка планового уровня
    assert "plan-checker" in result["decision"]["reason"]

# -*- coding: utf-8 -*-
"""Гейт корректировки плана по разведке (plan_cycle)."""
from __future__ import annotations

import plan_cycle as pc

GOOD = """# 1. Демо-план

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
| 07.11.2026 | S1 | abc1234 | гейты зелёные | ЗАКРЫТ |
"""


def test_research_section_by_anchor() -> None:
    body = pc.research_section(GOOD)
    assert body is not None
    assert "Решение: go" in body
    assert "Правки по разведке: 2" in body  # отметка внутри тела раздела

    # без якоря раздела нет
    assert pc.research_section(GOOD.replace("## Разведка", "## Рассуждения")) is None
    # заголовок шага разделом не считается (D2)
    assert pc.research_section("### S3. Разведка рынка\n\nтело шага\n") is None
    # заголовок внутри раздела шагов — тоже шаг, а не якорь плана (D2)
    assert pc.research_section("## Шаги\n\n### Разведка в проекте\n\n- тело.\n") is None


def test_can_review_blocks_without_research() -> None:
    assert pc.can_review(GOOD) == (True, "")

    broken = GOOD.replace("## Разведка", "## Рассуждения")
    assert ("FAIL", "P1", "нет раздела «Разведка» — план без ссылки на разведку "
                          "не идёт на ревью") in pc.check_plan(broken)
    ok, reason = pc.can_review(broken)
    assert ok is False
    assert "Разведка" in reason  # причина — сообщение P1


def test_take_verdict_without_integration_is_blocked() -> None:
    assert pc.take_candidates(GOOD) == ("demo/spec-kit",)

    broken = GOOD.replace("### S2. Интеграция demo/spec-kit", "### S2. Итоги")
    level, code, msg = next(row for row in pc.check_plan(broken) if row[0] == "FAIL")
    assert (level, code) == ("FAIL", "P2")
    assert "берём" in msg
    assert "demo/spec-kit" in msg
    assert pc.can_review(broken)[0] is False

    # проза «берём» вне таблицы вердиктом не считается (D3): строка таблицы цела,
    # шаг интеграции есть — P2 молчит
    prose = GOOD.replace("Решение: go — берём практики demo/spec-kit",
                         "Решение: go — свои решения")
    assert not any(level == "FAIL" and code == "P2"
                   for level, code, _ in pc.check_plan(prose))

    # вердикт снят, шаг интеграции остался — P2 не срабатывает (4.2, п.4в)
    assert not any(level == "FAIL" and code == "P2"
                   for level, code, _ in pc.check_plan(GOOD.replace("| берём |", "| своё |")))

    # вердикта «берём» не осталось — P2 не срабатывает и без шага интеграции
    take_gone = (GOOD.replace("| берём |", "| своё |")
                     .replace("### S2. Интеграция demo/spec-kit", "### S2. Итоги"))
    assert not any(level == "FAIL" and code == "P2"
                   for level, code, _ in pc.check_plan(take_gone))


def test_integration_step_satisfies_take() -> None:
    step = pc.integration_step(GOOD)
    assert step is not None
    assert "Интеграция" in step

    assert {code: level for level, code, _ in pc.check_plan(GOOD)} == {
        "P1": "PASS", "P2": "PASS", "P3": "PASS", "P4": "PASS"}

    # P2 срабатывает только от вердикта: убраны и вердикт, и шаг интеграции
    text = (GOOD.replace("| берём |", "| своё |")
                .replace("### S2. Интеграция demo/spec-kit", "### S2. Итоги"))
    assert not any(level == "FAIL" and code == "P2"
                   for level, code, _ in pc.check_plan(text))


def test_edits_marker_and_diff() -> None:
    assert pc.edits_count(GOOD) == 2
    assert pc.edits_count(GOOD.replace("Правки по разведке: 2", "без отметки")) is None

    diff = pc.edit_diff("раз\nдва\n", "раз\nтри\n")
    assert diff.startswith("---")
    assert "план (до правки)" in diff
    assert "-два" in diff
    assert "+три" in diff
    assert pc.edit_diff("тот же\n", "тот же\n") == ""


def test_missing_marker_blocks_review() -> None:
    broken = GOOD.replace("Правки по разведке: 2\n", "")
    assert any(level == "FAIL" and code == "P3" and "правок по разведке" in msg
               for level, code, msg in pc.check_plan(broken))

    ok, reason = pc.can_review(broken)
    assert ok is False
    assert "правок по разведке" in reason  # P1 и P2 PASS — первая FAIL это P3
    assert pc.edits_count(broken) is None


# ── P4: раздел ссылается на артефакт ЭТОГО плана (0.20.0) ─────────────────────

def test_artifact_link_to_foreign_plan_is_fail() -> None:
    """Ссылка на артефакт чужого плана — FAIL P4 с обоими путями в тексте."""
    foreign = GOOD.replace("docs/research/1-prior-art.md", "docs/research/2-prior-art.md")
    level, code, message = pc.check_artifact(foreign, "1")
    assert (level, code) == ("FAIL", "P4")
    assert "docs/research/2-prior-art.md" in message
    assert "docs/research/1-prior-art.md" in message
    assert pc.can_review(foreign, "1")[0] is False


def test_artifact_link_to_own_plan_is_pass() -> None:
    """Свой артефакт в ссылке — P4 PASS; номер берётся и из заголовка плана."""
    assert pc.check_artifact(GOOD, "1")[0] == "PASS"
    assert pc.check_plan(GOOD, "1")[-1][:2] == ("PASS", "P4")
    assert pc.can_review(GOOD, "1") == (True, "")
    # номер плана передан явно: чужая нумерация в заголовке не спасает
    assert pc.check_artifact(GOOD, "2")[0] == "FAIL"


def test_missing_artifact_link_is_fail() -> None:
    """Ссылки на артефакт нет вовсе — тоже FAIL P4: сверять раздел не с чем."""
    no_link = GOOD.replace("Артефакт: `docs/research/1-prior-art.md`\n", "")
    level, code, message = pc.check_artifact(no_link, "1")
    assert (level, code) == ("FAIL", "P4")
    assert "а артефакт плана — docs/research/1-prior-art.md" in message


def test_no_research_section_keeps_old_behaviour() -> None:
    """Без раздела «Разведка» поведение прежнее: P1 FAIL, P4 молчит."""
    broken = GOOD.replace("## Разведка", "## Рассуждения")
    levels = {code: level for level, code, _ in pc.check_plan(broken, "1")}
    assert levels["P1"] == "FAIL"
    assert levels["P4"] == "PASS"
    assert "нет раздела «Разведка»" in pc.can_review(broken, "1")[1]

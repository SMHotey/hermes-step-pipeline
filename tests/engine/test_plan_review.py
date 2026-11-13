# -*- coding: utf-8 -*-
"""Ревью плана как отдельный шаг: карточка, вердикт, круги правок по лимиту, эскалация.

Девять тестов шага S5 плана 5 плюс четыре теста круга правок: гейт не отключает себя сам
(состояние тика не признак «план идёт»), сбой создания карточки не открывает шаги, а
устаревший отчёт ревью не считается вердиктом. Решение проверяется через `gate.run_gate`
на временном проекте с поддельной бордой (`hermes_stub`), карточки читаются из sqlite,
отчёт ревью — файлом `docs/reviews/plan-1.md`.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path

import pytest

import config as config_mod
import gate
import registry
from engine_kit import cards, complete, make_project, plan_sha256, write_approval

# План действующего формата: конституция, оформленная разведка (P1-P3 пройдены),
# шаг интеграции по вердикту «берём» и журнал, где ВСЕ строки открыты: закрытый шаг
# сделал бы план «идущим» (`ctx["closed"]`), и тогда D2 отключил бы гейт.
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


def approve_report(plan: Path) -> str:
    """Отчёт APPROVE с меткой версии и ссылкой на конституцию (§5a, шаг 5-S9).

    Без метки вердикт непривязан (`unbound`), без строки `Конституция:` — `unreferenced`:
    оба состояния означают «шаги не открываются», поэтому фикстура несёт обе строки.
    """
    return (f"# Ревью плана 1\n\n- находок нет\n\n"
            f"Конституция: п.1, п.7 — нарушений нет\n"
            f"plan_sha256: {plan_sha256(plan)}\n\n## ВЕРДИКТ: APPROVE\n")


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
    for anchor in ("plan-1.md", "связность", "1.5", "APPROVE", "НЕ code review",
                   "Конституция"):
        assert anchor in body, anchor


def test_old_format_plan_gets_no_plan_review(stand):
    """План без строки `Конституция:` ревью плана не получает (D1)."""
    root, p, db = stand
    write_plan(root, PLAN_READY.replace("Конституция: docs/CONSTITUTION.md\n\n", ""))

    first = gate.run_gate(p, notify_fn=quiet)
    assert first["decision"]["action"] == "create_step", first["decision"]
    assert "planloop:1:planreview" not in keys_of(db)


def test_running_plan_is_not_stopped(stand):
    """Идущий план ревью плана не получает: признак «идёт» — закрытый шаг журнала (D2)."""
    root, p, db = stand
    running = PLAN_READY.replace("| — | S1 | — | — | открыт |",
                                 "| — | S1 | — | — | закрыт |")
    assert running != PLAN_READY
    write_plan(root, running)
    prompts = root / "docs" / "prompts"
    (prompts / "2026-11-07_1-S2_prompt.md").write_text(
        "# Постановка S2\n\n## Что сделать\n\nподключить готовое.\n", encoding="utf-8")

    first = gate.run_gate(p, notify_fn=quiet)
    assert first["decision"]["action"] == "create_step", first["decision"]
    assert plan_review_cards(db) == []


def test_state_file_of_own_tick_does_not_cancel_review(stand):
    """Путь «разведка → ревью»: состояние, записанное тиком, ревью не отменяет (находка 1)."""
    root, p, db = stand
    (root / "docs" / "research" / "1-prior-art.md").unlink()      # разведка не проведена

    scout = gate.run_gate(p, notify_fn=quiet)
    assert scout["decision"]["action"] == "create_scout", scout["decision"]
    # тик сам записал файл состояния — из него признак «план идёт» брать нельзя
    assert (root / ".pipeline" / "state" / "plan_1_state.json").is_file()

    write_plate(root)                                             # разведка проведена
    second = gate.run_gate(p, notify_fn=quiet)
    assert second["decision"]["action"] == "create_plan_review", second["decision"]
    assert "planloop:1:S1:step" not in keys_of(db)


def test_research_wait_does_not_turn_into_step(stand):
    """Путь «правка по разведке → ревью»: второй тик не начинает шаг (0.20.0).

    План без отметки правок и с живым артефактом разведки получает карточку правки
    плана по разведке, а не простой: пока карточка не закрыта, шаг не заказывается.
    """
    root, p, db = stand
    write_plan(root, PLAN_READY.replace("Правки по разведке: 2\n", ""))

    first = gate.run_gate(p, notify_fn=quiet)
    assert first["decision"]["action"] == "create_scout_fix", first["decision"]
    assert "правок по разведке" in first["decision"]["reason"]
    assert (root / ".pipeline" / "state" / "plan_1_state.json").is_file()

    second = gate.run_gate(p, notify_fn=quiet)
    decision = second["decision"]
    assert decision["action"] == "idle", decision
    assert "ждёт правку плана по разведке" in decision["reason"]
    assert plan_review_cards(db) == []          # ни ревью, ни шага не заказано
    assert "planloop:1:S1:step" not in keys_of(db)


def test_failed_card_creation_does_not_mark_plan_started(stand):
    """Сбой создания карточки состояние не пишет: заказ повторяется, шаг не стартует."""
    root, p, db = stand

    failed = gate.run_gate(p, create_card=lambda *_a, **_k: None, notify_fn=quiet)
    assert failed["decision"]["action"] == "create_plan_review", failed["decision"]
    assert failed["applied"] is False

    second = gate.run_gate(p, notify_fn=quiet)
    assert second["decision"]["action"] == "create_plan_review", second["decision"]
    assert "planloop:1:S1:step" not in keys_of(db)


def test_stale_report_is_not_a_verdict(stand):
    """Отчёт старше карточки ревью — не вердикт: стоп «отчёт не обновлён» (находка 2)."""
    root, p, db = stand
    report = write_report(root, "# Ревью плана 1\n\n- шаг S2 не опирается на S1\n\n"
                                "## ВЕРДИКТ: REQUEST_CHANGES\n")
    old = int(time.time()) - 3600          # отчёт написан до карточки (прошлый круг)
    os.utime(report, (old, old))

    assert gate.run_gate(p, notify_fn=quiet)["decision"]["action"] == "create_plan_review"
    complete(db, ":planreview")

    stopped = gate.run_gate(p, notify_fn=quiet)
    decision = stopped["decision"]
    assert decision["action"] == "stop", decision
    assert "не обновлён" in decision["reason"]
    assert "plan-1.md" in decision["reason"]
    assert "planloop:1:planfix:1" not in keys_of(db)


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
    assert decision["action"] == "create_scout_fix", decision
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
    write_report(root, approve_report(Path(root) / "docs" / "plans" / "1-demo.md"))
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


def test_second_request_changes_asks_new_fix_round(stand):
    """Второй REQUEST_CHANGES даёт ВТОРУЮ карточку правок, а не ревью того же текста.

    Регресс 0.25.2: правка заказывалась только когда карточек правок нет вовсе, поэтому
    после первого круга каждый следующий отказ давал ещё одно ревью неизменённого плана —
    до owner-stop. Живой случай — план 6: ревью (14:00) → правки (14:09) → ревью (14:30) →
    ревью того же sha.
    """
    root, p, db = stand
    assert gate.run_gate(p, notify_fn=quiet)["decision"]["action"] == "create_plan_review"
    complete(db, ":planreview")

    request = ("# Ревью плана 1\n\n- шаг S2 не опирается на S1\n\n"
               "## ВЕРДИКТ: REQUEST_CHANGES\n")
    write_report(root, request)

    assert gate.run_gate(p, notify_fn=quiet)["decision"]["action"] == "create_plan_fix"
    complete(db, ":planfix:1")

    repeated = gate.run_gate(p, notify_fn=quiet)
    assert repeated["decision"]["action"] == "create_plan_review", repeated["decision"]
    assert repeated["decision"]["round"] == 2
    complete(db, ":planreview:2")
    write_report(root, request)

    decision = gate.run_gate(p, notify_fn=quiet)["decision"]
    assert decision["action"] == "create_plan_fix", decision
    assert decision["round"] == 2
    assert "кругом 2 из 2" in decision["reason"]
    fixes = [c["key"] for c in cards(db) if c["key"].startswith("planloop:1:planfix")]
    assert fixes == ["planloop:1:planfix:1", "planloop:1:planfix:2"], fixes


def test_fix_rounds_exhausted_escalates_to_owner(stand):
    """Когда круги правок исчерпаны, отказ останавливает цикл стоп-флагом владельца (D6, D7)."""
    root, p, db = stand
    request = ("# Ревью плана 1\n\n- шаг S2 не опирается на S1\n\n"
               "## ВЕРДИКТ: REQUEST_CHANGES\n")
    assert gate.run_gate(p, notify_fn=quiet)["decision"]["action"] == "create_plan_review"
    complete(db, ":planreview")
    write_report(root, request)
    assert gate.run_gate(p, notify_fn=quiet)["decision"]["action"] == "create_plan_fix"
    complete(db, ":planfix:1")
    assert gate.run_gate(p, notify_fn=quiet)["decision"]["action"] == "create_plan_review"
    complete(db, ":planreview:2")
    write_report(root, request)
    assert gate.run_gate(p, notify_fn=quiet)["decision"]["action"] == "create_plan_fix"
    complete(db, ":planfix:2")
    assert gate.run_gate(p, notify_fn=quiet)["decision"]["action"] == "create_plan_review"
    complete(db, ":planreview:3")
    write_report(root, request)

    escalated = gate.run_gate(p, notify_fn=quiet)
    decision = escalated["decision"]
    assert decision["action"] == "stop", decision
    assert decision["kind"] == "owner"
    assert "круги правок плана 1 исчерпаны (2 из 2)" in decision["reason"]
    assert "plan-1.md" in decision["reason"]
    assert "S2 не опирается на S1" in decision["details"]
    assert gate.load_stop(p)["kind"] == "owner"


def test_fix_after_reapproval_gets_review_not_owner_stop(stand):
    """Правка плана после переутверждения не съедает остаток бюджета кругов.

    Регресс: наследование записи утверждения переписывало её целиком и теряло
    `review_reset_at`; бюджет снова считался за всю жизнь плана, и круг правок упирался
    в owner-stop до повторного ревью. Живой случай — `gapaudit`, план 1, 21:21:05:
    «второй REQUEST_CHANGES», хотя поправленный план ещё ни разу не ревьюили.
    """
    root, p, db = stand
    plan = Path(root) / "docs" / "plans" / "1-demo.md"
    request = ("# Ревью плана 1\n\n- шаг S2 не опирается на S1\n\n"
               "## ВЕРДИКТ: REQUEST_CHANGES\n")

    # Круг до переутверждения: карточка ревью и правка. Их отметки времени сдвигаем в
    # прошлое — иначе секундная точность карточек делает сцену недетерминированной.
    assert gate.run_gate(p, notify_fn=quiet)["decision"]["action"] == "create_plan_review"
    complete(db, ":planreview")
    write_report(root, request)
    assert gate.run_gate(p, notify_fn=quiet)["decision"]["action"] == "create_plan_fix"
    complete(db, ":planfix:1")

    reset = int(time.time()) - 2
    con = sqlite3.connect(db)
    try:
        con.execute("UPDATE tasks SET created_at = ? WHERE idempotency_key LIKE 'planloop:1:%'",
                    (reset - 10,))
        con.commit()
    finally:
        con.close()

    # Владелец переутверждает план: бюджет кругов начинается заново.
    write_approval(root, plan, review_reset_at=reset)
    assert gate.run_gate(p, notify_fn=quiet)["decision"]["action"] == "create_plan_review"
    complete(db, ":planreview:2")
    later = time.time() + 5
    os.utime(write_report(root, request), (later, later))
    again = gate.run_gate(p, notify_fn=quiet)
    assert again["decision"]["action"] == "create_plan_fix", again["decision"]
    # Ключ нумеруется по всем правкам за жизнь плана (planfix:2), а круг в заголовке — по
    # эпохе переутверждения: иначе карточка читается как «круг 2 из 2» при свежей эпохе.
    fresh = [c for c in cards(db) if c["key"].startswith("planloop:1:planfix")][-1]
    assert fresh["key"] == "planloop:1:planfix:2", fresh["key"]
    assert "(круг 1)" in fresh["title"], fresh["title"]

    # Правка правит текст плана: утверждение переписывается наследованием (inherited),
    # но метка переутверждения обязана пережить эту запись.
    complete(db, ":planfix:2")
    plan.write_text(plan.read_text(encoding="utf-8") + "\nПравка по ревью.\n",
                    encoding="utf-8")
    os.utime(write_report(root, request), (later, later))

    decision = gate.run_gate(p, notify_fn=quiet)["decision"]
    assert decision["action"] == "create_plan_review", decision
    assert "поправлен по ревью" in decision["reason"]
    assert gate.load_stop(p) is None


def test_approval_reset_at_falls_back_to_approved_at(stand):
    """Запись старого формата (без `review_reset_at`) даёт момент утверждения.

    Панель, запущенная до обновления плагина, писала утверждение без метки переутверждения;
    без запасного пути страж считал бюджет за всю жизнь плана и сразу ставил owner-stop
    «второй REQUEST_CHANGES» (живой случай — gapaudit, план 2, 21:59:19 → стоп в 22:00:51).
    """
    _root, _p, _db = stand
    stamp = "2026-11-10T21:59:19"
    expected = int(time.mktime(time.strptime(stamp, "%Y-%m-%dT%H:%M:%S")))
    assert gate.approval_reset_at({"review_reset_at": 1794327676}) == 1794327676
    assert gate.approval_reset_at({"approved_at": stamp}) == expected
    assert gate.approval_reset_at({"approved_at": stamp + ".123456"}) == expected
    assert gate.approval_reset_at({}) == 0
    assert gate.approval_reset_at({"approved_at": "не дата"}) == 0


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


# ── Ссылка вердикта на конституцию (§5a, шаг 5-S9). ──────────────────────────

def test_review_without_constitution_reference_is_unreferenced(stand):
    """Отчёт с меткой версии, но без строки конституции вердиктом не считается (D3)."""
    root, p, _db = stand
    plan = Path(root) / "docs" / "plans" / "1-demo.md"
    write_report(root, (f"# Ревью плана 1\n\n- находок нет\n\n"
                        f"plan_sha256: {plan_sha256(plan)}\n\n## ВЕРДИКТ: APPROVE\n"))

    status = registry.approval_status(p.cfg, registry.get(p.cfg, "1"))
    assert status["review"]["state"] == "unreferenced"
    assert status["ok"] is False
    assert status["block_kind"] == "review"
    assert "нет ссылки на пункты конституции" in status["reason"]


def test_review_with_constitution_reference_is_fresh(stand):
    """Тот же отчёт со строкой `Конституция: п.1 — нарушений нет` — свежий вердикт."""
    root, p, _db = stand
    plan = Path(root) / "docs" / "plans" / "1-demo.md"
    write_report(root, (f"# Ревью плана 1\n\n- нарушений нет\n\n"
                        f"Конституция: п.1 — план следует принципам\n\n"
                        f"plan_sha256: {plan_sha256(plan)}\n\n## ВЕРДИКТ: APPROVE\n"))

    status = registry.approval_status(p.cfg, registry.get(p.cfg, "1"))
    assert status["review"]["state"] == "fresh"
    assert status["ok"] is True


# ─────────────────── переутверждение после owner-stop (0.21.0) ───────────────────

REQUEST = ("# Ревью плана 1\n\n- шаг S2 не опирается на S1\n\n"
           "## ВЕРДИКТ: REQUEST_CHANGES\n")


def _to_owner_stop(root: Path, p, db) -> dict:
    """Довести план до owner-stop: круги правок исчерпаны, каждое ревью — REQUEST_CHANGES.

    Возвращает решение тика, поставившего стоп: тесты S1 плана 6 сверяют по нему бюджет.
    """
    assert gate.run_gate(p, notify_fn=quiet)["decision"]["action"] == "create_plan_review"
    complete(db, ":planreview")
    write_report(root, REQUEST)
    assert gate.run_gate(p, notify_fn=quiet)["decision"]["action"] == "create_plan_fix"
    complete(db, ":planfix:1")
    assert gate.run_gate(p, notify_fn=quiet)["decision"]["action"] == "create_plan_review"
    complete(db, ":planreview:2")
    write_report(root, REQUEST)
    assert gate.run_gate(p, notify_fn=quiet)["decision"]["action"] == "create_plan_fix"
    complete(db, ":planfix:2")
    assert gate.run_gate(p, notify_fn=quiet)["decision"]["action"] == "create_plan_review"
    complete(db, ":planreview:3")
    write_report(root, REQUEST)
    stopped = gate.run_gate(p, notify_fn=quiet)["decision"]
    assert stopped["action"] == "stop" and stopped["kind"] == "owner", stopped
    return stopped


def _reapprove_edited(root: Path, db) -> None:
    """Владелец правит план и переутверждает его: `plan --accept` пишет `review_reset_at`.

    Карточки первой попытки сдвигаются на две минуты назад: в жизни переутверждение идёт
    ПОСЛЕ них, а в тесте всё происходит в одну секунду, и без сдвига метка неотличима от
    них. Новый круг ревью заказывается уже после метки — как и в живом проекте.
    """
    con = sqlite3.connect(db)
    try:
        con.execute("UPDATE tasks SET created_at = created_at - 120")
        con.commit()
    finally:
        con.close()
    plan = write_plan(root, PLAN_READY.replace(
        "### S2. Интеграция demo/spec-kit\n\n- Цель: подключить готовое.",
        "### S2. Интеграция demo/spec-kit\n\n- Цель: подключить готовое.\n"
        "- Правка по ревью: шаг опирается на S1 (провенанс из T5-1)."))
    write_approval(root, plan, review_reset_at=int(time.time()))


def test_reapproval_after_owner_stop_opens_new_review_round(stand):
    """Правка плана владельцем не возвращает тот же стоп: заказывается свежий круг ревью."""
    root, p, db = stand
    _to_owner_stop(root, p, db)
    _reapprove_edited(root, db)
    gate.clear_stop(p)
    # 7-S4: переутверждение больше не прячет рост версии — правка сверх `max_plan_growth`
    # даёт owner-стоп с замером ДО ветки утверждения (D7). Утверждение теста при этом не
    # сужено: стоп снимает сам владелец, свежий круг ревью открывается следующим тиком.
    growth = gate.run_gate(p, notify_fn=quiet)["decision"]
    assert growth["action"] == "stop" and growth["kind"] == "owner", growth
    assert growth["growth"]["over"] is True, growth
    gate.clear_stop(p)

    decision = gate.run_gate(p, notify_fn=quiet)["decision"]
    assert decision["action"] == "create_plan_review", decision
    assert decision["round"] == 4
    assert "переутверждён" in decision["reason"]


def test_reapproval_resets_review_budget(stand):
    """После переутверждения бюджет кругов начинается заново: отказ даёт правку, не стоп."""
    root, p, db = stand
    _to_owner_stop(root, p, db)
    _reapprove_edited(root, db)
    gate.clear_stop(p)
    # 7-S4: перед новым кругом — owner-стоп роста версии (см. тест выше).
    assert gate.run_gate(p, notify_fn=quiet)["decision"]["action"] == "stop"
    gate.clear_stop(p)
    assert gate.run_gate(p, notify_fn=quiet)["decision"]["action"] == "create_plan_review"

    complete(db, ":planreview:4")
    write_report(root, REQUEST)
    decision = gate.run_gate(p, notify_fn=quiet)["decision"]
    assert decision["action"] == "create_plan_fix", decision
    fixes = [c for c in cards(db) if c["key"].startswith("planloop:1:planfix")]
    # Ключ правки обязан быть новым: закрытая карточка прошлой версии не переиспользуется
    # (ядро дедуплицирует по ключу и вернуло бы её — правок бы не появилось вовсе).
    assert [c["key"] for c in fixes] == ["planloop:1:planfix:1", "planloop:1:planfix:2",
                                        "planloop:1:planfix:3"], fixes
    assert fixes[-1]["status"] == "ready"


# ─────────────────── зависание: тревога владельцу (0.21.1) ───────────────────

def test_next_fix_round_skips_taken_keys():
    """Номер круга правок — по занятым ключам, а не по кругу ревью (иначе переиспользование)."""
    assert gate.next_fix_round([], 1) == 1
    assert gate.next_fix_round([{"key": "planloop:1:planfix:1"}], 1) == 2
    assert gate.next_fix_round([{"key": "planloop:1:planfix:1"},
                                {"key": "planloop:1:planfix:2"}], 1) == 3


def test_attention_text_has_loud_header_and_plan():
    """Тревога начинается с громкой строки и говорит, что случилось и что делать."""
    text = gate.attention_text("1", "план стоит", evidence=["карточка done"],
                               todo=["обновить движок"], commands=["python .pipeline/gate.py --status"])
    assert text.splitlines()[0] == "ВНИМАНИЕ!!! Требуется починить конвейер."
    assert "План 1: план стоит" in text
    assert "Что случилось:" in text and "Что нужно сделать:" in text and "Команды:" in text


def test_attention_is_throttled_per_reason(stand):
    """Одна тревога на причину: cron не должен засыпать чат одинаковыми сообщениями."""
    _root, p, _db = stand
    p.cfg.notify["enabled"] = True
    said: list[str] = []
    assert gate.attention(p.cfg, "раз", "stall:1", said.append) is True
    assert gate.attention(p.cfg, "два", "stall:1", said.append) is False
    assert gate.attention(p.cfg, "три", "stall:2", said.append) is True
    assert said == ["раз", "три"]


def test_note_action_streak_counts_identical_actions(stand):
    """Счёт повторов растёт на одинаковых действиях и сбрасывается на другом результате."""
    _root, p, _db = stand
    entry = {"action": "create_plan_fix", "detail": "карточка правок плана: t_1"}
    assert gate.note_action_streak(p, entry) == 1
    assert gate.note_action_streak(p, entry) == 2
    assert gate.note_action_streak(p, {"action": "create_plan_fix",
                                       "detail": "карточка правок плана: t_2"}) == 1


def test_closed_card_on_create_raises_alarm(stand):
    """Заказ вернул закрытую карточку — стоп-инцидент и тревога, а не «выполнено»."""
    root, p, db = stand
    assert gate.run_gate(p, notify_fn=quiet)["decision"]["action"] == "create_plan_review"
    complete(db, ":planreview")
    write_report(root, REQUEST)
    said: list[str] = []

    def closed_create(_p, title, body, assignee, key, skills, parent=None, max_runtime=None):
        """Имитация настоящего `hermes kanban create`: на занятый ключ отдаёт done-карточку."""
        con = sqlite3.connect(db)
        try:
            con.execute(
                "INSERT INTO tasks (id, title, status, assignee, idempotency_key, completed_at,"
                " result, last_failure_error, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                ("t_closed01", title, "done", assignee, key, int(time.time()), "", "",
                 int(time.time())))
            con.commit()
        finally:
            con.close()
        return "t_closed01"

    result = gate.run_gate(p, create_card=closed_create, notify_fn=said.append)
    assert result["applied"] is True
    assert "закрытую карточку" in result["detail"], result
    stop = json.loads((Path(root) / ".pipeline" / "state" / "plan_1_stop.json")
                      .read_text(encoding="utf-8"))
    assert stop["kind"] == "incident"
    assert any(gate.ATTENTION_HEADER in text for text in said), said


# ───────── дополнительный круг правок по маркеру владельца (S1 плана 6) ─────────

def test_marker_adds_fix_round_after_owner_stop(stand):
    """Жест владельца маркером даёт третий круг правок, и только после него — новый стоп.

    Без маркера бюджет — два круга из конфига: третий REQUEST_CHANGES ставит owner-стоп
    «2 из 2». Маркер `plan_1_rounds.json` добавляет круг: после `--resume` тик заказывает
    карточку `planloop:1:planfix:3` с причиной и телом «круг 3 из 3», а исчерпание
    расширенного бюджета снова упирается в owner-стоп — уже «3 из 3».
    """
    root, p, db = stand
    first_stop = _to_owner_stop(root, p, db)
    assert "исчерпаны (2 из 2)" in first_stop["reason"], first_stop

    marker = Path(p.cfg.state_dir) / "plan_1_rounds.json"
    marker.write_text(json.dumps({"extra_fix_rounds": 1, "at": "2026-11-12T18:00:00",
                                  "reason": "владелец разрешил ещё один круг правок"}),
                      encoding="utf-8")
    assert gate.load_extra_fix_rounds(p) == 1
    gate.clear_stop(p)                     # это делает `gate.py --plan 1 --resume`

    bodies: list[str] = []

    def spy(_p, title, body, assignee, key, skills, parent=None, max_runtime=None):
        bodies.append(body)
        return gate._create_card(_p, title, body, assignee, key, skills, parent, max_runtime)

    third = gate.run_gate(p, create_card=spy, notify_fn=quiet)["decision"]
    assert third["action"] == "create_plan_fix", third
    assert third["round"] == 3, third
    assert "кругом 3 из 3" in third["reason"], third
    assert "круг 3 из 3" in bodies[0], bodies[0][:200]
    assert "planloop:1:planfix:3" in keys_of(db)

    complete(db, ":planfix:3")
    assert gate.run_gate(p, notify_fn=quiet)["decision"]["action"] == "create_plan_review"
    complete(db, ":planreview:4")
    write_report(root, REQUEST)

    second_stop = gate.run_gate(p, notify_fn=quiet)["decision"]
    assert second_stop["action"] == "stop", second_stop
    assert second_stop["kind"] == "owner", second_stop
    assert "исчерпаны (3 из 3)" in second_stop["reason"], second_stop
    assert gate.load_stop(p)["kind"] == "owner"


def test_marker_read_from_configured_state_dir(stand, monkeypatch):
    """Маркер читается из `paths.state`, а не из литерала `.pipeline/state`."""
    root, _p, db = stand
    for name in ("PIPELINE_STATE_DIR", "PIPELINE_CONFIG"):
        monkeypatch.delenv(name, raising=False)
    raw = json.loads((Path(root) / "pipeline.json").read_text(encoding="utf-8"))
    raw["paths"] = {"plans": "docs/plans", "reviews": "docs/reviews",
                    "prompts": "docs/prompts", "state": "run/state"}
    (Path(root) / "pipeline.json").write_text(json.dumps(raw, ensure_ascii=False),
                                             encoding="utf-8")
    p = gate.resolve_paths(config_mod.load(root), "1")
    assert Path(p.cfg.state_dir) == Path(root) / "run" / "state"

    # Запись утверждения переезжает вместе с каталогом состояния: иначе план «не утверждён»
    # и ревью не начнётся.
    record = write_approval(root, Path(root) / "docs" / "plans" / "1-demo.md")
    moved = Path(p.cfg.state_dir) / record.name
    moved.parent.mkdir(parents=True, exist_ok=True)
    record.replace(moved)
    assert moved.is_file() and not record.exists()

    # Приманка в прежнем каталоге: литерал `.pipeline/state` дал бы бюджет 7, а не 3.
    (Path(root) / ".pipeline" / "state" / "plan_1_rounds.json").write_text(
        json.dumps({"extra_fix_rounds": 5}), encoding="utf-8")
    (Path(p.cfg.state_dir) / "plan_1_rounds.json").write_text(
        json.dumps({"extra_fix_rounds": 1, "at": "2026-11-12T18:00:00",
                    "reason": "владелец разрешил ещё один круг правок"}), encoding="utf-8")
    assert gate.load_extra_fix_rounds(p) == 1

    assert gate.run_gate(p, notify_fn=quiet)["decision"]["action"] == "create_plan_review"
    complete(db, ":planreview")
    write_report(root, REQUEST)
    for round_no in (1, 2):
        fix = gate.run_gate(p, notify_fn=quiet)["decision"]
        assert fix["action"] == "create_plan_fix", fix
        assert fix["round"] == round_no, fix
        complete(db, f":planfix:{round_no}")
        assert gate.run_gate(p, notify_fn=quiet)["decision"]["action"] == "create_plan_review"
        complete(db, f":planreview:{round_no + 1}")
        write_report(root, REQUEST)

    third = gate.run_gate(p, notify_fn=quiet)["decision"]
    assert third["action"] == "create_plan_fix", third
    assert third["round"] == 3, third
    assert "кругом 3 из 3" in third["reason"], third


def test_broken_marker_gives_zero_extra_rounds(stand):
    """Битый маркер — ноль доп. кругов, а не падение тика: страж упирается в «2 из 2».

    Маркер правят руки и панель, поэтому неверное значение — не повод для `GateError`:
    файла нет, он не разбирается, это не словарь, ключа нет, `true`, минус, не число — всё
    ноль. Строка из цифр считается числом: такой маркер мог прийти из JSON, собранного
    руками. Отдельно — значения, которые проходят `str.isdigit()`, но не `int()` («²» и
    цифровая строка длиннее лимита CPython): разбор обязан отдать 0, а не бросить
    `ValueError`, иначе тик падает до `try/except` вокруг коллектора.
    """
    root, p, db = stand
    marker = gate.rounds_path(p)
    assert marker == Path(p.cfg.state_dir) / "plan_1_rounds.json"
    broken = [json.dumps("abc"), json.dumps(-1), json.dumps(True), json.dumps([]),
              json.dumps({"at": "2026-11-12T18:00:00"}), '{"extra_fix_rounds": 1',
              json.dumps({"extra_fix_rounds": "\u00b2"}),
              json.dumps({"extra_fix_rounds": "1" * 4400}),
              json.dumps({"extra_fix_rounds": 1.5})]
    for payload in broken:
        marker.write_text(payload, encoding="utf-8")
        assert gate.load_extra_fix_rounds(p) == 0, payload
    marker.write_text(json.dumps({"extra_fix_rounds": "1"}), encoding="utf-8")
    assert gate.load_extra_fix_rounds(p) == 1, "строка из цифр — число кругов"
    marker.write_text(json.dumps({"extra_fix_rounds": 1.0}), encoding="utf-8")
    assert gate.load_extra_fix_rounds(p) == 1, "1.0 — целое число кругов, а не ноль"
    marker.write_text(json.dumps({"extra_fix_rounds": 1}), encoding="utf-8")
    assert gate.load_extra_fix_rounds(p) == 1     # защита от «функция всегда возвращает 0»

    # Тик идёт с нулём доп. кругов и на значении из класса «проходит isdigit(), но не int()»:
    # `build_ctx` читает маркер ДО `try/except` вокруг коллектора, поэтому разбор обязан быть
    # небросающим — иначе вместо owner-стопа будет трейсбек.
    marker.write_text(json.dumps({"extra_fix_rounds": "\u00b2"}), encoding="utf-8")
    stopped = _to_owner_stop(root, p, db)
    assert "исчерпаны (2 из 2)" in stopped["reason"], stopped
    assert gate.load_stop(p)["kind"] == "owner"

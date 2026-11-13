# -*- coding: utf-8 -*-
"""Вердикт ревью плана по реестру находок (S2 плана 7): реестр сильнее отчёта, заморозка."""
from __future__ import annotations

import hashlib
import json
import sys
import time
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent
ENGINE = TESTS.parent / "step_pipeline" / "engine"
if str(ENGINE) not in sys.path:
    sys.path.insert(0, str(ENGINE))

import config as config_mod  # noqa: E402 - движок импортирует соседей плоско
import gate  # noqa: E402

# План действующего формата: конституция, оформленная разведка (P1-P3 пройдены),
# шаг интеграции по вердикту «берём» и журнал, где ВСЕ строки открыты: закрытый шаг
# сделал бы план «идущим», и тогда ревью плана ему не заказывается.
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


def write_plan(root: Path) -> Path:
    """План нового формата плюс запись утверждения (форма — `tests/helpers.py:94-110`)."""
    path = root / "docs" / "plans" / "1-demo.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(PLAN_READY, encoding="utf-8")
    record = root / ".pipeline" / "state" / "plan_1_approval.json"
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_text(json.dumps({
        "plan": "1",
        "file": path.relative_to(root).as_posix(),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "approved_at": "2026-01-01T00:00:00",
        "profile": "test",
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def write_plate(root: Path) -> None:
    """Разведка пройдена, постановка S1 на месте: гейты плана молчат."""
    research = root / "docs" / "research"
    research.mkdir(parents=True, exist_ok=True)
    (research / "1-prior-art.md").write_text("Итог: берём demo/spec-kit\n", encoding="utf-8")
    prompts = root / "docs" / "prompts"
    prompts.mkdir(parents=True, exist_ok=True)
    (prompts / "2026-11-07_1-S1_prompt.md").write_text(
        "# Постановка S1\n\n## Что сделать\n\nвзять цикл.\n", encoding="utf-8")


@pytest.fixture()
def stand(project, hermes_stub, monkeypatch):
    """Стенд плана 1: план нового формата, утверждение, разведка, постановка.

    `hermes_stub` нужен ради `PIPELINE_KANBAN_DB`: без него страж читал бы настоящую борду.
    """
    monkeypatch.setenv("PIPELINE_REPO", str(project))
    write_plan(project)
    write_plate(project)
    return project, gate.resolve_paths(config_mod.load(project), "1")


def review_card(status: str = "done") -> dict:
    """Карточка ревью в ctx: форма `gate.read_cards`, created_at в прошлом — детерминизм."""
    now = int(time.time())
    return {"id": "t_card01", "title": "[ревью плана 1]", "status": status,
            "assignee": "reviewer", "key": "planloop:1:planreview", "completed_at": now,
            "result": "", "error": "", "created_at": now - 60, "started_at": now - 60}


def ctx_with_card(p, status: str = "done") -> dict:
    ctx = gate.build_ctx(p)
    ctx["plan_review_cards"] = [review_card(status)]
    return ctx


@pytest.fixture()
def bodies() -> list[str]:
    """Копилка тел карточек: `spy` кладёт сюда каждое тело, тест читает последнее."""
    return []


@pytest.fixture()
def spy(bodies):
    """`create_card` как в образце (`tests/engine/test_plan_review.py:310-312`)."""
    def create_card(_p, title, body, assignee, key, skills, parent=None, max_runtime=None):
        bodies.append(body)
        return gate._create_card(_p, title, body, assignee, key, skills, parent, max_runtime)
    return create_card


def write_report(root: Path, verdict: str, sha: str | None = None) -> Path:
    """Отчёт ревью плана: метка версии, конституция и ровно одна строка вердикта."""
    plan = root / "docs" / "plans" / "1-demo.md"
    path = root / "docs" / "reviews" / "plan-1.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    digest = sha or hashlib.sha256(plan.read_bytes()).hexdigest()
    path.write_text(
        f"# Ревью плана 1\n\n- находок нет\n\nКонституция: п.1 — нарушений нет\n"
        f"plan_sha256: {digest}\n\n"
        f"## ВЕРДИКТ: {verdict}\n", encoding="utf-8")
    return path


def write_registry(p, *statuses: str) -> Path:
    """Реестр плана 1: по находке на статус, ID растут от F1."""
    rows = [{"id": f"F{number}", "step": "S1", "kind": "замысел",
             "text": f"находка {number}", "action": f"правка {number}", "status": status}
            for number, status in enumerate(statuses, start=1)]
    path = gate.plan_findings_path(p)
    gate.write_findings(path, rows)
    return path


def quiet(_text: str) -> None:
    """Заглушка уведомлений: тексты проверяются там, где они обязательны."""


def test_no_registry_keeps_report_verdict(stand):
    """Реестра нет — работают прежние правила отчёта; мусорный реестр — инцидентный стоп."""
    root, p = stand

    write_report(root, "REQUEST_CHANGES")
    decision = gate._plan_review_decision(ctx_with_card(p))
    assert decision["action"] == "create_plan_fix", decision
    assert "кругом 1 из 2" in decision["reason"]

    write_report(root, "APPROVE")
    assert gate._plan_review_decision(ctx_with_card(p)) is None      # дверь открыта: шаг идёт

    registry = gate.plan_findings_path(p)
    registry.write_text("# Реестр находок плана 1\n\nтаблицы нет\n", encoding="utf-8")
    stopped = gate._plan_review_decision(ctx_with_card(p))
    assert stopped["action"] == "stop", stopped
    assert "реестр находок плана 1 не читается" in stopped["reason"]
    assert str(registry) in stopped["reason"]


def test_zero_open_findings_approve(stand):
    """Ноль открытых ID при REQUEST_CHANGES в отчёте — APPROVE по реестру и расхождение."""
    root, p = stand
    write_report(root, "REQUEST_CHANGES")
    write_registry(p, "закрыто")
    ctx = ctx_with_card(p)

    view = gate.effective_plan_verdict(ctx)
    assert view["verdict"] == "APPROVE", view
    assert view["open"] == [] and view["total"] == 1
    assert view["report"] == "REQUEST_CHANGES" and view["registry"] is True
    assert view["discrepancy"] is True
    assert gate._plan_review_decision(ctx) is None

    note = gate.verdict_discrepancy_text(ctx)
    assert "APPROVE" in note and "REQUEST_CHANGES" in note and "plan-1.md" in note


def test_one_open_finding_requests_changes(stand):
    """Один открытый ID при APPROVE в отчёте — REQUEST_CHANGES по реестру, причина называет ID."""
    root, p = stand
    write_report(root, "APPROVE")
    write_registry(p, "открыто")
    ctx = ctx_with_card(p)

    view = gate.effective_plan_verdict(ctx)
    assert view["verdict"] == "REQUEST_CHANGES", view
    assert view["open"] == ["F1"] and view["discrepancy"] is True

    decision = gate._plan_review_decision(ctx)
    assert decision["action"] == "create_plan_fix", decision
    assert "F1" in decision["reason"] and "реестр" in decision["reason"]


def test_discrepancy_is_commented_and_fix_card_names_findings(stand, spy, bodies, monkeypatch):
    """Расхождение уходит комментарием в карточку ревью, карточка правок называет открытый ID."""
    root, p = stand
    write_report(root, "APPROVE")
    write_registry(p, "открыто")
    ctx = ctx_with_card(p)

    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(gate, "_comment",
                        lambda _p, cid, text: sent.append((cid, text)) or True)

    got = gate.run_gate(p, ctx=ctx, create_card=spy, notify_fn=quiet)
    assert got["decision"]["action"] == "create_plan_fix", got["decision"]
    assert got["applied"] is True
    assert len(sent) == 1 and sent[0][0] == "t_card01", sent
    assert "F1" in sent[0][1] and "REQUEST_CHANGES" in sent[0][1], sent
    assert "F1" in bodies[-1]


def test_second_round_card_freezes_new_findings(stand, spy, bodies):
    """Круг 2 требует новых находок только по изменённому тексту; ворота собирают это тело."""
    root, p = stand
    rel = p.cfg.rel(Path(p.plan_file))
    report_rel = gate.plan_review_rel(p)
    phrase = "только по строкам, изменённым последней правкой"

    first = gate.plan_review_card_body(p, rel, report_rel, "reviewer")
    second = gate.plan_review_card_body(p, rel, report_rel, "reviewer", round_no=2)
    assert phrase not in first and "ЗАМОРОЗКА НАХОДОК" not in first
    assert phrase in second and "ЗАМОРОЗКА НАХОДОК (круг 2)" in second

    # Ветка заказа: вердикт привязан к чужой версии плана — повторное ревью, круг 2.
    write_report(root, "APPROVE", sha="0" * 64)
    got = gate.run_gate(p, ctx=ctx_with_card(p), create_card=spy, notify_fn=quiet)
    assert got["decision"]["action"] == "create_plan_review", got["decision"]
    assert got["decision"]["round"] == 2
    assert phrase in bodies[-1]


def test_registry_only_non_blocking_approves(stand):
    """«Не блокирует» вердикт не держит: открытых нет — APPROVE при любом отчёте."""
    root, p = stand
    write_report(root, "REQUEST_CHANGES")
    write_registry(p, "не блокирует")
    ctx = ctx_with_card(p)

    view = gate.effective_plan_verdict(ctx)
    assert view["open"] == [] and view["total"] == 1
    assert view["verdict"] == "APPROVE", view
    assert gate._plan_review_decision(ctx) is None

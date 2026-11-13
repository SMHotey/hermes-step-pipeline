# -*- coding: utf-8 -*-
"""Правка плана не расширяет план (S4 плана 7): допуск из двух границ и owner-стоп.

Семь проверок контракта шага плюс одна на реестр находок: допуск — МЕНЬШЕЕ из процента
версии плана и символов на закрытую находку; превышение даёт owner-стоп с замером, а план
не усекается; замер уходит комментарием в карточку правок и в состояние плана, и по той же
версии плана стоп не повторяется; битый конфиг — `ConfigError`, умолчание одинаково в трёх
местах.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent
ENGINE = TESTS.parent / "step_pipeline" / "engine"
if str(ENGINE) not in sys.path:
    sys.path.insert(0, str(ENGINE))

import approval  # noqa: E402 - движок импортирует соседей плоско
import config as config_mod  # noqa: E402
import gate  # noqa: E402

DEFAULT_LIMIT = {"percent": 10, "chars_per_finding": 400}


@pytest.fixture()
def stand(project, hermes_stub, monkeypatch):
    """Стенд плана 1: проект после установки движка (план, утверждение, журнал).

    `hermes_stub` нужен ради `PIPELINE_KANBAN_DB`: без него страж читал бы настоящую борду.
    """
    monkeypatch.setenv("PIPELINE_REPO", str(project))
    return project, gate.resolve_paths(config_mod.load(project), "1")


@pytest.fixture()
def spy_comment(monkeypatch) -> list:
    """Копилка комментариев: `_comment` подменяется, тест читает адреса и тексты."""
    capsule: list = []

    def fake(_p, card_id, text):
        capsule.append((card_id, text))
        return True

    monkeypatch.setattr(gate, "_comment", fake)
    return capsule


def plan_path(root: Path) -> Path:
    return root / "docs" / "plans" / "1-demo.md"


def version_chars(root: Path, p) -> int:
    """Объём версии плана — тем же счётом, что у стража: журнал нормализован."""
    return len(approval.plan_version_text(plan_path(root), approval.journal_marker_of(p.cfg)))


def set_plan_size(root: Path, p, size: int) -> None:
    """План ровно `size` символов версии: тело плана плюс подгонка заполнителем.

    Допуск считается долями объёма, и проверять его на «случайной» длине стенда значило бы
    проверять арифметику на числах, которых не видно в тесте.
    """
    path = plan_path(root)
    head = path.read_text(encoding="utf-8").split("ЗАПОЛНИТЕЛЬ-S4")[0].rstrip("\n") + "\n"
    path.write_text(head, encoding="utf-8")
    filler = "ЗАПОЛНИТЕЛЬ-S4\n"
    pad = size - version_chars(root, p) - len(filler)
    assert pad >= 0, f"план стенда больше {size} символов: {version_chars(root, p)}"
    path.write_text(head + filler + "x" * pad, encoding="utf-8")
    assert version_chars(root, p) == size


def append_to_plan(root: Path, text: str) -> None:
    """Дописать в план ровно `text`: так правка и выглядит в жизни — файл стал длиннее."""
    path = plan_path(root)
    path.write_text(path.read_text(encoding="utf-8") + text, encoding="utf-8")


def set_baseline(p, *findings: str) -> dict:
    """Базовая версия плана в состоянии — так её пишет заказ карточки правок."""
    baseline = gate.plan_fix_snapshot(p, 1, gate.build_ctx(p), list(findings))
    gate.save_state(p, {"plan_fix_baseline": baseline})
    return baseline


def fix_card(status: str = "done") -> dict:
    """Карточка правки плана в ctx: форма `gate.read_cards`, created_at в прошлом."""
    now = int(time.time())
    return {"id": "t_fix01", "title": "[правки плана 1]", "status": status,
            "assignee": "plan_fixer", "key": "planloop:1:planfix:1", "completed_at": now,
            "result": "Итог: находки закрыты", "error": "",
            "created_at": now - 60, "started_at": now - 60}


def ctx_with_fix(p, status: str = "done") -> dict:
    ctx = gate.build_ctx(p)
    ctx["plan_fix_cards"] = [fix_card(status)]
    return ctx


def quiet(_text: str) -> None:
    """Заглушка уведомлений: тексты проверяются там, где они обязательны."""

    return None


def no_cards(*_args, **_kwargs):
    """Заглушка заказа карточек: owner-стоп не имеет права ничего заказывать."""
    return None


def write_cfg(root: Path, value) -> None:
    """Задать `max_plan_growth` в `pipeline.json` проекта как есть, без проверок."""
    cfg_path = root / "pipeline.json"
    data = json.loads(cfg_path.read_text(encoding="utf-8"))
    data["max_plan_growth"] = value
    cfg_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def findings_rows(*statuses: str) -> list[dict]:
    """Строки реестра находок: ID по порядку, статусы — как переданы."""
    return [{"id": f"F{index}", "step": "S1", "kind": "стиль", "text": f"находка {index}",
             "action": "поправить", "status": status}
            for index, status in enumerate(statuses, start=1)]


def test_growth_within_limit_no_stop(stand):
    """Рост 600 при допуске 1000 (3 находки: 3 × 400 больше 10 %) — стопа нет."""
    root, p = stand
    set_plan_size(root, p, 10000)
    set_baseline(p, "F1 (S1): находка", "F2 (S2): находка", "F3 (S3): находка")
    append_to_plan(root, "x" * 600)
    assert gate._plan_growth_decision(ctx_with_fix(p)) is None

    # Карточка правок называет исполнителю те же числа, что действуют в движке.
    body = gate.plan_fix_card_body(p, "docs/plans/1-demo.md", "docs/reviews/plan-1.md",
                                   ["F1 (S1): находка"])
    assert "10% объёма" in body and "400 символов" in body, body
    assert "отчёт и журнал" in body, body


def test_growth_over_percent_stops(stand, spy_comment):
    """Рост 1500 при допуске min(10 % = 1000, 400 × 10) — owner-стоп, а не правка втихую."""
    root, p = stand
    set_plan_size(root, p, 10000)
    set_baseline(p, *[f"F{index} (S1): находка" for index in range(1, 11)])
    append_to_plan(root, "x" * 1500)

    decision = gate._plan_growth_decision(ctx_with_fix(p))
    assert decision is not None, "рост сверх допуска прошёл молча"
    assert decision["action"] == "stop" and decision["kind"] == "owner", decision
    assert decision["plan_review"] is True, decision
    assert "step" not in decision, "стоп планового уровня не должен занимать слот шага"
    reason = decision["reason"]
    for part in ("на 15%", "1500", "находок закрыто 10", "1000"):
        assert part in reason, (part, reason)
    # Граница находок (400 × 10 = 4000) не может перекрыть процент: берётся меньшее.
    assert decision["growth"]["allowed"] == 1000, decision["growth"]
    assert "СТОП-СИГНАЛ" in decision["comment"] and "11500" in decision["comment"], decision

    result = gate.run_gate(p, ctx=ctx_with_fix(p), create_card=no_cards, notify_fn=quiet)
    assert spy_comment and spy_comment[0][0] == "t_fix01", spy_comment
    assert result["decision"]["action"] == "stop"


def test_growth_over_per_finding_cap_stops(stand):
    """Рост 900 при допуске min(10 % = 2000, 400 × 1) — граница находок, процент «4.5»."""
    root, p = stand
    set_plan_size(root, p, 20000)
    set_baseline(p, "F1 (S1): находка")
    append_to_plan(root, "x" * 900)

    decision = gate._plan_growth_decision(ctx_with_fix(p))
    assert decision is not None, "граница на находку не сработала"
    assert decision["growth"]["allowed"] == 400, decision["growth"]
    assert "на 4.5%" in decision["reason"], decision["reason"]
    assert "закрыто 1" in decision["reason"], decision["reason"]


def test_no_previous_version_no_limit(stand):
    """Сравнивать не с чем (нет базы, план не менялся, правка не закрыта) — стопа нет."""
    root, p = stand
    assert gate._plan_growth_decision(ctx_with_fix(p)) is None      # базы нет вовсе

    set_plan_size(root, p, 10000)
    set_baseline(p, "F1 (S1): находка")
    assert gate._plan_growth_decision(ctx_with_fix(p)) is None      # план не менялся

    append_to_plan(root, "x" * 5000)
    # Карточка правок ещё не закрыта: мерить нечего, объём сверяется после неё.
    assert gate._plan_growth_decision(ctx_with_fix(p, status="running")) is None


def test_broken_config_raises(project):
    """Мусор в `max_plan_growth` — `ConfigError` с полем; частичный ключ — умолчание."""
    write_cfg(project, {"percent": "десять"})
    with pytest.raises(config_mod.ConfigError, match="max_plan_growth.percent"):
        config_mod.load(project)

    write_cfg(project, 10)
    with pytest.raises(config_mod.ConfigError):
        config_mod.load(project)

    write_cfg(project, {"percent": 10})
    assert config_mod.load(project).max_plan_growth["chars_per_finding"] == 400


def test_default_limit_matches_three_places(project):
    """Умолчание 10 % / 400 одинаково в DEFAULTS, в собранном конфиге и в движке."""
    cfg = config_mod.load(project)
    assert cfg.max_plan_growth == DEFAULT_LIMIT, cfg.max_plan_growth
    assert cfg.max_plan_growth == config_mod.DEFAULTS["max_plan_growth"]
    assert gate.PLAN_GROWTH_PERCENT == 10
    assert gate.PLAN_GROWTH_CHARS_PER_FINDING == 400


def test_stop_logged_once_and_plan_not_truncated(stand, spy_comment):
    """Стоп и замер — на диск; повторный тик тот же стоп не выносит; план не усечён."""
    root, p = stand
    set_plan_size(root, p, 10000)
    set_baseline(p, "F1 (S1): находка")
    append_to_plan(root, "x" * 1500)
    before_bytes = plan_path(root).read_bytes()

    result = gate.run_gate(p, ctx=ctx_with_fix(p), create_card=no_cards, notify_fn=quiet)
    assert result["decision"]["action"] == "stop", result["decision"]
    stop = gate.load_stop(p)
    assert stop["kind"] == "owner" and "15%" in stop["reason"], stop
    state = gate.load_state(p)
    assert state["plan_growth_report"]["growth"] == 1500, state.get("plan_growth_report")
    assert state["plan_fix_baseline"]["reported_growth"] == 1500
    assert state["plan_fix_baseline"]["reported_at"]
    current = gate.build_ctx(p)["approval"]["version_sha256"]
    assert state["plan_fix_baseline"]["reported_version_sha256"] == current
    # Усечения нет: план остался таким, каким его оставила правка.
    assert plan_path(root).read_bytes() == before_bytes

    # Владелец снял стоп: по этой версии плана решение уже вынесено — тик идёт дальше.
    gate.clear_stop(p)
    second = gate.run_gate(p, ctx=ctx_with_fix(p), create_card=no_cards, notify_fn=quiet)
    assert second["decision"]["action"] != "stop", second["decision"]
    assert "расширила план" not in str(second["decision"].get("reason") or "")
    assert len(spy_comment) == 1, spy_comment


def test_closed_findings_by_registry(stand):
    """Закрытые находки считаются по реестру, а не по длине списка находок карточки."""
    root, p = stand
    registry = gate.plan_findings_path(p)
    gate.write_findings(registry, findings_rows("открыто", "открыто", "открыто", "открыто"))
    set_plan_size(root, p, 10000)
    ctx = gate.build_ctx(p)
    gate.save_state(p, {"plan_fix_baseline": gate.plan_fix_snapshot(
        p, 1, ctx, gate.open_findings_lines(ctx))})
    assert gate.load_state(p)["plan_fix_baseline"]["open_ids"] == ["F1", "F2", "F3", "F4"]

    gate.write_findings(registry, findings_rows("закрыто", "закрыто", "закрыто", "открыто"))
    append_to_plan(root, "x" * 1500)
    decision = gate._plan_growth_decision(ctx_with_fix(p))
    assert decision is not None, "рост сверх допуска прошёл молча"
    # Допуск по реестру: min(10 % = 1000, 400 × 3 закрытых = 1200) — закрыто три, не четыре.
    assert decision["growth"]["closed"] == 3, decision["growth"]
    assert "F1, F2, F3" in decision["details"], decision["details"]
    assert decision["growth"]["allowed"] == 1000, decision["growth"]

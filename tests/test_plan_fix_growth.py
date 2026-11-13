# -*- coding: utf-8 -*-
"""Правка плана не расширяет план (S4 плана 7): лимит `max_plan_growth` и owner-стоп.

Проверяется три вещи: допуск считается по проценту версии плана или по символам на
закрытую находку; превышение не режется молча — карточка правок получает owner-стоп
с цифрой, и по этой версии плана стоп не повторяется; замер уходит комментарием в
карточку правок, а не только в состояние.
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

import config as config_mod  # noqa: E402 - движок импортирует соседей плоско
import gate  # noqa: E402

DEFAULT_LIMIT = {"percent": 10.0, "chars_per_finding": 400}


@pytest.fixture()
def stand(project, hermes_stub, monkeypatch):
    """Стенд плана 1: проект после установки движка (план, утверждение, журнал).

    `hermes_stub` нужен ради `PIPELINE_KANBAN_DB`: без него страж читал бы настоящую борду.
    """
    monkeypatch.setenv("PIPELINE_REPO", str(project))
    return project, gate.resolve_paths(config_mod.load(project), "1")


@pytest.fixture()
def capsule() -> list:
    """Копилка комментариев: `_comment` подменяется, тест читает тексты."""
    return []


@pytest.fixture()
def spy_comment(monkeypatch, capsule):
    """`_comment` как заглушка: движок считает комментарий отправленным."""
    def fake(_p, card_id, text):
        capsule.append((card_id, text))
        return True
    monkeypatch.setattr(gate, "_comment", fake)
    return capsule


def plan_path(root: Path) -> Path:
    return root / "docs" / "plans" / "1-demo.md"


def append_to_plan(root: Path, text: str) -> None:
    """Дописать в план ровно `text`: так правка и выглядит в жизни — файл стал длиннее."""
    path = plan_path(root)
    path.write_text(path.read_text(encoding="utf-8") + text, encoding="utf-8")


def patch_growth(root: Path, **limits):
    """Лимит расширения в `pipeline.json` проекта и пути, перечитанные с ним."""
    cfg_path = root / "pipeline.json"
    data = json.loads(cfg_path.read_text(encoding="utf-8"))
    data["max_plan_growth"] = limits
    cfg_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return gate.resolve_paths(config_mod.load(root), "1")


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


def set_baseline(p, *findings: str) -> dict:
    """Базовая версия плана в состоянии — так её пишет заказ карточки правок."""
    baseline = gate.plan_fix_snapshot(p, 1, gate.build_ctx(p), list(findings))
    gate.save_state(p, {"plan_fix_baseline": baseline})
    return baseline


def quiet(_text: str) -> None:
    """Заглушка уведомлений: тексты проверяются там, где они обязательны."""


def test_growth_within_limit_passes(stand):
    """Правка в пределах допуска проходит, а карточка правок требует минимальной правки."""
    root, p = stand
    set_baseline(p, "F1: найти", "F2: поправить")      # допуск: 400 × 2 = 800 символов
    append_to_plan(root, "Короткая правка по находкам: " + "x" * 100 + "\n")
    assert gate._plan_growth_decision(ctx_with_fix(p)) is None

    body = gate.plan_fix_card_body(p, "docs/plans/1-demo.md", "docs/reviews/plan-1.md",
                                   ["F1: найти"])
    assert "минимальной правкой" in body, body
    assert "отчёт и журнал" in body, body


def test_growth_over_percent_stops(stand):
    """Расширение сверх процента даёт owner-стоп с процентом в причине, без усечения."""
    root, p = stand
    baseline = set_baseline(p, "F1: находка")
    append_to_plan(root, "Пояснение для ревьюера. " * 40)

    ctx = ctx_with_fix(p)
    decision = gate._plan_growth_decision(ctx)
    assert decision is not None, "расширение сверх лимита прошло молча"
    assert decision["action"] == "stop" and decision["kind"] == "owner", decision
    added = ctx["plan_version_len"] - baseline["version_len"]
    percent = round(added * 100 / baseline["version_len"])
    assert f"на {percent}%" in decision["reason"], decision["reason"]
    assert "находок закрыто 1" in decision["reason"], decision["reason"]
    # Усечения нет: файл плана остался таким, каким его оставила правка.
    assert plan_path(root).read_text(encoding="utf-8").endswith("ревьюера. ")


def test_growth_over_per_finding_stop(stand):
    """Лимит на закрытую находку срабатывает сам, даже когда процент нулевой."""
    root, p = stand
    set_baseline(p, "F1: находка")
    p = patch_growth(root, percent=0, chars_per_finding=10)
    append_to_plan(root, "y" * 200)

    decision = gate._plan_growth_decision(ctx_with_fix(p))
    assert decision is not None, "лимит на находку не сработал"
    assert decision["action"] == "stop" and decision["kind"] == "owner", decision
    assert "10 символов на находку" in decision["reason"], decision["reason"]


def test_growth_without_previous_version_passes(stand):
    """Предыдущей версии плана нет (или план не менялся) — сравнивать не с чем."""
    root, p = stand
    assert gate._plan_growth_decision(ctx_with_fix(p)) is None      # состояния нет вовсе

    set_baseline(p, "F1: находка")
    assert gate._plan_growth_decision(ctx_with_fix(p)) is None      # файл не менялся
    append_to_plan(root, "z" * 2000)
    # Правка не закрыта — мерить нечего: объём сверяется только после её карточки.
    assert gate._plan_growth_decision(ctx_with_fix(p, status="running")) is None


@pytest.mark.parametrize("value", [
    "не объект",
    ["percent"],
    {"percent": "abc"},
    {"percent": -5},
    {"percent": True},
    {"percent": 5, "chars": 1},
])
def test_broken_growth_config_raises(project, value):
    """Мусор в `max_plan_growth` — ошибка конфигурации, а не молчаливое умолчание."""
    cfg_path = project / "pipeline.json"
    data = json.loads(cfg_path.read_text(encoding="utf-8"))
    data["max_plan_growth"] = value
    cfg_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    with pytest.raises(config_mod.ConfigError):
        config_mod.load(project)


def test_growth_default_when_key_absent(stand):
    """Без ключа в `pipeline.json` действует умолчание движка, и оно видно в конфиге."""
    _root, p = stand
    assert p.cfg.max_plan_growth == DEFAULT_LIMIT
    assert p.cfg.state.get("max_plan_growth") == DEFAULT_LIMIT
    assert gate._plan_growth_limits(None) == DEFAULT_LIMIT


def test_growth_logged_to_comment(stand, spy_comment):
    """Замер расширения — комментарием в карточку правок; повторно по той же версии не идёт."""
    root, p = stand
    set_baseline(p, "F1: находка")
    append_to_plan(root, "Пояснение для ревьюера. " * 40)
    created: list = []
    no_cards = lambda *args, **kwargs: created.append(args) or None  # noqa: E731

    result = gate.run_gate(p, execute=True, notify_fn=quiet, create_card=no_cards,
                           ctx=ctx_with_fix(p))
    assert result["decision"]["action"] == "stop", result["decision"]
    assert result["decision"]["kind"] == "owner", result["decision"]
    assert created == [], "owner-стоп не заказывает карточек"
    stop = gate.load_stop(p)
    assert "расширила план" in stop["reason"] and stop["kind"] == "owner", stop

    assert spy_comment, "замер не ушёл комментарием"
    card_id, text = spy_comment[0]
    assert card_id == "t_fix01" and "Замер расширения плана" in text, spy_comment[0]
    state = gate.load_state(p)
    assert "Замер расширения плана" in state["plan_fix_growth_report"]
    current = gate.build_ctx(p)["approval"]["version_sha256"]
    assert state["plan_fix_baseline"]["reported_version_sha256"] == current

    # Владелец снял стоп: по этой версии плана решение уже вынесено — то же не повторяется.
    gate.clear_stop(p)
    assert gate._plan_growth_decision(ctx_with_fix(p)) is None, "owner-стоп повторяется"
    second = gate.run_gate(p, execute=True, notify_fn=quiet, create_card=no_cards,
                           ctx=ctx_with_fix(p))
    assert second["decision"]["action"] != "stop", second["decision"]
    assert "расширила план" not in second["decision"].get("reason", "")
    assert len(spy_comment) == 1, spy_comment

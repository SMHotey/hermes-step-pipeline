# -*- coding: utf-8 -*-
"""Правка плана не расширяет план (S4 плана 7): допуск из двух границ и owner-стоп.

Допуск — МЕНЬШЕЕ из процента версии плана и символов на каждую закрытую находку;
превышение даёт owner-стоп с замером, а план не усекается; замер уходит комментарием
в карточку правок и в состояние плана, и по той же версии плана стоп не повторяется.
Семь проверок: ровно столько требует контракт шага.
"""
from __future__ import annotations

import json
import sys
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
SHA_BEFORE = "a" * 64
SHA_AFTER = "b" * 64


@pytest.fixture()
def stand(project, hermes_stub, monkeypatch):
    """Стенд плана 1: проект после установки движка (план, утверждение, журнал).

    `hermes_stub` нужен ради `PIPELINE_KANBAN_DB`: без него страж читал бы настоящую борду.
    """
    monkeypatch.setenv("PIPELINE_REPO", str(project))
    return project, gate.resolve_paths(config_mod.load(project), "1")


def plan_path(root: Path) -> Path:
    return root / "docs" / "plans" / "1-demo.md"


def plan_chars(root: Path, p) -> int:
    """Объём версии плана — тем же счётом, что у стража: журнал нормализован."""
    return len(approval.plan_version_text(plan_path(root), approval.journal_marker_of(p.cfg)))


def append_to_plan(root: Path, text: str) -> None:
    """Дописать в план абзац обычного текста: файл стал длиннее, журнал не тронут."""
    path = plan_path(root)
    path.write_text(path.read_text(encoding="utf-8") + text, encoding="utf-8")


def baseline(chars: int, open_ids=(), findings_open: int = 1) -> dict:
    """Снимок версии плана до правки — той же формы, что пишет `plan_fix_snapshot`."""
    return {"round": 1, "version_sha256": SHA_BEFORE, "chars": chars,
            "open_ids": list(open_ids), "findings_open": findings_open,
            "at": "13.11.2026 12:00:00"}


def fix_card(card_id: str = "t_fix01", status: str = "done", round_no: int = 1) -> dict:
    """Карточка правки плана в ctx: форма `gate.read_cards`, времена в прошлом."""
    now = 1_760_000_000
    return {"id": card_id, "title": "[правки плана 1] Правки плана по ревью",
            "status": status, "assignee": "plan_fixer", "key": f"planloop:1:planfix:{round_no}",
            "completed_at": now, "result": "Итог: находки закрыты", "error": "",
            "created_at": now - 60, "started_at": now - 60}


def ctx_with_fix(p, base: dict, *, after_chars: int, statuses=(), findings: int = 3) -> dict:
    """Синтетический `ctx` решения: снимок, объём после правки, статусы реестра находок."""
    ctx = gate.build_ctx(p)
    ctx["plan_fix_cards"] = [fix_card()]
    ctx["plan_fix_baseline"] = dict(base)
    ctx["plan_version_chars"] = after_chars
    ctx["approval"] = dict(ctx.get("approval") or {}, version_sha256=SHA_AFTER, state="changed")
    rows = list(statuses) or ["закрыто"] * findings
    ctx["plan_findings"] = [{"id": f"F{index}", "step": "S1", "kind": "стиль",
                             "text": f"находка {index}", "action": "поправить",
                             "status": status}
                            for index, status in enumerate(rows, start=1)]
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


def test_growth_within_limit_no_stop(stand):
    """Рост 600 при допуске 1000 (3 находки: 3 × 400 больше 10 %) — стопа нет."""
    _root, p = stand
    ctx = ctx_with_fix(p, baseline(10000, ["F1", "F2", "F3"]), after_chars=10600,
                       statuses=("закрыто", "закрыто", "закрыто"))
    assert gate._plan_growth_decision(ctx) is None
    # Граница процента здесь меньше границы находок, и замер обязан брать именно её.
    assert gate.plan_growth_report(10000, 10600, 3, 10, 400)["allowed"] == 1000


def test_growth_over_percent_stops(stand):
    """Рост 1500 при допуске min(10 % = 1000, 400 × 10) — owner-стоп, а не правка втихую."""
    _root, p = stand
    statuses = ("закрыто",) * 10
    ctx = ctx_with_fix(p, baseline(10000, [f"F{index}" for index in range(1, 11)]),
                       after_chars=11500, statuses=statuses, findings=10)
    decision = gate._plan_growth_decision(ctx)
    assert decision is not None, "рост сверх допуска прошёл молча"
    assert decision["action"] == "stop" and decision["kind"] == "owner", decision
    assert decision["growth"]["over"] is True, decision["growth"]
    assert decision["plan_review"] is True, decision
    assert decision.get("step") is None, "стоп планового уровня не должен занимать слот шага"
    reason = decision["reason"]
    for part in ("на 15%", "1500", "находок закрыто 10", "1000"):
        assert part in reason, (part, reason)
    # Граница находок (400 × 10 = 4000) не может перекрыть процент: берётся меньшее.
    assert decision["growth"]["allowed"] == 1000, decision["growth"]
    comment = decision["comment"]
    assert comment["card"] == "t_fix01", comment
    assert "СТОП-СИГНАЛ" in comment["text"] and "11500" in comment["text"], comment


def test_growth_over_per_finding_cap_stops(stand):
    """Рост 900 при допуске min(10 % = 2000, 400 × 1) — граница находок, процент «4.5»."""
    _root, p = stand
    ctx = ctx_with_fix(p, baseline(20000, ["F1"]), after_chars=20900, statuses=("закрыто",),
                       findings=1)
    decision = gate._plan_growth_decision(ctx)
    assert decision is not None, "граница на находку не сработала"
    assert decision["growth"]["allowed"] == 400, decision["growth"]
    assert "на 4.5%" in decision["reason"], decision["reason"]
    assert "закрыто 1" in decision["reason"], decision["reason"]


def test_no_previous_version_no_limit(stand):
    """Сравнивать не с чем (нет базы либо версия неизвестна) — легаси-план не наказан."""
    _root, p = stand
    ctx = ctx_with_fix(p, {}, after_chars=10 ** 6)
    assert gate._plan_growth_decision(ctx) is None

    # Снимок без версии: сравнивать не с чем, даже если объём вырос втрое.
    unnamed = baseline(10000, ["F1"])
    unnamed.pop("version_sha256")
    ctx = ctx_with_fix(p, unnamed, after_chars=30000, statuses=("закрыто",), findings=1)
    assert gate._plan_growth_decision(ctx) is None


def test_broken_config_raises(project):
    """Мусор в `max_plan_growth` — `ConfigError` с полем; частичный ключ — умолчание."""
    write_cfg(project, {"percent": "десять", "chars_per_finding": 400})
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
    assert config_mod.DEFAULTS["max_plan_growth"] == cfg.max_plan_growth
    assert gate.PLAN_GROWTH_PERCENT == 10
    assert gate.PLAN_GROWTH_CHARS_PER_FINDING == 400


def test_stop_logged_once_and_plan_not_truncated(stand, monkeypatch):
    """Сквозной путь на файлах: стоп и замер на диске, повторный тик тот же стоп не выносит."""
    root, p = stand
    state = gate.load_state(p)
    state["plan_fix_baseline"] = gate.plan_fix_snapshot(p, 1, gate.build_ctx(p),
                                                       ["F1: находка"])
    gate.save_state(p, state)
    # Рост заметно больше и процента версии, и 400 символов на находку: абзац, не строка
    # таблицы, — журнал `plan_version_text` нормализует, обычный текст входит в версию.
    append_to_plan(root, "\n" + "Правка плана по находке ревью. " * 50 + "\n")
    before_bytes = plan_path(root).read_bytes()
    calls: list = []

    def spy(_p, card_id, text):
        calls.append((card_id, text))
        return True

    monkeypatch.setattr(gate, "_comment", spy)
    ctx2 = gate.build_ctx(p)
    ctx2["plan_fix_cards"] = [fix_card()]
    result = gate.run_gate(p, ctx=ctx2, create_card=no_cards, notify_fn=quiet)
    decision = result["decision"]
    assert decision["action"] == "stop", decision
    assert decision["kind"] == "owner", decision
    assert result["applied"] is True, result
    saved = gate.load_state(p)["plan_fix_baseline"]
    assert saved["reported_version_sha256"] == ctx2["approval"]["version_sha256"], saved
    assert calls and calls[0][0] == "t_fix01", calls
    assert len(calls) == 1, calls
    # Усечения нет: план остался таким, каким его оставила правка.
    assert plan_path(root).read_bytes() == before_bytes

    # Владелец стоп не снимал: по этой версии плана решение уже вынесено, конвейер стоит.
    ctx3 = gate.build_ctx(p)
    ctx3["plan_fix_cards"] = [fix_card()]
    second = gate.run_gate(p, ctx=ctx3, create_card=no_cards, notify_fn=quiet)
    assert second["decision"]["action"] == "idle", second["decision"]
    assert len(calls) == 1, calls

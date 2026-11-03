# -*- coding: utf-8 -*-
"""Зачем: §S7 плана 4 — страж не ведёт неутверждённый план.

Проверяются три состояния записи утверждения (`approved`/`missing`/`changed`),
пропуск неутверждённого плана без карточки, унаследованная запись для уже идущих
планов (файл состояния или закрытые шаги журнала), одно уведомление на план и то, что
пропуск не съедает действие тика у соседнего плана.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import config as config_mod
import gate
import registry
from engine_kit import PLAN, cards, plan_sha256, write_approval


@pytest.fixture()
def env(project: Path, monkeypatch) -> Path:
    """Временный проект с чистым окружением движка (образец — test_watchdog.py)."""
    for name in ("PIPELINE_CONFIG", "PIPELINE_PLANS", "PIPELINE_STATE_DIR",
                 "PIPELINE_BOARD"):
        monkeypatch.delenv(name, raising=False)
    # collector() запускает next_step.py, а тот берёт корень из PIPELINE_REPO.
    monkeypatch.setenv("PIPELINE_REPO", str(project))
    return project


def load_cfg(root: Path, **overrides) -> config_mod.Config:
    """Конфиг временного проекта с правками pipeline.json (require_prompt и т.п.)."""
    raw = json.loads((root / "pipeline.json").read_text(encoding="utf-8"))
    raw.update(overrides)
    (root / "pipeline.json").write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    return config_mod.load(root)


def plan_path(root: Path) -> Path:
    return Path(root) / "docs" / "plans" / "1-demo.md"


def drop_approval(root: Path) -> None:
    registry.approval_file_for(config_mod.load(root), "1").unlink()


def read_state(root: Path, plan_id: str = "1") -> dict:
    path = Path(root) / ".pipeline" / "state" / f"plan_{plan_id}_state.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}


def test_registry_status_missing_approved_and_changed(env: Path):
    cfg = config_mod.load(env)
    spec = registry.get(cfg, "1")

    drop_approval(env)
    missing = registry.approval_status(cfg, spec)
    assert missing["state"] == "missing"
    assert "не утверждён" in missing["reason"]
    assert missing["record"] is None

    write_approval(env, plan_path(env))
    approved = registry.approval_status(cfg, spec)
    assert approved["state"] == "approved"
    assert approved["reason"] == ""
    assert approved["sha256"] == plan_sha256(plan_path(env))
    assert approved["record"]["sha256"] == approved["sha256"]

    write_approval(env, plan_path(env), sha="0" * 64)
    changed = registry.approval_status(cfg, spec)
    assert changed["state"] == "changed"
    assert "изменён после утверждения" in changed["reason"]

    registry.approval_file_for(cfg, "1").write_text("{", encoding="utf-8")
    broken = registry.approval_status(cfg, spec)          # битый JSON — не исключение
    assert broken["state"] == "missing"
    assert "не утверждён" in broken["reason"]


def test_new_plan_without_approval_gets_no_card(env: Path):
    cfg = config_mod.load(env)
    drop_approval(env)
    assert registry.plan_started(cfg, registry.get(cfg, "1")) is False

    p = gate.resolve_paths(cfg, "1")
    created: list = []
    res = gate.run_gate(p, create_card=lambda *a, **k: created.append(a) or None,
                        notify_fn=lambda text: None)

    assert created == []
    assert res["decision"]["action"] == "idle"
    assert res["decision"]["approval_blocked"] is True
    assert "не утверждён" in res["decision"]["reason"]
    assert not registry.approval_file_for(cfg, "1").exists()


def test_changed_plan_is_blocked_with_its_own_reason(env: Path):
    (Path(env) / "docs" / "prompts" / "2026-11-03_1-S1_prompt.md").write_text(
        "# постановка\n", encoding="utf-8")
    write_approval(env, plan_path(env), sha="f" * 64)
    cfg = config_mod.load(env)

    created: list = []
    res = gate.run_gate(gate.resolve_paths(cfg, "1"),
                        create_card=lambda *a, **k: created.append(a) or None,
                        notify_fn=lambda text: None)

    assert created == []
    assert res["decision"]["approval_blocked"] is True
    assert "изменён после утверждения" in res["decision"]["reason"]


def test_approved_plan_creates_step_card_as_before(env: Path, hermes_stub: Path):
    cfg = load_cfg(env, require_prompt=False)
    res = gate.run_gate(gate.resolve_paths(cfg, "1"))

    assert res["decision"]["action"] == "create_step"
    assert "planloop:1:S1:step" in [c["key"] for c in cards(hermes_stub)]


def test_started_plan_gets_inherited_record_and_continues(env: Path, hermes_stub: Path,
                                                          capsys):
    drop_approval(env)
    state_dir = Path(env) / ".pipeline" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "plan_1_state.json").write_text(json.dumps({"hold_steps": []}),
                                                 encoding="utf-8")
    cfg = load_cfg(env, require_prompt=False)
    p = gate.resolve_paths(cfg, "1")

    first = gate.run_gate(p)
    assert first["decision"]["action"] == "create_step"
    assert "planloop:1:S1:step" in [c["key"] for c in cards(hermes_stub)]
    assert "унаследована" in capsys.readouterr().out

    record_path = registry.approval_file_for(cfg, "1")
    record = json.loads(record_path.read_text(encoding="utf-8"))
    assert record["inherited"] is True
    assert record["sha256"] == plan_sha256(plan_path(env))
    assert record["plan"] == "1"
    assert record["file"] == "docs/plans/1-demo.md"
    assert record["approved_at"]
    before = record_path.read_bytes()

    gate.run_gate(p)
    assert "унаследована" not in capsys.readouterr().out
    assert record_path.read_bytes() == before


def test_started_by_closed_steps_inherits_and_moves_on(env: Path, hermes_stub: Path):
    plan = plan_path(env)
    text = plan.read_text(encoding="utf-8").replace(
        "| — | S1 | — | — | открыт |",
        "| 02.11.2026 | S1 (Таблица счетов) | abc1234 | ревью APPROVE | ЗАКРЫТ |")
    plan.write_text(text, encoding="utf-8")
    drop_approval(env)
    cfg = load_cfg(env, require_prompt=False)

    res = gate.run_gate(gate.resolve_paths(cfg, "1"))

    record = json.loads(registry.approval_file_for(cfg, "1").read_text(encoding="utf-8"))
    assert record["inherited"] is True
    assert res["decision"]["action"] == "create_step"
    assert "planloop:1:S2:step" in [c["key"] for c in cards(hermes_stub)]


def test_approval_notice_is_sent_once_per_plan(env: Path):
    drop_approval(env)
    cfg = config_mod.load(env)
    p = gate.resolve_paths(cfg, "1")
    sent: list[str] = []

    gate.run_gate(p, notify_fn=sent.append)
    gate.run_gate(p, notify_fn=sent.append)

    assert len(sent) == 1
    assert "не утверждён" in sent[0]
    assert "approval_notice" in read_state(env)


def test_status_mode_prints_reason_and_writes_nothing(env: Path):
    drop_approval(env)
    cfg = config_mod.load(env)
    p = gate.resolve_paths(cfg, "1")
    created: list = []

    res = gate.run_gate(p, execute=False,
                        create_card=lambda *a, **k: created.append(a) or None)

    assert res["decision"]["approval_blocked"] is True
    assert "не утверждён" in res["status"]
    assert created == []
    assert not registry.approval_file_for(cfg, "1").exists()
    assert not (Path(env) / ".pipeline" / "state" / "plan_1_state.json").exists()


def test_blocked_plan_does_not_start_idle_watchdog(env: Path):
    drop_approval(env)
    cfg = config_mod.load(env)

    gate.run_gate(gate.resolve_paths(cfg, "1"), notify_fn=lambda text: None)

    state = read_state(env)
    assert "approval_notice" in state
    assert "waiting_since" not in state


def test_blocked_plan_does_not_starve_another_plan(env: Path, hermes_stub: Path):
    plans = Path(env) / "docs" / "plans"
    (plans / "2-demo.md").write_text(PLAN, encoding="utf-8")
    write_approval(env, plans / "2-demo.md")
    drop_approval(env)
    cfg = load_cfg(env, require_prompt=False, max_actions=1, driven_plans=["1", "2"])

    gate.tick(cfg)

    keys = [c["key"] for c in cards(hermes_stub)]
    assert "planloop:2:S1:step" in keys
    assert [k for k in keys if str(k).startswith("planloop:1:")] == []

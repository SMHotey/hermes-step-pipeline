# -*- coding: utf-8 -*-
"""Зачем: простаивающий идущий план не получал унаследованное утверждение.

Тик звал `ensure_approval` только внутри `run_gate`, а до `run_gate` доходили лишь планы
с непустым решением. План, у которого шаг уже в работе, простаивает (`idle`), и запись
утверждения оставалась со старым sha — панель вечно показывала «изменён после
утверждения», а кнопки «Утвердить» нет: у стартовавшего плана блокировки нет.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from stub_hermes import SCHEMA

import config as config_mod
import gate
import registry
from engine_kit import cards, plan_sha256, plan_version_sha, write_approval


def plan_path(root: Path) -> Path:
    return Path(root) / "docs" / "plans" / "1-demo.md"


def work_card(db: Path, key: str, status: str = "todo") -> None:
    """Карточка шага «в работе»: решение стража — детерминированный idle."""
    con = sqlite3.connect(db)
    try:
        con.executescript(SCHEMA)
        con.execute("INSERT OR REPLACE INTO tasks (id, title, status, assignee,"
                    " idempotency_key, created_at) VALUES (?,?,?,?,?,?)",
                    ("t_idle001", "шаг", status, "demo", key, 0))
        con.commit()
    finally:
        con.close()


def mark_started(root: Path) -> None:
    """План уже ведётся: файл состояния (как в test_approval.py)."""
    state_dir = Path(root) / ".pipeline" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "plan_1_state.json").write_text(json.dumps({"hold_steps": []}),
                                                 encoding="utf-8")


@pytest.fixture()
def env(project: Path, hermes_stub: Path, monkeypatch) -> Path:
    for name in ("PIPELINE_CONFIG", "PIPELINE_PLANS", "PIPELINE_STATE_DIR",
                 "PIPELINE_BOARD"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PIPELINE_REPO", str(project))
    raw = json.loads((project / "pipeline.json").read_text(encoding="utf-8"))
    raw["notify"] = {"enabled": False}
    raw["require_prompt"] = False
    (project / "pipeline.json").write_text(json.dumps(raw, ensure_ascii=False),
                                           encoding="utf-8")
    return project


def test_idle_started_plan_inherits_approval_and_panel_flips(env: Path, hermes_stub: Path,
                                                             capsys):
    write_approval(env, plan_path(env), sha="f" * 64)   # запись «изменена после утверждения»
    mark_started(env)
    work_card(hermes_stub, "planloop:1:S1:step")
    cfg = config_mod.load(env)

    res = gate.tick(cfg)

    out = capsys.readouterr().out
    assert "унаследована" in out
    assert res["actions"] == [] and res["applied"] is False
    assert cards(hermes_stub)[0]["status"] == "todo"     # борду тик не трогал

    record_path = registry.approval_file_for(cfg, "1")
    record = json.loads(record_path.read_text(encoding="utf-8"))
    assert record["inherited"] is True
    assert record["sha256"] == plan_sha256(plan_path(env))
    assert record["version_sha256"] == plan_version_sha(plan_path(env))

    # Панель берёт утверждение из того же build_ctx/plan_view: бейдж гаснет без действий.
    view = gate.plan_view(cfg, gate.resolve_paths(cfg, "1"),
                          set(registry.driven_ids(cfg)), True)
    assert view["approval"]["state"] == "approved"
    assert view["action"]["kind"] == "none"

    before = record_path.read_bytes()
    gate.tick(cfg)
    assert "унаследована" not in capsys.readouterr().out
    assert record_path.read_bytes() == before


def test_unstarted_plan_is_still_blocked_and_not_inherited(env: Path, capsys):
    """Обратная сторона: план без закрытых шагов и файла состояния наследовать нельзя."""
    write_approval(env, plan_path(env), sha="f" * 64)
    cfg = config_mod.load(env)
    record_path = registry.approval_file_for(cfg, "1")
    before = record_path.read_bytes()

    gate.tick(cfg)

    out = capsys.readouterr()
    assert "унаследована" not in out.out
    assert "не утверждён" in out.err or "изменён после утверждения" in out.err
    assert record_path.read_bytes() == before

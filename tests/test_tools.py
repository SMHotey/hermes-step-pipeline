# -*- coding: utf-8 -*-
"""Инструменты агента: статус, тик, управление — на реальном движке проекта."""
from __future__ import annotations

import json
import sys
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

from helpers import run_gate, stub_cards  # noqa: E402
import tools  # noqa: E402


def test_status_reports_plan_and_decision(project: Path, hermes_stub: Path):
    data = json.loads(tools.handle_status({"project": str(project)}))
    assert data["ok"] is True
    assert data["project"] == "demo"
    assert data["plan"]["next"]["id"] == "S1"
    assert data["decision"]["action"] == "create_step"
    assert data["plan"]["steps_total"] == 2


def test_status_without_project_is_error(tmp_path: Path):
    data = json.loads(tools.handle_status({"project": str(tmp_path)}))
    assert data["ok"] is False
    assert "setup" in data["error"]


def test_status_limits_cards(project: Path, hermes_stub: Path):
    run_gate(project, "--run")
    data = json.loads(tools.handle_status({"project": str(project), "cards": 1}))
    assert len(data["cards"]) == 1


def test_run_creates_prompt_card(project: Path, hermes_stub: Path):
    data = json.loads(tools.handle_run({"project": str(project)}))
    assert data["ok"] is True
    assert data["result"]["decision"]["action"] == "create_step"
    assert any(c["key"].endswith(":prompt") for c in stub_cards(hermes_stub))


def test_control_stop_then_resume(project: Path, hermes_stub: Path):
    data = json.loads(tools.handle_control({"project": str(project), "action": "stop",
                                            "reason": "тест"}))
    assert data["ok"] is True
    stop = json.loads((project / ".pipeline" / "state" / "plan_1_stop.json").read_text(encoding="utf-8"))
    assert stop["kind"] == "owner" and stop["reason"] == "тест"
    data = json.loads(tools.handle_control({"project": str(project), "action": "resume"}))
    assert data["ok"] is True
    assert not (project / ".pipeline" / "state" / "plan_1_stop.json").exists()


def test_control_hold_requires_step(project: Path, hermes_stub: Path):
    data = json.loads(tools.handle_control({"project": str(project), "action": "hold"}))
    assert data["ok"] is False
    assert "step" in data["error"]


def test_control_unknown_action(project: Path, hermes_stub: Path):
    data = json.loads(tools.handle_control({"project": str(project), "action": "fly"}))
    assert data["ok"] is False


def test_tool_schemas_are_wellformed():
    names = [name for name, _schema, _handler, _emoji in tools.TOOLS]
    assert names == ["pipeline_status", "pipeline_run", "pipeline_control"]
    for name, schema, handler, _emoji in tools.TOOLS:
        assert schema["name"] == name
        assert schema["parameters"]["type"] == "object"
        assert callable(handler)

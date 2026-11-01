# -*- coding: utf-8 -*-
"""Конфигурация и реестр планов."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import config as config_mod
import registry


def test_missing_config_is_error(tmp_path: Path):
    with pytest.raises(config_mod.ConfigError):
        config_mod.load(tmp_path)


def test_defaults_merged(project: Path):
    cfg = config_mod.load(project)
    assert cfg.board == "testboard"
    assert cfg.project == "demo"
    assert cfg.driver == "demo"
    assert cfg.prompter == "prompter"
    assert cfg.reviewer == "reviewer"
    assert cfg.require_prompt is True
    assert cfg.step_runtime == "3h"
    assert cfg.review_runtime == "150m"
    assert cfg.plans_dir == project / "docs" / "plans"
    assert cfg.state_dir == project / ".pipeline" / "state"


def test_env_overrides(project: Path, monkeypatch):
    monkeypatch.setenv("PIPELINE_BOARD", "other")
    monkeypatch.setenv("PIPELINE_STATE_DIR", str(project / "tmpstate"))
    monkeypatch.setenv("PIPELINE_PLANS", "7")
    cfg = config_mod.load(project)
    assert cfg.board == "other"
    assert cfg.state_dir == project / "tmpstate"
    assert cfg.driven == ("7",)


def test_partial_config_keeps_defaults(project: Path):
    path = project / "pipeline.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data.pop("runtimes")
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    cfg = config_mod.load(project)
    assert cfg.step_runtime == "3h"
    assert cfg.review_runtime == "150m"


def test_registry_discovers_plan_with_journal(project: Path):
    cfg = config_mod.load(project)
    found = registry.discover(cfg)
    assert [s.id for s in found] == ["1"]
    assert found[0].journal_heading.startswith("## Журнал")
    assert found[0].state_file.name == "plan_1_state.json"


def test_plan_without_journal_is_ignored(project: Path):
    (project / "docs" / "plans" / "2-prose.md").write_text("# 2. Проза\n\nБез журнала.\n",
                                                          encoding="utf-8")
    cfg = config_mod.load(project)
    assert [s.id for s in registry.discover(cfg)] == ["1"]


def test_closed_steps_from_journal(project: Path):
    cfg = config_mod.load(project)
    plan = project / "docs" / "plans" / "1-demo.md"
    plan.write_text(plan.read_text(encoding="utf-8")
                    + "| 01.11.2026 | S1 | abc123 | ревью APPROVE | ЗАКРЫТ |\n",
                    encoding="utf-8")
    spec = registry.get(cfg, "1")
    assert registry.closed_steps(spec, cfg) == ["S1"]


def test_driven_plans_star_and_list(project: Path):
    cfg = config_mod.load(project)
    assert registry.driven_ids(cfg) == ("1",)
    data = json.loads((project / "pipeline.json").read_text(encoding="utf-8"))
    data["driven_plans"] = []
    (project / "pipeline.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    assert registry.driven_ids(config_mod.load(project)) == ()


def test_unknown_plan_raises(project: Path):
    cfg = config_mod.load(project)
    with pytest.raises(registry.PlanNotFound):
        registry.get(cfg, "9")

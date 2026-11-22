# -*- coding: utf-8 -*-
"""Конфигурация и реестр планов."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import config as config_mod
import registry
from engine_kit import make_project


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


def write_plan(root: Path, name: str, plan_id: int) -> Path:
    """Положить в планы проекта ещё один план с журналом (иначе реестр его не увидит)."""
    path = Path(root) / "docs" / "plans" / name
    path.write_text(
        f"# {plan_id}. Демо-план по адресу\n\nЦель: второй план проекта.\n\n"
        "## Шаги\n\n"
        f"### S1. Первый шаг {plan_id}-го плана (1 дн)\n\n"
        "- Файл: `app/second.py`\n"
        "- Готово = шаг есть\n\n"
        "## Журнал выполнения\n\n"
        "| Дата | Шаг | Коммиты | Прогоны и доказательства | Статус |\n"
        "|---|---|---|---|---|\n"
        "| — | S1 | — | — | открыт |\n",
        encoding="utf-8")
    return path


def test_driven_default_id_is_first_driven(tmp_path: Path):
    # Суть шага: «план для чтения» и «план, которому служит тик» — разные ответы.
    root = make_project(tmp_path / "driven-default", driven_plans=["2"])
    write_plan(root, "2-address.md", 2)
    cfg = config_mod.load(root)
    assert registry.driven_default_id(cfg) == "2"
    assert registry.default_plan_id(cfg) == "1"


def test_driven_default_id_with_star(tmp_path: Path):
    root = make_project(tmp_path / "driven-star", driven_plans=["*"])
    write_plan(root, "2-address.md", 2)
    cfg = config_mod.load(root)
    assert registry.driven_default_id(cfg) == "1"


def test_driven_default_id_empty_raises(tmp_path: Path):
    root = make_project(tmp_path / "driven-empty", driven_plans=[])
    cfg = config_mod.load(root)
    with pytest.raises(registry.PlanNotFound) as err:
        registry.driven_default_id(cfg)
    assert "ни один план не ведётся" in str(err.value)


def test_unknown_plan_raises(project: Path):
    cfg = config_mod.load(project)
    with pytest.raises(registry.PlanNotFound):
        registry.get(cfg, "9")

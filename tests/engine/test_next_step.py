# -*- coding: utf-8 -*-
"""Сборщик контекста шага: разбор плана и выбор следующего шага."""
from __future__ import annotations

import json
from pathlib import Path

import config as config_mod
import next_step


def test_plan_steps_parsed(project: Path):
    cfg = config_mod.load(project)
    steps = next_step.plan_steps(cfg, cfg.plans_dir / "1-demo.md")
    assert [s.id for s in steps] == ["S1", "S2"]
    first, second = steps
    assert first.title == "Таблица счетов"
    assert first.days == 1.0
    assert first.owner == ""          # владелец не указан — берётся из конфига
    assert first.files == ["app/models/invoice.py", "tests/test_invoice.py"]
    assert first.gates and "pytest" in first.gates[0]
    assert first.dod.startswith("таблица `invoice`")
    assert "не менять контракт" in second.notes
    assert second.owner == "backend"


def test_owner_falls_back_to_driver(project: Path):
    cfg = config_mod.load(project)
    step = next_step.plan_steps(cfg, cfg.plans_dir / "1-demo.md")[0]
    assert (step.owner or cfg.driver) == "demo"


def test_next_is_first_open_step(project: Path):
    cfg = config_mod.load(project)
    pkg = next_step.build_package(cfg, "1")
    assert pkg["next"]["id"] == "S1"
    assert pkg["steps_total"] == 2
    assert pkg["remaining"] == ["S1", "S2"]


def test_closed_step_is_skipped(project: Path):
    plan = project / "docs" / "plans" / "1-demo.md"
    plan.write_text(plan.read_text(encoding="utf-8")
                    + "| 01.11.2026 | S1 | abc123 | ревью APPROVE | ЗАКРЫТ |\n",
                    encoding="utf-8")
    cfg = config_mod.load(project)
    pkg = next_step.build_package(cfg, "1")
    assert pkg["next"]["id"] == "S2"
    assert pkg["closed"] == ["S1"]


def test_all_closed_gives_note(project: Path):
    plan = project / "docs" / "plans" / "1-demo.md"
    plan.write_text(plan.read_text(encoding="utf-8")
                    + "| 01.11.2026 | S1 | a | r | ЗАКРЫТ |\n"
                    + "| 02.11.2026 | S2 | b | r | ЗАКРЫТ |\n", encoding="utf-8")
    cfg = config_mod.load(project)
    pkg = next_step.build_package(cfg, "1")
    assert pkg["next"] is None
    assert "все шаги" in pkg["note"]


def test_force_step(project: Path):
    cfg = config_mod.load(project)
    pkg = next_step.build_package(cfg, "1", force_step="s2")
    assert pkg["next"]["id"] == "S2"


def test_plan_without_steps_section(tmp_path: Path, project: Path):
    (project / "docs" / "plans" / "2-prose.md").write_text(
        "# 2. Проза\n\n## Журнал\n\n| Дата | Шаг | Коммиты | Прогоны | Статус |\n"
        "|---|---|---|---|---|\n| — | S1 | — | — | открыт |\n", encoding="utf-8")
    cfg = config_mod.load(project)
    pkg = next_step.build_package(cfg, "2")
    assert pkg["next"] is None
    assert pkg["steps_total"] == 0
    assert "раздел шагов" in pkg["note"]


def test_list_plans_overview(project: Path):
    cfg = config_mod.load(project)
    rows = next_step.plans_overview(cfg)
    assert rows[0]["id"] == "1"
    assert rows[0]["next"] == "S1"
    assert rows[0]["steps"] == 2


def test_json_is_serialisable(project: Path):
    cfg = config_mod.load(project)
    pkg = next_step.build_package(cfg, "1")
    text = json.dumps(pkg, ensure_ascii=False)
    assert '"next"' in text
    assert pkg["plan_file"].endswith("docs/plans/1-demo.md")

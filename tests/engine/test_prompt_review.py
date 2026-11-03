# -*- coding: utf-8 -*-
"""Ревью постановки перед стартом шага (план 2, §S6, правило 3).

Постановка без отметки `recon_commit` ревью не требует (режим `auto`), REQUEST_CHANGES
по постановке останавливает старт, неизвестный режим в `pipeline.json` — ошибка
конфигурации, а не молчаливое «auto».
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import config as config_mod
import gate
from engine_kit import cards, complete, plan_body, write_plan, write_prompt


@pytest.fixture()
def stand(project: Path, hermes_stub: Path, monkeypatch):
    monkeypatch.setenv("PIPELINE_REPO", str(project))
    cfg = config_mod.load(project)
    return project, cfg, gate.resolve_paths(cfg, "1"), hermes_stub


def quiet(_text: str) -> None:
    return None


def test_prompt_without_recon_is_not_reviewed(stand):
    root, cfg, p, db = stand
    write_plan(root, plan_body(3))
    write_prompt(root, "S1")                       # отметки recon_commit нет

    res = gate.run_gate(p, execute=True, notify_fn=quiet)
    assert res["decision"]["action"] == "create_step", res["decision"]
    keys = {c["key"] for c in cards(db)}
    assert "planloop:1:S1:promptreview" not in keys


def test_request_changes_on_prompt_stops_start(stand):
    root, cfg, p, db = stand
    write_plan(root, plan_body(3))
    write_prompt(root, "S1", recon="1111111")

    first = gate.run_gate(p, execute=True, notify_fn=quiet)
    assert first["decision"]["action"] == "create_prompt_review", first["decision"]
    complete(db, ":promptreview")
    (root / "docs" / "reviews" / "1-S1-prompt.md").write_text(
        "recon_commit: 1111111\n\n## ВЕРДИКТ: REQUEST_CHANGES\n\n"
        "- постановка ссылается на несуществующий файл\n", encoding="utf-8")

    res = gate.run_gate(p, execute=True, notify_fn=quiet)
    assert res["decision"]["action"] == "stop", res["decision"]
    assert "переписать" in res["detail"]
    keys = {c["key"] for c in cards(db)}
    assert "planloop:1:S1:step" not in keys         # шаг не начат
    stop = json.loads((root / ".pipeline" / "state" / "plan_1_stop.json")
                      .read_text(encoding="utf-8"))
    assert stop["kind"] == "incident"


def test_unknown_prompt_review_mode_is_rejected(project: Path):
    raw = json.loads((project / "pipeline.json").read_text(encoding="utf-8"))
    raw["prompt_review"] = "sometimes"
    (project / "pipeline.json").write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(config_mod.ConfigError) as exc:
        config_mod.load(project)
    assert "prompt_review" in str(exc.value)

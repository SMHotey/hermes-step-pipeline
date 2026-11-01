# -*- coding: utf-8 -*-
"""End-to-end: движок, который плагин ставит в проект, ведёт шаг до журнала."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from helpers import complete_card, install_project, stub_cards


def sh(cmd: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True,
                          encoding="utf-8", errors="replace")


def git(root: Path, *args: str) -> str:
    res = sh(["git", *args], root)
    assert res.returncode == 0, res.stderr
    return res.stdout.strip()


def gate(root: Path, *args: str) -> subprocess.CompletedProcess:
    return sh([sys.executable, str(root / ".pipeline" / "gate.py"), *args], root)


def test_full_cycle_close_step_and_move_on(tmp_path: Path, hermes_stub: Path):
    root = install_project(tmp_path / "e2e", board="e2eboard")
    cfg = json.loads((root / "pipeline.json").read_text(encoding="utf-8"))
    cfg["git"] = {"commit": True, "push": False}
    (root / "pipeline.json").write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    git(root, "init", "-b", "main")
    git(root, "config", "user.email", "test@example.com")
    git(root, "config", "user.name", "Test")
    git(root, "add", "-A")
    git(root, "commit", "-m", "init")

    # 1. нет постановки → карточка промптеру + инцидентный стоп
    assert gate(root, "--run").returncode == 0
    cards = stub_cards(hermes_stub)
    assert [c["key"] for c in cards] == ["planloop:1:S1:prompt"]
    assert cards[0]["assignee"] == "prompter"

    # 2. постановка появилась → карточка шага, инцидент снят
    (root / "docs" / "prompts" / "2026-11-01_1-S1_prompt.md").write_text("# промт\n", encoding="utf-8")
    assert gate(root, "--run").returncode == 0
    keys = {c["key"] for c in stub_cards(hermes_stub)}
    assert "planloop:1:S1:step" in keys
    assert not (root / ".pipeline" / "state" / "plan_1_stop.json").exists()

    # 3. шаг выполнен → карточка ревью
    complete_card(hermes_stub, ":step")
    assert gate(root, "--run").returncode == 0
    review = [c for c in stub_cards(hermes_stub) if c["key"].endswith(":review:1")]
    assert review and review[0]["assignee"] == "reviewer"

    # 4. APPROVE со ссылкой на реальный коммит → журнал + commit
    sha = git(root, "rev-parse", "HEAD")
    (root / "docs" / "reviews" / "1-S1.md").write_text(
        f"## ВЕРДИКТ: APPROVE\n\nОбъект ревью: коммит `{sha}`\n", encoding="utf-8")
    complete_card(hermes_stub, ":review:1")
    assert gate(root, "--run").returncode == 0
    plan = (root / "docs" / "plans" / "1-demo.md").read_text(encoding="utf-8")
    assert "| ЗАКРЫТ |" in plan
    assert "закрыт шаг S1" in git(root, "log", "-1", "--pretty=%s")

    # 5. следующий тик берёт S2 и снова ждёт постановку
    assert gate(root, "--run").returncode == 0
    keys = {c["key"] for c in stub_cards(hermes_stub)}
    assert "planloop:1:S2:prompt" in keys


def test_status_json_has_steps(tmp_path: Path, hermes_stub: Path):
    root = install_project(tmp_path / "e2e2")
    res = gate(root, "--status", "--json")
    payload = json.loads(res.stdout)
    assert payload["decision"]["action"] == "create_step"
    assert "S1" in payload["status"]

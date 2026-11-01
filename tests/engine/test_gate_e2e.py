# -*- coding: utf-8 -*-
"""End-to-end: страж ведёт шаг от постановки до журнала (поддельный `hermes`)."""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from engine_kit import make_project


def sh(cmd: list[str], cwd: Path, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True,
                          encoding="utf-8", errors="replace",
                          env={**os.environ, **(env or {})})


def git(root: Path, *args: str) -> str:
    res = sh(["git", *args], root)
    assert res.returncode == 0, res.stderr
    return res.stdout.strip()


def cards(db: Path) -> list[dict]:
    con = sqlite3.connect(db)
    try:
        rows = con.execute(
            "SELECT id, title, status, assignee, idempotency_key, max_runtime FROM tasks"
            " ORDER BY created_at").fetchall()
    finally:
        con.close()
    return [{"id": r[0], "title": r[1], "status": r[2], "assignee": r[3], "key": r[4],
             "max_runtime": r[5]} for r in rows]


def complete(db: Path, suffix: str, status: str = "done", result: str = "") -> None:
    con = sqlite3.connect(db)
    try:
        con.execute("UPDATE tasks SET status=?, completed_at=strftime('%s','now'), result=?"
                    " WHERE idempotency_key LIKE ?", (status, result, "%" + suffix))
        con.commit()
    finally:
        con.close()


def gate(root: Path, *args: str) -> subprocess.CompletedProcess:
    return sh([sys.executable, str(root / ".pipeline" / "gate.py"), *args], root)


@pytest.fixture()
def e2e(tmp_path: Path, hermes_stub: Path) -> Path:
    root = make_project(tmp_path / "e2e", git={"commit": True, "push": False})
    git(root, "init", "-b", "main")
    git(root, "config", "user.email", "test@example.com")
    git(root, "config", "user.name", "Test")
    git(root, "add", "-A")
    git(root, "commit", "-m", "init")
    return root


def test_full_step_cycle(e2e: Path, hermes_stub: Path):
    root, db = e2e, hermes_stub

    # 1. Шага нет, постановки нет → заказ промптеру + инцидентный стоп.
    res = gate(root, "--run")
    assert res.returncode == 0, res.stderr
    cards_now = cards(db)
    assert len(cards_now) == 1
    assert cards_now[0]["assignee"] == "prompter"
    assert cards_now[0]["key"] == "planloop:1:S1:prompt"
    assert cards_now[0]["max_runtime"] == "45m"
    stop = json.loads((root / ".pipeline" / "state" / "plan_1_stop.json").read_text(encoding="utf-8"))
    assert stop["kind"] == "incident"
    assert "постановки" in stop["reason"]

    # 2. Постановка появилась → инцидент снимается, создаётся карточка шага.
    (root / "docs" / "prompts" / "2026-11-01_1-S1_prompt.md").write_text(
        "# Промт S1\n\n## 9. Ревью (обязательно)\n\nроль `reviewer`, вердикт APPROVE.\n",
        encoding="utf-8")
    res = gate(root, "--run")
    assert res.returncode == 0, res.stderr
    keys = {c["key"] for c in cards(db)}
    assert "planloop:1:S1:step" in keys
    assert "planloop:1:S1:prompt" in keys
    assert not (root / ".pipeline" / "state" / "plan_1_stop.json").exists()
    step_card = next(c for c in cards(db) if c["key"] == "planloop:1:S1:step")
    assert step_card["assignee"] == "demo"        # profiles.driver
    assert step_card["max_runtime"] == "3h"

    # 3. Шаг выполнен → появляется карточка ревью.
    complete(db, ":step")
    res = gate(root, "--run")
    assert res.returncode == 0, res.stderr
    review_card = next(c for c in cards(db) if c["key"] == "planloop:1:S1:review:1")
    assert review_card["assignee"] == "reviewer"
    assert review_card["max_runtime"] == "150m"

    # 4. Ревьюер вынес APPROVE со ссылкой на реальный коммит.
    sha = git(root, "rev-parse", "HEAD")
    (root / "docs" / "reviews" / "1-S1.md").write_text(
        "## ВЕРДИКТ: APPROVE\n\nОбъект ревью: коммит `%s`\n\nГейты: 3 passed\n" % sha,
        encoding="utf-8")
    complete(db, ":review:1")
    res = gate(root, "--run")
    assert res.returncode == 0, res.stderr

    # 5. Журнал закрыт стражем, коммит сделан только по своим файлам.
    plan = (root / "docs" / "plans" / "1-demo.md").read_text(encoding="utf-8")
    assert "| ЗАКРЫТ |" in plan
    assert "S1 (Таблица счетов)" in plan
    log = git(root, "log", "-1", "--pretty=%s")
    assert "закрыт шаг S1" in log
    assert git(root, "status", "--porcelain").strip() != ""   # постановка не закоммичена стражем

    # 6. Следующий тик берёт следующий шаг и снова ждёт постановку.
    res = gate(root, "--run")
    assert res.returncode == 0, res.stderr
    keys = {c["key"] for c in cards(db)}
    assert "planloop:1:S2:prompt" in keys


def test_status_is_read_only(e2e: Path, hermes_stub: Path):
    before = cards(hermes_stub) if hermes_stub.exists() else []
    res = gate(e2e, "--status")
    assert res.returncode == 0, res.stderr
    assert "Решение:" in res.stdout
    after = cards(hermes_stub) if hermes_stub.exists() else []
    assert before == after


def test_plans_table(e2e: Path):
    res = gate(e2e, "--plans")
    assert res.returncode == 0, res.stderr
    assert "1" in res.stdout
    assert "ПЛАН" in res.stdout


def test_owner_stop_blocks_and_resume(e2e: Path, hermes_stub: Path):
    assert gate(e2e, "--stop", "хватит").returncode == 0
    res = gate(e2e, "--run")
    assert res.returncode == 0
    assert not hermes_stub.exists() or cards(hermes_stub) == []
    res = gate(e2e, "--status")
    assert "стоп-флаг владельца" in res.stdout
    assert gate(e2e, "--resume").returncode == 0
    assert not (e2e / ".pipeline" / "state" / "plan_1_stop.json").exists()


def test_bad_commit_sha_stops(e2e: Path, hermes_stub: Path):
    # доходим до стадии ревью
    (e2e / "docs" / "prompts" / "2026-11-01_1-S1_prompt.md").write_text("# p\n", encoding="utf-8")
    gate(e2e, "--run")
    complete(hermes_stub, ":step")
    gate(e2e, "--run")
    (e2e / "docs" / "reviews" / "1-S1.md").write_text(
        "## ВЕРДИКТ: APPROVE\n\nОбъект ревью: коммит `deadbeef`\n", encoding="utf-8")
    complete(hermes_stub, ":review:1")
    res = gate(e2e, "--run")
    assert res.returncode == 0
    stop = json.loads((e2e / ".pipeline" / "state" / "plan_1_stop.json").read_text(encoding="utf-8"))
    assert "несуществующий коммит" in stop["reason"]

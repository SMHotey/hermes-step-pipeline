# -*- coding: utf-8 -*-
"""Комментарий с находками ревьюера в карточке правок (§S2 плана 2).

Зачем файл: находки ревьюера должны быть доступны там, где идёт работа, — в карточке
правок, а не только в файле отчёта. Проверяется отбор блокирующих находок, текст
комментария, сквозной путь через поддельный `hermes` и то, что отказ `kanban comment` —
предупреждение: он не меняет решение стража и не роняет тик.
"""
from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

import config as config_mod
import gate
from engine_kit import cards, make_project
from stub_hermes import SCHEMA

STEP = {"id": "S1", "epoch": "", "title": "Таблица счетов", "days": 1.0, "owner": "",
        "goal": "g", "files": [], "gates": [], "dod": "d", "read": [], "notes": ""}

REPORT_REL = "docs/reviews/1-S1.md"

# Отчёт в стиле `docs/reviews/3-S3.md`: подраздел «Блокирующие» и значимые пункты ниже.
FINDINGS = """\
## ВЕРДИКТ: REQUEST_CHANGES

Объект ревью: коммит `a1b2c3d`

### Блокирующие

1. **[блокирующая]** `step_pipeline/engine/gate.py:1634` — комментарий не отправляется,
   ожидается предупреждение
2. **[блокирующая]** `tests/engine/test_comments.py:12` — тест не утверждает значение

### Значимые

- [значимая] `gate.py:797` — выжимка теряет продолжения строк
- [стилистическая] `gate.py:900` — длинная строка
"""


def sh(cmd: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True,
                          encoding="utf-8", errors="replace", env={**os.environ})


def git(root: Path, *args: str) -> str:
    res = sh(["git", *args], root)
    assert res.returncode == 0, res.stderr
    return res.stdout.strip()


def gate_run(root: Path, *args: str) -> subprocess.CompletedProcess:
    """Тик стража отдельным процессом — так его запускает cron."""
    return sh([sys.executable, str(root / ".pipeline" / "gate.py"), *args], root)


def insert_card(db: Path, card_id: str, key: str, status: str = "done",
                created_at: int = 0) -> None:
    """Карточка в поддельной борде (колонки — stub_hermes.SCHEMA)."""
    con = sqlite3.connect(db)
    try:
        con.executescript(SCHEMA)
        con.execute(
            "INSERT OR REPLACE INTO tasks (id, title, status, assignee, idempotency_key,"
            " completed_at, result, last_failure_error, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (card_id, "карточка", status, "demo", key, created_at, "", "", created_at))
        con.commit()
    finally:
        con.close()


def comments(db: Path) -> list[dict]:
    """Комментарии поддельной борды в порядке записи."""
    if not db.is_file():
        return []
    con = sqlite3.connect(db)
    try:
        rows = con.execute("SELECT task_id, body, author FROM comments"
                           " ORDER BY created_at").fetchall()
    finally:
        con.close()
    return [{"task_id": r[0], "body": r[1], "author": r[2]} for r in rows]


def review_changes(root: Path, db: Path, findings_body: str = FINDINGS) -> None:
    """Состояние «ревью вернуло REQUEST_CHANGES»: постановка, две карточки и отчёт."""
    (root / "docs" / "prompts" / "2026-11-03_1-S1_prompt.md").write_text(
        "# Промт S1\n", encoding="utf-8")
    insert_card(db, "t_step_s1", "planloop:1:S1:step", created_at=1)
    insert_card(db, "t_rev_s1", "planloop:1:S1:review:1", created_at=2)
    (root / REPORT_REL).write_text(findings_body, encoding="utf-8")


@pytest.fixture()
def env(project: Path, monkeypatch) -> Path:
    """Окружение временного проекта: переменные плагина не должны утекать в движок."""
    for name in ("PIPELINE_CONFIG", "PIPELINE_PLANS", "PIPELINE_STATE_DIR", "PIPELINE_BOARD"):
        monkeypatch.delenv(name, raising=False)
    # collector() запускает next_step.py, а тот берёт корень из PIPELINE_REPO.
    monkeypatch.setenv("PIPELINE_REPO", str(project))
    return project


@pytest.fixture()
def e2e(tmp_path: Path, hermes_stub: Path) -> Path:
    """Проект под git: страж коммитит файл плана и отчёт, а не только карточки."""
    root = make_project(tmp_path / "e2e", git={"commit": True, "push": False})
    git(root, "init", "-b", "main")
    git(root, "config", "user.email", "test@example.com")
    git(root, "config", "user.name", "Test")
    git(root, "add", "-A")
    git(root, "commit", "-m", "init")
    return root


def test_blocking_findings_keep_addresses_and_classes(project: Path):
    """Отбор: метка в пункте и подраздел «Блокирующие» — адрес и класс остаются в тексте."""
    report = project / REPORT_REL
    report.write_text(FINDINGS, encoding="utf-8")

    found = gate._blocking_findings(report)
    assert len(found) == 2
    assert "gate.py:1634" in found[0]
    assert "test_comments.py:12" in found[1]
    assert not any("значимая" in f or "стилистическая" in f for f in found)
    assert all("`" not in f and "**" not in f for f in found)
    assert all(len(f) <= 180 for f in found)

    # Отчёт без подразделов: блокирующая только в тексте пункта.
    report.write_text("## ВЕРДИКТ: REQUEST_CHANGES\n\n"
                      "- [блокирующая] `gate.py:1647` — находка\n", encoding="utf-8")
    found = gate._blocking_findings(report)
    assert len(found) == 1
    assert "gate.py:1647" in found[0]


def test_comment_body_names_findings_report_and_review_card(project: Path):
    """Тело комментария: шаг, отчёт, карточка ревью, адреса находок и инструкция."""
    cfg = config_mod.load(project)
    p = gate.resolve_paths(cfg, "1")
    (project / REPORT_REL).write_text(FINDINGS, encoding="utf-8")

    body = gate.fix_comment_body(p, STEP, 1, REPORT_REL, "t_rev_s1")
    assert REPORT_REL in body
    assert "t_rev_s1" in body
    assert "круг 1" in body
    assert "S1" in body
    assert "gate.py:1634" in body
    assert "test_comments.py:12" in body
    assert "Блокирующие находки" in body
    assert "Ответ на находку — комментарий" in body
    assert len(body) <= gate.COMMENT_MAX_LEN

    # Блокирующих нет — в комментарий идут замечания из отчёта, без громкой формулировки.
    (project / REPORT_REL).write_text(
        "## ВЕРДИКТ: REQUEST_CHANGES\n\n"
        "- [стилистическая] `gate.py:900` — длинная строка\n", encoding="utf-8")
    body = gate.fix_comment_body(p, STEP, 1, REPORT_REL, "t_rev_s1")
    assert "Блокирующих находок с пометкой в отчёте нет" in body
    assert "gate.py:900" in body
    assert "Блокирующие находки" not in body


def test_fix_card_gets_comment_e2e(e2e: Path, hermes_stub: Path):
    """Сквозной путь: карточка правок создана, находки легли в неё комментарием."""
    root, db = e2e, hermes_stub
    review_changes(root, db)

    res = gate_run(root, "--run")
    assert res.returncode == 0, res.stderr

    fix = next(c for c in cards(db) if c["key"] == "planloop:1:S1:fix:1")
    rows = comments(db)
    assert len(rows) == 1
    assert rows[0]["task_id"] == fix["id"]
    assert rows[0]["author"] == gate.COMMENT_AUTHOR
    for expected in ("gate.py:1634", "test_comments.py:12", REPORT_REL, "t_rev_s1"):
        assert expected in rows[0]["body"]


def test_comment_refusal_keeps_tick_alive(project: Path, hermes_stub: Path, env: Path,
                                          monkeypatch, capsys):
    """Отказ `kanban comment` — предупреждение: решение и тик не меняются."""
    monkeypatch.setenv("PIPELINE_STUB_COMMENT_RC", "2")
    review_changes(project, hermes_stub)
    cfg = config_mod.load(project)

    res = gate.run_gate(gate.resolve_paths(cfg, "1"))
    err = capsys.readouterr().err
    assert res["decision"]["action"] == "create_fix"
    assert res["applied"] is True
    assert not (project / ".pipeline" / "state" / "plan_1_stop.json").exists()
    assert "[comment]" in err
    assert any(c["key"] == "planloop:1:S1:fix:1" for c in cards(hermes_stub))


def test_comment_call_failure_is_warning(project: Path, env: Path, monkeypatch, capsys):
    """Юнит на вызов: нет `hermes` в PATH — False и предупреждение, пустой id — без CLI."""
    p = gate.resolve_paths(config_mod.load(project), "1")
    real_run = gate._run
    calls: list[list[str]] = []

    def fake_run(cmd, *args, **kwargs):
        calls.append(list(cmd))
        if "comment" in cmd:
            raise OSError("hermes не найден")
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(gate, "_run", fake_run)
    assert gate._comment(p, "t_fix_1", "текст") is False
    err = capsys.readouterr().err
    assert "[comment]" in err
    assert "t_fix_1" in err

    before = len(calls)
    assert gate._comment(p, "", "текст") is False
    assert len(calls) == before          # пустой id — CLI не зовём вовсе

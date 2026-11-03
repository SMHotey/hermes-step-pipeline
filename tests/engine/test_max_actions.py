# -*- coding: utf-8 -*-
"""Предел действий за тик (S14): несколько действий, флаг поверх конфига, повтор шага.

Проверяется поведение, а не наличие ключей: при `max_actions=3` цепочка «ревью APPROVE →
журнал → следующий шаг → заказ постановки» проезжается одним тиком, флаг `--max-actions`
перекрывает конфиг, а шаг за тик получает не больше одного действия (D5).
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import config as config_mod
from engine_kit import cards, make_project, write_approval
from stub_hermes import SCHEMA

import gate


def sh(cmd: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True,
                          encoding="utf-8", errors="replace",
                          env={**os.environ})


def git(root: Path, *args: str) -> str:
    res = sh(["git", *args], root)
    assert res.returncode == 0, res.stderr
    return res.stdout.strip()


def gate_run(root: Path, *args: str) -> subprocess.CompletedProcess:
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


def board(db: Path) -> list[dict]:
    con = sqlite3.connect(db)
    try:
        rows = con.execute("SELECT id, status, idempotency_key FROM tasks"
                           " ORDER BY created_at").fetchall()
    finally:
        con.close()
    return [{"id": r[0], "status": r[1], "key": r[2]} for r in rows]


def plan_state(root: Path) -> dict:
    path = root / ".pipeline" / "state" / "plan_1_state.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}


def init_git(root: Path) -> None:
    git(root, "init", "-b", "main")
    git(root, "config", "user.email", "test@example.com")
    git(root, "config", "user.name", "Test")
    git(root, "add", "-A")
    git(root, "commit", "-m", "init")


def approved_project(tmp_path: Path, db: Path, max_actions: int = 3) -> Path:
    """Проект, где шаг S1 уже принят ревью: карточки закрыты, отчёт APPROVE с живым коммитом."""
    root = make_project(tmp_path / "max-actions", git={"commit": True, "push": False},
                        max_actions=max_actions)
    init_git(root)
    (root / "docs" / "prompts" / "2026-11-01_1-S1_prompt.md").write_text(
        "# Промт S1\n", encoding="utf-8")
    insert_card(db, "t_step_s1", "planloop:1:S1:step", created_at=1)
    insert_card(db, "t_rev_s1", "planloop:1:S1:review:1", created_at=2)
    sha = git(root, "rev-parse", "HEAD")
    (root / "docs" / "reviews" / "1-S1.md").write_text(
        f"## ВЕРДИКТ: APPROVE\n\nОбъект ревью: коммит `{sha}`\n", encoding="utf-8")
    return root


FAST_STUB = '''# -*- coding: utf-8 -*-
"""Поддельный `hermes` этого теста: карточка создаётся сразу закрытой.

Идемпотентность настоящей борды не воспроизводится намеренно: только так видно,
что тик не ставит второе действие по тому же шагу (D5).
"""
import contextlib
import io
import os
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(r"__STUB__"))
import stub_hermes

buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    code = stub_hermes.main()
card = buf.getvalue().strip().splitlines()[-1] if buf.getvalue().strip() else ""
if card:
    con = sqlite3.connect(os.environ["PIPELINE_KANBAN_DB"])
    try:
        con.execute("UPDATE tasks SET status='done', completed_at=? WHERE id=?",
                    (int(time.time()), card))
        con.commit()
    finally:
        con.close()
print(card)
raise SystemExit(code)
'''


@pytest.fixture()
def fast_stub(tmp_path: Path, monkeypatch) -> Path:
    """`hermes` в PATH, который пишет карточку сразу в статусе done (быстрый воркер)."""
    bin_dir = tmp_path / "fastbin"
    bin_dir.mkdir()
    stub_path = Path(__file__).with_name("stub_hermes.py")
    script = tmp_path / "fast_hermes.py"
    script.write_text(FAST_STUB.replace("__STUB__", str(stub_path).replace("\\", "/")),
                      encoding="utf-8")
    if os.name == "nt":
        wrapper = bin_dir / "hermes.cmd"
        wrapper.write_text(f'@"{sys.executable}" "{script}" %*\n', encoding="utf-8")
    else:
        wrapper = bin_dir / "hermes"
        wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n')
        wrapper.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ.get("PATH", ""))
    db = tmp_path / "fast-kanban.db"
    monkeypatch.setenv("PIPELINE_KANBAN_DB", str(db))
    return db


def test_chain_of_three_actions_in_one_tick(tmp_path: Path, hermes_stub: Path) -> None:
    """max_actions=3: закрытие шага и заказ постановки следующего — в одном тике."""
    root = approved_project(tmp_path, hermes_stub, max_actions=3)

    res = gate_run(root, "--run")
    assert res.returncode == 0, res.stderr

    plan_text = (root / "docs" / "plans" / "1-demo.md").read_text(encoding="utf-8")
    assert "| ЗАКРЫТ |" in plan_text                 # close_step внутри того же тика
    assert "planloop:1:S2:prompt" in {c["key"] for c in board(hermes_stub)}

    actions = plan_state(root).get("last_actions") or []
    names = [a["action"] for a in actions]
    assert len(actions) >= 2, names
    assert names[0] == "close_step", names
    assert "create_step" in names, names
    assert actions[0]["step"] == "S1" and actions[1]["step"] == "S2"
    assert {"at", "plan", "action", "step", "detail"} <= set(actions[0])


def test_flag_overrides_config_limit(tmp_path: Path, hermes_stub: Path) -> None:
    """`--run --max-actions 1` при max_actions=3 в конфиге: ровно одно действие."""
    root = approved_project(tmp_path, hermes_stub, max_actions=3)

    res = gate_run(root, "--run", "--max-actions", "1")
    assert res.returncode == 0, res.stderr

    plan_text = (root / "docs" / "plans" / "1-demo.md").read_text(encoding="utf-8")
    assert "| ЗАКРЫТ |" in plan_text
    assert "planloop:1:S2:prompt" not in {c["key"] for c in board(hermes_stub)}
    actions = plan_state(root).get("last_actions") or []
    assert [a["action"] for a in actions] == ["close_step"]


def test_step_key_and_limit_reading(tmp_path: Path) -> None:
    """Юнит: ключ шага `1:S1`, idle без шага, повтор из набора тронутых — повтор."""
    assert gate.step_key("1", {"action": "create_step", "step": "s1",
                               "data": {"id": "s1"}}) == "1:S1"
    assert gate.step_key("1", {"action": "close_step", "step": "S2",
                               "data": {"id": "S2"}}) == "1:S2"
    assert gate.step_key("1", {"action": "idle", "reason": "нет работы"}) is None
    assert gate.step_key("1", {"action": "retry_pending"}) is None

    touched = {gate.step_key("1", {"action": "create_step", "step": "s1"})}
    again = gate.step_key("1", {"action": "create_review", "step": "s1",
                                "data": {"id": "s1"}})
    assert again in touched                       # тот же шаг — повтор
    assert gate.step_key("2", {"action": "create_review", "step": "s1"}) not in touched

    assert gate.max_actions_of(SimpleNamespace(state={"max_actions": 3})) == 3
    assert gate.max_actions_of(SimpleNamespace(state={"max_actions": "2"})) == 2
    assert gate.max_actions_of(SimpleNamespace(state={})) == gate.MAX_ACTIONS_DEFAULT
    assert gate.max_actions_of(SimpleNamespace(state={"max_actions": 0})) == 1
    assert gate.max_actions_of(SimpleNamespace(state={"max_actions": "мусор"})) == 1


def test_one_action_per_step_in_one_tick(tmp_path: Path, fast_stub: Path) -> None:
    """Быстрый воркер закрыл карточку между действиями — второго действия по шагу нет."""
    root = make_project(tmp_path / "fast", max_actions=3)
    (root / "docs" / "prompts" / "2026-11-01_1-S1_prompt.md").write_text(
        "# Промт S1\n", encoding="utf-8")

    res = gate_run(root, "--run", "--json")
    assert res.returncode == 0, res.stderr
    payload = json.loads(res.stdout)

    cards_now = board(fast_stub)
    assert [c["key"] for c in cards_now] == ["planloop:1:S1:step"]
    assert cards_now[0]["status"] == "done"       # воркер успел закрыть карточку
    assert payload["actions_count"] == 1
    assert "уже получал действие" in payload["reason"]
    assert [a["action"] for a in payload["actions"]] == ["create_step"]


RISK_PLAN = """# 1. Демо-план конвейера

Цель: проверить, что страж ведёт шаги.

## Шаги

### S1. Опасный шаг (1 дн)

- Файл: `app/danger.py`
- Тесты/гейты: `python -m pytest tests/test_danger.py -q` → 1 passed
- Риск: high
- Готово = опасный шаг сделан

### S2. Обычный шаг (1 дн)

- Файл: `app/plain.py`
- Тесты/гейты: `python -m pytest tests/test_plain.py -q` → 1 passed
- Готово = обычный шаг сделан

## Журнал выполнения

| Дата | Шаг | Коммиты | Прогоны и доказательства | Статус |
|---|---|---|---|---|
| — | S1 | — | — | открыт |
| — | S2 | — | — | открыт |
"""


def test_risk_hold_is_not_swallowed_by_multi_action_tick(project: Path, hermes_stub: Path,
                                                         monkeypatch) -> None:
    """`max_actions=2`: рискованный шаг первым действием уходит в hold, карточек ноль.

    Инвариант «одно действие на шаг за тик» не даёт второму действию обойти hold:
    `hold_risk` ставится первым и остаётся единственным действием тика.
    """
    monkeypatch.setenv("PIPELINE_REPO", str(project))
    plan = project / "docs" / "plans" / "1-demo.md"
    plan.write_text(RISK_PLAN, encoding="utf-8")
    write_approval(project, plan)          # план изменён: sha256 в записи утверждения
    cfg = config_mod.load(project)

    res = gate.tick(cfg, max_actions=2)

    actions = res["actions"]
    assert len(actions) == 1, actions
    assert actions[0]["action"] == "hold_risk", actions
    assert actions[0]["applied"] is True, actions
    assert actions[0]["step"] == "S1"
    keys = [c["key"] for c in cards(hermes_stub)] if hermes_stub.is_file() else []
    assert keys == []                          # ни карточки шага, ни заказа постановки
    assert gate.load_state(gate.resolve_paths(cfg, "1"))["hold_steps"] == ["S1"]
    assert res["reason"] == "во всех планах нет работы"

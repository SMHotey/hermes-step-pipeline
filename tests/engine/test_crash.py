# -*- coding: utf-8 -*-
"""Сбой тика не умирает молча: инцидентный стоп с причиной вместо трейсбека.

Неисправная база борды и битый файл состояния превращаются в `GateError`:
тик (`--run`) пишет стоп kind=incident и уведомляет владельцу, панель
(`--status --json`) получает `ok: false` с причиной, в stderr нет трейсбека.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

KIT = Path(__file__).resolve().parents[2]


def run_gate(root: Path, *args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    """Запустить `gate.py` проекта-приёмника как отдельный процесс."""
    return subprocess.run([sys.executable, str(root / ".pipeline" / "gate.py"), *args],
                          cwd=str(root), capture_output=True, text=True,
                          encoding="utf-8", errors="replace",
                          env={**os.environ, **(env or {})}, timeout=120)


def break_board() -> Path:
    """Положить в путь борды мусорные байты: sqlite на таком файле не читается."""
    db = Path(os.environ["PIPELINE_KANBAN_DB"])
    db.parent.mkdir(parents=True, exist_ok=True)
    db.write_bytes(b"\x00\x01 not a sqlite database \xff" * 64)
    return db


def stop_payload(root: Path) -> dict | None:
    """Стоп-флаг плана 1 или None, если его нет."""
    path = root / ".pipeline" / "state" / "plan_1_stop.json"
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def state_file(root: Path) -> Path:
    return root / ".pipeline" / "state" / "plan_1_state.json"


def board_cards(db: Path) -> list[dict]:
    """Карточки борды-заглушки; базы или таблицы нет — карточек нет."""
    if not db.is_file():
        return []
    con = sqlite3.connect(db)
    try:
        rows = con.execute("SELECT id, title, status FROM tasks ORDER BY created_at").fetchall()
    except sqlite3.OperationalError:      # файл есть, таблицы ещё нет
        return []
    finally:
        con.close()
    return [{"id": r[0], "title": r[1], "status": r[2]} for r in rows]


def test_broken_board_leaves_incident_stop_and_notifies(project: Path, hermes_stub: Path) -> None:
    break_board()
    res = run_gate(project, "--run")
    assert res.returncode == 1, res.stderr
    assert res.stdout.strip() == ""
    payload = stop_payload(project)
    assert payload is not None, res.stderr
    assert payload["kind"] == "incident"
    assert payload["source"] == "gate"
    assert "борда недоступна" in payload["reason"]
    notices = [line for line in res.stderr.splitlines() if "[notify]" in line]
    assert len(notices) == 1, res.stderr
    assert "борда недоступна" in notices[0]

    again = run_gate(project, "--run")
    assert again.returncode == 1
    kept = stop_payload(project)
    assert kept is not None and kept["reason"] == payload["reason"]
    assert "[notify]" not in again.stderr

    # Стоп владельца инцидент не затирает: его снимает только владелец, а причина
    # сбоя остаётся видимой (stderr для панели и уведомление).
    stop_file = project / ".pipeline" / "state" / "plan_1_stop.json"
    stop_file.parent.mkdir(parents=True, exist_ok=True)
    stop_file.write_text(json.dumps({"reason": "остановка владельца", "source": "owner",
                                     "kind": "owner"}, ensure_ascii=False), encoding="utf-8")
    res = run_gate(project, "--run")
    assert res.returncode == 1
    owner = stop_payload(project)
    assert owner is not None and owner["kind"] == "owner", res.stderr
    assert owner["reason"] == "остановка владельца"
    notices = [line for line in res.stderr.splitlines() if "[notify]" in line]
    assert len(notices) == 1, res.stderr
    assert "борда недоступна" in notices[0]
    assert "ошибка: борда недоступна" in res.stderr

    # Сбой чтения/записи стоп-флага не отменяет уведомление.
    stop_file.unlink()
    stop_file.mkdir()
    broken = run_gate(project, "--run")
    assert broken.returncode == 1
    assert "Traceback (most recent call last)" not in broken.stderr
    broken_notices = [line for line in broken.stderr.splitlines() if "[notify]" in line]
    assert len(broken_notices) == 1, broken.stderr
    assert "файл стоп-флага не читается" in broken_notices[0]
    assert "ошибка: файл стоп-флага не читается" in broken.stderr
    assert "стоп-флаг не записывается" in broken.stderr


def test_broken_board_prints_no_traceback(project: Path, hermes_stub: Path) -> None:
    break_board()
    res = run_gate(project, "--run")
    assert res.returncode == 1
    assert "Traceback (most recent call last)" not in res.stderr
    assert "ошибка: борда недоступна" in res.stderr
    assert "file is not a database" in res.stderr      # причина не потеряна
    frames = [line for line in res.stderr.splitlines() if line.startswith('  File "')]
    assert frames == [], res.stderr
    assert res.stdout.strip() == ""


def test_run_json_reports_ok_false(project: Path, hermes_stub: Path) -> None:
    break_board()
    res = run_gate(project, "--run", "--json")
    assert res.returncode == 1
    data = json.loads(res.stdout)
    assert data["ok"] is False
    assert "борда недоступна" in data["error"]
    assert data["applied"] is False
    assert data["decision"]["action"] == "incident"
    payload = stop_payload(project)
    assert payload is not None and payload["kind"] == "incident"


def test_status_json_and_panel_show_reason(project: Path, hermes_stub: Path) -> None:
    break_board()
    res = run_gate(project, "--status", "--json", "--plan", "1")
    assert res.returncode == 1
    data = json.loads(res.stdout)
    assert data["ok"] is False
    assert "борда недоступна" in data["error"]
    # чтение не меняет состояние: ни стопа, ни уведомления
    assert stop_payload(project) is None
    assert "[notify]" not in res.stderr

    sys.path.insert(0, str(KIT))
    from step_pipeline import snapshot as snap

    data_out = snap.snapshot(project)
    errors = data_out.get("errors") or []
    assert any("борда недоступна" in str(e) for e in errors), errors
    assert "Traceback" not in " ".join(str(e) for e in errors)
    assert len(str(errors[0])) < 300


def test_broken_state_file_is_incident_not_traceback(project: Path, hermes_stub: Path) -> None:
    # Борда читается, но пуста: колонки те же, что в запросе `read_cards`, карточек нет.
    con = sqlite3.connect(hermes_stub)
    try:
        con.execute("CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, title TEXT,"
                    " status TEXT, assignee TEXT, idempotency_key TEXT, completed_at INTEGER,"
                    " result TEXT, last_failure_error TEXT, created_at INTEGER)")
        con.commit()
    finally:
        con.close()
    assert board_cards(hermes_stub) == []          # до прогона карточек нет
    state_file(project).parent.mkdir(parents=True, exist_ok=True)
    state_file(project).write_text('{"hold_steps": ["S1"]', encoding="utf-8")
    res = run_gate(project, "--run", "--plan", "1")
    assert res.returncode == 1, res.stderr
    assert "Traceback (most recent call last)" not in res.stderr
    payload = stop_payload(project)
    assert payload is not None, res.stderr
    assert payload["kind"] == "incident"
    assert "файл состояния не читается" in payload["reason"]
    # ручной допуск владельца не обойдён: действие не состоялось
    assert board_cards(hermes_stub) == []

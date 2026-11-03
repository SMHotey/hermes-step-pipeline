# -*- coding: utf-8 -*-
"""История тиков и метрики движка (§S3 плана 2).

Зачем файл: «конвейер идёт» должно подтверждаться числами, а не ощущением. Каждое
применённое действие оставляет строку в `state/history.jsonl`, файл не растёт бесконечно,
а снимок проекта отдаёт посчитанные по истории метрики под ключом `metrics`. Проверяется
форма строки, ротация по числу строк и размеру, подсчёт метрик (включая битые строки) и
сквозной путь: четыре тика на демо-проекте → числа в снимке совпадают с журналом плана.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

import config as config_mod
import gate
from engine_kit import KIT, complete, make_project

HISTORY_KEYS = {"ts", "at", "plan", "step", "action", "duration_s", "detail", "commit"}


def sh(cmd: list[str], cwd: Path) -> subprocess.CompletedProcess:
    """Команда в каталоге проекта (движок зовёт `hermes` и `git` из PATH)."""
    return subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True,
                          encoding="utf-8", errors="replace", env={**os.environ})


def git(root: Path, *args: str) -> str:
    res = sh(["git", *args], root)
    assert res.returncode == 0, res.stderr
    return res.stdout.strip()


def gate_run(root: Path, *args: str) -> subprocess.CompletedProcess:
    """Тик стража отдельным процессом — так его запускает cron."""
    return sh([sys.executable, str(root / ".pipeline" / "gate.py"), *args], root)


def history_rows(cfg) -> list[dict]:
    """Разобранные строки истории; битых быть не должно — тест это утверждает."""
    path = gate.history_path(cfg)
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]


def prewrite(cfg, count: int, detail: str = "", start: float = 1000.0) -> Path:
    """Предзаписать историю напрямую: строки в том же формате, что пишет движок."""
    path = gate.history_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps({"ts": start + i, "at": "01.01.2026 00:00:00", "plan": "1",
                         "step": "S1", "action": "create_step", "duration_s": 0.0,
                         "detail": detail, "commit": ""}, ensure_ascii=False)
             for i in range(count)]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def write_history(cfg, rows: list[tuple[str, str, str, float]]) -> None:
    """Записать историю с заданными моментами: `(план, шаг, действие, ts)`."""
    path = gate.history_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps({"ts": ts, "at": "01.01.2026 00:00:00", "plan": plan_id,
                         "step": step, "action": action, "duration_s": 0.0,
                         "detail": "", "commit": ""}, ensure_ascii=False)
             for plan_id, step, action, ts in rows]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


@pytest.fixture()
def env(project: Path, monkeypatch) -> Path:
    """Временный проект: переменные плагина не должны утекать в движок."""
    for name in ("PIPELINE_CONFIG", "PIPELINE_PLANS", "PIPELINE_STATE_DIR", "PIPELINE_BOARD"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PIPELINE_REPO", str(project))
    return project


@pytest.fixture()
def e2e(tmp_path: Path, hermes_stub: Path) -> Path:
    """Проект под git: страж коммитит файл плана и отчёт, а не только карточки."""
    root = make_project(tmp_path / "history-e2e", git={"commit": True, "push": False})
    git(root, "init", "-b", "main")
    git(root, "config", "user.email", "test@example.com")
    git(root, "config", "user.name", "Test")
    git(root, "add", "-A")
    git(root, "commit", "-m", "init")
    return root


def test_remember_action_writes_history_line(env: Path):
    """Действие → строка истории с длительностью, коммитом и ts; состояние прежней формы."""
    cfg = config_mod.load(env)
    p = gate.resolve_paths(cfg, "1")
    before = time.time()

    gate.remember_action(p, "1", {"action": "create_step", "step": "S1"},
                         "карточка t_abc", duration=1.25, commit="a1b2c3d")

    path = gate.history_path(p.cfg)
    assert path.is_file()
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert HISTORY_KEYS <= set(row)
    assert row["plan"] == "1"
    assert row["step"] == "S1"
    assert row["action"] == "create_step"
    assert row["duration_s"] == 1.25
    assert row["commit"] == "a1b2c3d"
    assert row["detail"] == "карточка t_abc"
    assert row["at"]
    assert isinstance(row["ts"], float)
    assert before - 1 <= row["ts"] <= time.time() + 1
    assert gate.load_state(p)["last_actions"][-1]["action"] == "create_step"

    # Старые вызовы (без длительности и коммита) продолжают работать: нули, не пропуск.
    gate.remember_action(p, "1", {"action": "create_review", "step": "S1"}, "карточка t_rev")
    rows = history_rows(p.cfg)
    assert len(rows) == 2
    assert rows[-1]["duration_s"] == 0.0
    assert rows[-1]["commit"] == ""
    assert rows[-1]["detail"] == "карточка t_rev"


def test_history_rotates_by_lines_and_bytes(env: Path):
    """История не растёт бесконечно: пределы строк и размера, последняя строка целая."""
    cfg = config_mod.load(env)
    p = gate.resolve_paths(cfg, "1")

    # (а) предел по числу строк.
    prewrite(cfg, 2100)
    gate.remember_action(p, "1", {"action": "create_step", "step": "S2"}, "новое")
    lines = [line for line in gate.history_path(cfg).read_text(encoding="utf-8").splitlines()
             if line.strip()]
    assert len(lines) == gate.HISTORY_MAX_LINES
    rows = [json.loads(line) for line in lines]           # ни одной разрезанной строки
    assert rows[-1]["step"] == "S2" and rows[-1]["detail"] == "новое"

    # (б) предел по размеру: длинные строки, обрезка по строкам, а не по байтам.
    prewrite(cfg, 3000, detail="x" * 400)
    gate.remember_action(p, "1", {"action": "create_review", "step": "S2"}, "новое")
    path = gate.history_path(cfg)
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert path.stat().st_size <= gate.HISTORY_MAX_BYTES
    assert len(lines) <= gate.HISTORY_MAX_LINES
    rows = [json.loads(line) for line in lines]
    assert rows[-1]["action"] == "create_review"

    # (в) каталога состояния нет — remember_action создаёт его и файл.
    shutil.rmtree(Path(cfg.state_dir))
    gate.remember_action(p, "1", {"action": "create_step", "step": "S3"}, "после удаления")
    path = gate.history_path(cfg)
    assert path.is_file()
    assert len([line for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip()]) == 1


def test_metrics_actions_7d_by_kind(env: Path):
    """Окно 7 дней режет по `ts` и считает действия по видам; старые строки в окно не входят."""
    cfg = config_mod.load(env)
    now = time.time()
    write_history(cfg, [
        ("1", "S1", "create_step", now - 3600),
        ("1", "S2", "create_step", now - 3600),
        ("1", "S3", "create_step", now - 3600),
        ("1", "S1", "create_review", now - 86400),
        ("1", "S1", "create_step", now - 8 * 86400),
    ])

    m = gate.history_metrics(cfg, now=now)

    assert m["actions_7d"] == {"create_step": 3, "create_review": 1}
    assert m["actions_7d_total"] == 4
    assert m["window_days"] == 7
    assert m["history"]["lines"] == 5
    assert m["history"]["bad_lines"] == 0
    assert m["generated_at"]


def test_metrics_step_seconds_journal_and_counters(env: Path):
    """Время шага считается по истории, закрытые шаги — по журналу, правки и стопы — тоже."""
    cfg = config_mod.load(env)
    plan = env / "docs" / "plans" / "1-demo.md"
    plan.write_text(plan.read_text(encoding="utf-8").replace("| открыт |", "| ЗАКРЫТ |"),
                    encoding="utf-8")
    now = time.time()
    base = now - 3600
    write_history(cfg, [
        ("1", "S1", "create_step", base),
        ("1", "S1", "close_step", base + 600),
        ("1", "S2", "create_step", base),
        ("1", "S2", "close_step", base + 100),
        ("1", "S3", "create_step", base),
        ("1", "S3", "close_step", base + 200),
        ("1", "S1", "create_fix", base + 300),
        ("1", "S1", "create_fix", base + 400),
        ("1", "S2", "stop", base + 500),
    ])

    m = gate.history_metrics(cfg, now=now)
    metrics = m["plans"]["1"]

    assert metrics["steps_closed"] == 2                       # совпадает с журналом плана
    assert metrics["step_seconds"] == {"avg": 300, "median": 200, "measured": 3}
    assert metrics["fix_rounds"] == 2
    assert metrics["incidents"] == 1


def test_metrics_skip_bad_lines(env: Path):
    """Битая строка JSONL не ломает подсчёт: пропускается и считается в `bad_lines`."""
    cfg = config_mod.load(env)
    now = time.time()
    good = json.dumps({"ts": now, "at": "01.01.2026 00:00:00", "plan": "1", "step": "S1",
                       "action": "create_step", "duration_s": 0.0, "detail": "", "commit": ""},
                      ensure_ascii=False)
    path = gate.history_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join([good, "{,битая", '"строка"', '{"ts": 1}']) + "\n",
                    encoding="utf-8")

    m = gate.history_metrics(cfg, now=now)
    assert m["history"] == {"lines": 4, "bad_lines": 3, "size_bytes": path.stat().st_size}
    assert m["actions_7d"] == {"create_step": 1}

    # Файла нет вовсе: нули, а не падение; план в метриках есть и заполнен нулями.
    path.unlink()
    m = gate.history_metrics(cfg, now=now)
    assert m["actions_7d"] == {}
    assert m["actions_7d_total"] == 0
    assert m["history"] == {"lines": 0, "bad_lines": 0, "size_bytes": 0}
    assert m["plans"]["1"] == {
        "steps_closed": 0,
        "step_seconds": {"avg": 0, "median": 0, "measured": 0},
        "fix_rounds": 0,
        "incidents": 0,
    }


def test_metrics_in_snapshot_after_ticks(e2e: Path, hermes_stub: Path):
    """Четыре тика на демо-проекте → непустые `metrics` в снимке, совпадающие с журналом."""
    root, db = e2e, hermes_stub

    # 1. Шага нет, постановки нет → заказ постановки (внутри действия — инцидентный стоп).
    res = gate_run(root, "--run")
    assert res.returncode == 0, res.stderr

    # 2. Постановка появилась → карточка шага.
    (root / "docs" / "prompts" / "2026-11-01_1-S1_prompt.md").write_text(
        "# Промт S1\n", encoding="utf-8")
    res = gate_run(root, "--run")
    assert res.returncode == 0, res.stderr

    # 3. Шаг выполнен → карточка ревью.
    complete(db, ":step")
    res = gate_run(root, "--run")
    assert res.returncode == 0, res.stderr

    # 4. Ревьюер вынес APPROVE со ссылкой на реальный коммит → журнал закрыт.
    sha = git(root, "rev-parse", "HEAD")
    (root / "docs" / "reviews" / "1-S1.md").write_text(
        "## ВЕРДИКТ: APPROVE\n\nОбъект ревью: коммит `%s`\n\nГейты: 3 passed\n" % sha,
        encoding="utf-8")
    complete(db, ":review:1")
    res = gate_run(root, "--run")
    assert res.returncode == 0, res.stderr

    sys.path.insert(0, str(KIT))
    from step_pipeline import snapshot

    snap = snapshot.snapshot(root)
    assert "metrics" in snap
    m = snap["metrics"]
    assert m["actions_7d"] == {"create_step": 2, "create_review": 1, "close_step": 1}
    assert m["actions_7d_total"] == 4
    assert m["history"]["lines"] == 4
    assert m["history"]["bad_lines"] == 0
    metrics = m["plans"]["1"]
    assert metrics["steps_closed"] == 1
    plan_text = (root / "docs" / "plans" / "1-demo.md").read_text(encoding="utf-8")
    assert plan_text.count("| ЗАКРЫТ |") == 1                 # число совпадает с журналом
    assert metrics["step_seconds"]["measured"] == 1
    # Стоп первого тика — внутри действия `create_step`, отдельного `stop` в истории нет.
    assert metrics["incidents"] == 0

# -*- coding: utf-8 -*-
"""Сторож не переписывает конфиг, где бюджет прогонов — авто (план 17, S5).

Замер основания: `watchdog.fix_run_budget` поднимал `step_runs.budget` до полутора
максимальных объявленных таймаутов, и это было лечением находки 14-S3. С шага 17-S5 умолчание
переехало в движок: ключа в `pipeline.json` нет — бюджет считается по таймаутам шага
(`gate.runs_budget_limit`). Сторож, вписавший в файл фиксированное число, отменил бы это
умолчание — ровно та находка, ради которой ключ и перестал быть числом.

Вторая половина теста держит прежнее поведение: заданный владельцем бюджет сторож
по-прежнему поднимает.
"""
from __future__ import annotations

import json
from pathlib import Path

from step_pipeline import watchdog


def project_with(tmp_path: Path, step_runs: dict, timeout: int = 1800) -> Path:
    """Проект с планом, где у шага объявлен долгий прогон, и заданным `step_runs`."""
    root = tmp_path / "proj"
    plans = root / "docs" / "plans"
    plans.mkdir(parents=True)
    (plans / "1-demo.md").write_text(
        "# 1. Демо\n\n## Шаги\n\n### S1. Долгий прогон (1 дн)\n\n"
        f"- Прогон: `python -m pytest` → exit 0; таймаут {timeout} сек\n",
        encoding="utf-8")
    (root / "pipeline.json").write_text(
        json.dumps({"project": "demo", "step_runs": step_runs}, ensure_ascii=False),
        encoding="utf-8")
    return root


def read_cfg(root: Path) -> dict:
    return json.loads((root / "pipeline.json").read_text(encoding="utf-8"))


def test_fix_run_budget_skips_auto(tmp_path: Path):
    """Ключа `budget` нет (авто) — файл не трогается; заданное число — по-прежнему поднимается.

    Формула второго случая прежняя: `max(макс. таймаут + 600, 3600)`; git-шаги в проекте
    теста падают (репозитория нет), и сообщение говорит об этом — бюджет всё равно поднят.
    """
    auto = project_with(tmp_path / "auto", {"timeout": 240})
    before = (auto / "pipeline.json").read_bytes()
    assert watchdog.fix_run_budget(auto) == ""
    assert (auto / "pipeline.json").read_bytes() == before
    assert read_cfg(auto)["step_runs"] == {"timeout": 240}

    explicit = project_with(tmp_path / "explicit", {"timeout": 240, "budget": 600})
    message = watchdog.fix_run_budget(explicit)
    block = read_cfg(explicit)["step_runs"]
    assert block["budget"] == 3600          # max(1800 + 600, 3600)
    assert block["timeout"] == 240
    assert "600 -> 3600" in message

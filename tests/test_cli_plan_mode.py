# -*- coding: utf-8 -*-
"""Терминальный переключатель режима планировщика: `hermes pipeline plan-mode`.

Подкоманда читает и пишет состояние проекта через `step_pipeline/plan_mode.py` (шаг S1):
`status` печатает режим и путь к файлу, `on`/`off` меняют состояние и говорят, что именно
изменилось и что это действует со следующей сессии, `--json` отдаёт один объект, а
неизвестное действие даёт код возврата 2 и подсказку `usage`.

Обработчик зовётся напрямую (`cli.cmd_plan_mode`), а не через `cli.run`: `run` переключает
настоящие потоки через `proj.setup_stdio()`, и `capsys` становится бесполезен.
"""
from __future__ import annotations

import argparse
import json
from argparse import Namespace
from datetime import datetime
from pathlib import Path

import helpers  # noqa: F401  (загружает плагин и алиасы)
import cli
from step_pipeline import plan_mode


def test_status_defaults_to_on_and_shows_file(project: Path, capsys):
    """Без файла режим включён: status печатает on и путь, но файл не создаёт."""
    path = plan_mode.state_file(project)
    assert cli.cmd_plan_mode(Namespace(action="status", project=str(project),
                                       json=False)) == 0
    captured = capsys.readouterr()
    assert "Режим планировщика: on" in captured.out
    assert f"Файл состояния: {path}" in captured.out
    assert captured.err == ""
    assert path.exists() is False


def test_off_then_status_prints_off(project: Path, capsys):
    """После off тот же status печатает off — состояние читается из файла, а не из памяти."""
    path = plan_mode.state_file(project)
    assert cli.cmd_plan_mode(Namespace(action="off", project=str(project),
                                       json=False)) == 0
    capsys.readouterr()

    assert cli.cmd_plan_mode(Namespace(action="status", project=str(project),
                                       json=False)) == 0
    captured = capsys.readouterr()
    assert "Режим планировщика: off" in captured.out
    assert f"Файл состояния: {path}" in captured.out
    assert plan_mode.read_mode(project) is False


def test_off_reports_exact_change_and_next_session(project: Path, capsys):
    """off говорит, что было on и что режим действует со следующей сессии; файл получает дату."""
    path = plan_mode.state_file(project)
    assert cli.cmd_plan_mode(Namespace(action="off", project=str(project),
                                       json=False)) == 0
    captured = capsys.readouterr()
    assert ("Режим планировщика: off (было on) — действует со следующей сессии"
            in captured.out)

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["plan_mode"] is False
    assert datetime.fromisoformat(payload["changed_at"])


def test_on_after_off_restores_and_second_on_is_noop(project: Path, capsys):
    """Повторный on без изменения состояния файл не трогает — дата последнего переключения та же."""
    path = plan_mode.state_file(project)
    assert cli.cmd_plan_mode(Namespace(action="off", project=str(project),
                                       json=False)) == 0
    capsys.readouterr()

    assert cli.cmd_plan_mode(Namespace(action="on", project=str(project),
                                       json=False)) == 0
    captured = capsys.readouterr()
    assert ("Режим планировщика: on (было off) — действует со следующей сессии"
            in captured.out)
    assert plan_mode.read_mode(project) is True
    stamp = json.loads(path.read_text(encoding="utf-8"))["changed_at"]

    capsys.readouterr()
    assert cli.cmd_plan_mode(Namespace(action="on", project=str(project),
                                       json=False)) == 0
    captured = capsys.readouterr()
    assert "без изменений" in captured.out
    assert "уже включён" in captured.out
    assert json.loads(path.read_text(encoding="utf-8"))["changed_at"] == stamp


def test_json_returns_single_object_with_mode_and_file(project: Path, capsys):
    """--json печатает ровно один объект: у status поля mode и file, у off ещё changed."""
    path = plan_mode.state_file(project)
    assert cli.cmd_plan_mode(Namespace(action="status", project=str(project),
                                       json=True)) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"mode": "on", "file": str(path)}

    assert cli.cmd_plan_mode(Namespace(action="off", project=str(project),
                                       json=True)) == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["mode"] == "off"
    assert payload["file"] == str(path)
    assert payload["changed"] is True
    assert datetime.fromisoformat(payload["changed_at"])


def test_unknown_action_returns_2_with_usage(project: Path, capsys):
    """Неизвестное и пропущенное действие — код 2 и подсказка usage в stderr, файла нет."""
    code = cli.cmd_plan_mode(Namespace(action="toggle", project=str(project), json=False))
    assert code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "usage" in captured.err
    assert "plan-mode status|on|off" in captured.err

    assert cli.cmd_plan_mode(Namespace(action="", project=str(project), json=False)) == 2
    assert plan_mode.state_file(project).exists() is False


def test_parser_registers_plan_mode_subcommand():
    """Подкоманда видна обычному argparse: action, --project, --json и обработчик на месте."""
    parser = argparse.ArgumentParser(prog="hermes pipeline")
    cli.build_parser(parser)

    args = parser.parse_args(["plan-mode", "off", "--project", "X", "--json"])
    assert args.cmd == "plan-mode"
    assert args.action == "off"
    assert args.project == "X"
    assert args.json is True
    assert args.handler is cli.cmd_plan_mode

    bare = parser.parse_args(["plan-mode", "status"])
    assert bare.project == ""
    assert bare.json is False

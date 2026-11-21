# -*- coding: utf-8 -*-
"""Подкоманда `keeper`: CLI зовёт модуль движка проекта и больше ничего не делает.

Проверяется ровно вызов: какой модуль, с какими флагами, с каким каталогом пакета и
как проброшен код возврата. Сам сканер живёт в движке и проверяется своим файлом
`tests/engine/test_keeper_scan.py`; здесь subprocess не запускается.
"""
from __future__ import annotations

import argparse
from pathlib import Path
from types import SimpleNamespace

import helpers  # noqa: F401  (загружает плагин и алиасы)
import cli
import project as proj


def keeper_args(project: Path, *, scan: bool = True, json_out: bool = False,
                heal: bool = False, dry_run: bool = False) -> argparse.Namespace:
    return argparse.Namespace(project=str(project), scan=scan, json=json_out, heal=heal,
                              dry_run=dry_run)


def patch_engine(monkeypatch, code: int = 0, stdout: str = "", stderr: str = "") -> list[dict]:
    """Подмена `proj.run_engine`: запоминает вызов, ничего не запускает."""
    calls: list[dict] = []

    def fake_run_engine(project, name, *args, **kwargs):
        calls.append({"project": project, "name": name, "args": list(args), **kwargs})
        return SimpleNamespace(returncode=code, stdout=stdout, stderr=stderr)

    monkeypatch.setattr(proj, "run_engine", fake_run_engine)
    return calls


def test_keeper_scan_calls_engine_module(project: Path, monkeypatch, capsys) -> None:
    calls = patch_engine(monkeypatch, stdout='{"symptoms": []}')
    code = cli.cmd_keeper(keeper_args(project))
    assert code == 0
    assert len(calls) == 1
    assert calls[0]["name"] == "keeper_scan.py"
    assert "--scan" in calls[0]["args"]
    assert "--json" not in calls[0]["args"]
    assert '{"symptoms": []}' in capsys.readouterr().out


def test_keeper_json_flag_is_forwarded(project: Path, monkeypatch) -> None:
    calls = patch_engine(monkeypatch)
    cli.cmd_keeper(keeper_args(project, json_out=True))
    assert "--json" in calls[0]["args"]


def test_keeper_plugin_dir_is_package_dir(project: Path, monkeypatch) -> None:
    calls = patch_engine(monkeypatch)
    cli.cmd_keeper(keeper_args(project))
    args = calls[0]["args"]
    assert "--plugin-dir" in args
    assert args[args.index("--plugin-dir") + 1] == str(proj.PLUGIN_DIR)


def test_keeper_returns_engine_exit_code(project: Path, monkeypatch, capsys) -> None:
    patch_engine(monkeypatch, code=2, stderr="конфигурация не читается: нет pipeline.json")
    code = cli.cmd_keeper(keeper_args(project))
    captured = capsys.readouterr()
    assert code == 2
    assert "конфигурация не читается" in captured.err


def test_parser_has_keeper_subcommand() -> None:
    parser = argparse.ArgumentParser()
    cli.build_parser(parser)
    parsed = parser.parse_args(["keeper", "--scan", "--json", "--project", "."])
    assert parsed.cmd == "keeper"
    assert parsed.json is True
    assert parsed.scan is True
    assert parsed.handler is cli.cmd_keeper


def test_keeper_heal_flag_runs_heal_module(project: Path, monkeypatch) -> None:
    """`--heal` поднимает модуль починок: флаг движка, `--scan` туда не идёт (скан встроен)."""
    calls = patch_engine(monkeypatch)
    code = cli.cmd_keeper(keeper_args(project, heal=True))
    assert code == 0
    assert calls[0]["name"] == "keeper_heal.py"
    assert "--heal" in calls[0]["args"]
    assert "--scan" not in calls[0]["args"]


def test_keeper_without_heal_keeps_scanner(project: Path, monkeypatch) -> None:
    calls = patch_engine(monkeypatch)
    cli.cmd_keeper(keeper_args(project))
    assert calls[0]["name"] == "keeper_scan.py"
    assert "--heal" not in calls[0]["args"]


def test_parser_accepts_heal_flag() -> None:
    parser = argparse.ArgumentParser()
    cli.build_parser(parser)
    parsed = parser.parse_args(["keeper", "--heal", "--project", "."])
    assert parsed.heal is True
    assert parsed.dry_run is False
    assert parsed.handler is cli.cmd_keeper
    assert parser.parse_args(["keeper", "--project", "."]).heal is False


def test_keeper_dry_run_flag_is_forwarded(project: Path, monkeypatch) -> None:
    """`--dry-run` уходит в движок рядом с `--heal`: режим решает движок, не CLI."""
    calls = patch_engine(monkeypatch)
    cli.cmd_keeper(keeper_args(project, heal=True, dry_run=True))
    args = calls[0]["args"]
    assert calls[0]["name"] == "keeper_heal.py"
    assert args.count("--dry-run") == 1
    assert "--plugin-dir" in args


def test_keeper_returns_heal_exit_code(project: Path, monkeypatch, capsys) -> None:
    """Код возврата модуля починок пробрасывается как у сканера."""
    patch_engine(monkeypatch, code=2, stderr="конфигурация не читается")
    code = cli.cmd_keeper(keeper_args(project, heal=True))
    captured = capsys.readouterr()
    assert code == 2
    assert "конфигурация не читается" in captured.err

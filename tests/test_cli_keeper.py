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


def keeper_args(project: Path, *, scan: bool = True, json_out: bool = False) -> argparse.Namespace:
    return argparse.Namespace(project=str(project), scan=scan, json=json_out)


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

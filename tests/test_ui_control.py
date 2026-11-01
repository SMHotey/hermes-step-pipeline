# -*- coding: utf-8 -*-
"""Фоновый UI: метка процесса, проверка доступности, остановка."""
from __future__ import annotations

import json
import socket
import sys
from pathlib import Path

import helpers  # noqa: F401  (загружает плагин и алиасы)
import pytest
import ui_control


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_not_running_on_free_port():
    assert ui_control.is_up(free_port(), timeout=0.4) is False


def test_pid_path_inside_state_dir(project: Path):
    assert ui_control.pid_path(project) == project / ".pipeline" / "ui.json"


def test_missing_marker_gives_error(project: Path):
    result = ui_control.stop(project)
    assert result["ok"] is False
    assert "ui.json" in result["error"]


def test_stale_pid_marker_is_cleaned(project: Path):
    marker = ui_control.pid_path(project)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps({"pid": 999999, "port": 8770}), encoding="utf-8")
    result = ui_control.stop(project)
    assert result["ok"] is True
    assert not marker.exists()


def test_read_pid_handles_broken_json(project: Path):
    marker = ui_control.pid_path(project)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("{не json", encoding="utf-8")
    assert ui_control.read_pid(project) == {}


def test_cli_stop_without_marker(project: Path, capsys):
    import argparse
    import cli

    code = cli.cmd_ui(argparse.Namespace(project=str(project), port=0, host="127.0.0.1",
                                         no_open=True, detach=False, stop=True))
    assert code == 1
    assert "ui.json" in capsys.readouterr().err

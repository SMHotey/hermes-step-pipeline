# -*- coding: utf-8 -*-
"""Фоновый UI: метка процесса, проверка доступности, остановка."""
from __future__ import annotations

import json
import socket
import subprocess
import sys
from pathlib import Path

import helpers  # noqa: F401  (загружает плагин и алиасы)
import project as proj
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


class _NtOs:
    """Заглушка `os` с веткой Windows: тест `_alive` одинаков на ubuntu и windows."""

    name = "nt"


def fake_run(calls: list[dict], stdout: bytes):
    """Подмена `project.run`: запоминает вызов, отдаёт заданные байты."""

    def run(cmd, **kwargs):
        calls.append({"cmd": cmd, **kwargs})
        return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr=b"")

    return run


def test_alive_searches_pid_in_bytes(monkeypatch):
    """PID ищется в байтах вывода tasklist: декодирование не участвует."""
    calls: list[dict] = []
    monkeypatch.setattr(ui_control, "os", _NtOs)
    monkeypatch.setattr(proj, "run", fake_run(calls, "ИНФОРМАЦИЯ: задача 4321 живёт".encode("cp866")))
    assert ui_control._alive(4321) is True
    assert len(calls) == 1
    assert calls[0]["text"] is False


def test_alive_without_pid_in_cp866_output(monkeypatch):
    """Регресс на предупреждение: не-utf-8 байты не декодируются, а ищутся как байты."""
    calls: list[dict] = []
    monkeypatch.setattr(ui_control, "os", _NtOs)
    monkeypatch.setattr(proj, "run", fake_run(calls, "ИНФОРМАЦИЯ: нет задач".encode("cp866")))
    assert ui_control._alive(4321) is False
    assert calls[0]["text"] is False


def test_run_helper_decodes_with_replace():
    """`project.run` декодирует utf-8 с заменой: чужие байты не роняют вызов."""
    res = proj.run([sys.executable, "-c",
                    "import sys; sys.stdout.buffer.write('привет'.encode('cp1251'))"],
                   timeout=60)
    assert isinstance(res.stdout, str)
    assert "\ufffd" in res.stdout


def test_ui_control_goes_through_project_helper():
    """Стража: subprocess.run в ui_control нет — только общий хелпер; Popen остаётся."""
    text = Path(ui_control.__file__).read_text(encoding="utf-8")
    assert "subprocess.run(" not in text
    assert "subprocess.Popen(" in text

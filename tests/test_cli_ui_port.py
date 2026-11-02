# -*- coding: utf-8 -*-
"""Порт UI: из `pipeline.json` проекта, флаг `--port` перекрывает конфиг."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import helpers  # noqa: F401  (загружает плагин и алиасы)
import cli
import ui_control


def ui_args(project: Path, port: int = 0) -> argparse.Namespace:
    return argparse.Namespace(project=str(project), port=port, host="127.0.0.1",
                              no_open=True, detach=True, stop=False)


def patch_start(monkeypatch) -> list[dict]:
    """Подмена `ui_control.start`: запоминает аргументы, ничего не поднимает."""
    calls: list[dict] = []

    def fake_start(project, port, host="127.0.0.1", **kwargs):
        calls.append({"project": project, "port": port, "host": host, **kwargs})
        return {"ok": True, "url": f"http://{host}:{port}/", "pid": 42, "port": port}

    monkeypatch.setattr(ui_control, "start", fake_start)
    return calls


def write_ui_port(project: Path, value) -> None:
    path = project / "pipeline.json"
    cfg = json.loads(path.read_text(encoding="utf-8"))
    cfg["ui_port"] = value
    path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def test_port_from_project_config(project: Path, monkeypatch):
    calls = patch_start(monkeypatch)
    write_ui_port(project, 8791)
    assert cli.cmd_ui(ui_args(project)) == 0
    assert calls and calls[0]["port"] == 8791


def test_port_default_when_key_absent(project: Path, monkeypatch):
    calls = patch_start(monkeypatch)
    assert cli.cmd_ui(ui_args(project)) == 0
    assert calls and calls[0]["port"] == 8770


def test_port_flag_overrides_config(project: Path, monkeypatch):
    calls = patch_start(monkeypatch)
    write_ui_port(project, 8791)
    assert cli.cmd_ui(ui_args(project, port=8899)) == 0
    assert calls and calls[0]["port"] == 8899

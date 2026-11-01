# -*- coding: utf-8 -*-
"""Фикстуры плагина: проект, поддельный `hermes`, борда в sqlite."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent
for path in (str(TESTS), str(TESTS.parent)):
    if path not in sys.path:
        sys.path.insert(0, path)

import helpers  # noqa: E402  (загружает плагин как пакет и алиасы модулей)
from helpers import install_project  # noqa: E402


@pytest.fixture()
def project(tmp_path: Path) -> Path:
    return install_project(tmp_path / "project")


@pytest.fixture()
def hermes_stub(tmp_path: Path, monkeypatch) -> Path:
    """Поддельный `hermes` в PATH: карточки пишутся в sqlite, борда не трогается."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = TESTS / "stub_hermes.py"
    if os.name == "nt":
        wrapper = bin_dir / "hermes.cmd"
        wrapper.write_text(f'@"{sys.executable}" "{stub}" %*\n', encoding="utf-8")
    else:
        wrapper = bin_dir / "hermes"
        wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{stub}" "$@"\n')
        wrapper.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ.get("PATH", ""))
    db = tmp_path / "kanban.db"
    monkeypatch.setenv("PIPELINE_KANBAN_DB", str(db))
    return db

# -*- coding: utf-8 -*-
"""Фикстуры тестов движка: проект с копией движка и поддельный `hermes` в PATH."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from engine_kit import ENGINE, PLAN, cards, complete, make_project  # noqa: E402,F401


@pytest.fixture()
def project(tmp_path: Path) -> Path:
    return make_project(tmp_path / "proj")


@pytest.fixture()
def hermes_stub(tmp_path: Path, monkeypatch) -> Path:
    """Поддельный `hermes` в PATH: карточки пишутся в sqlite, борда не трогается."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = Path(__file__).resolve().parent / "stub_hermes.py"
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


def pytest_configure(config):
    """Движок обязан быть на месте: тесты зовут его напрямую и через subprocess."""
    assert ENGINE.is_dir(), f"нет каталога движка: {ENGINE}"

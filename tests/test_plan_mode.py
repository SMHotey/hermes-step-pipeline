# -*- coding: utf-8 -*-
"""Состояние режима планировщика: умолчание «включён», атомарная запись, битый файл.

Режим — состояние проекта (файл в `.pipeline/state/`), поэтому тесты гоняют его на
фикстуре `project` и не трогают ни профиль, ни `pipeline.json`.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import helpers  # noqa: F401  (загружает плагин и алиасы)
import pytest
from step_pipeline import plan_mode


def test_defaults_on_without_file(project: Path):
    """Отсутствие файла в существующем каталоге — режим включён, и чтение его не создаёт."""
    path = plan_mode.state_file(project)
    assert path.parts[-3:] == (".pipeline", "state", "plan_mode.json")
    path.parent.mkdir(parents=True, exist_ok=True)

    assert plan_mode.read_mode(project) is True
    assert path.exists() is False


def test_defaults_on_without_state_dir(project: Path):
    """Нет самого каталога состояния — тоже включён, каталог чтение не создаёт."""
    assert plan_mode.state_file(project).parent.exists() is False

    assert plan_mode.read_mode(project) is True
    assert plan_mode.state_file(project).parent.exists() is False


def test_write_creates_dir_and_roundtrips(project: Path):
    """Запись создаёт каталог, кладёт булево и дату, повторная запись перезаписывает."""
    path = plan_mode.state_file(project)
    plan_mode.write_mode(project, False)

    assert path.exists() is True
    assert plan_mode.read_mode(project) is False
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["plan_mode"] is False
    assert datetime.fromisoformat(payload["changed_at"])  # дата разбирается

    plan_mode.write_mode(project, True)
    assert plan_mode.read_mode(project) is True
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["plan_mode"] is True


def test_write_leaves_one_file_and_no_temp(project: Path):
    """Атомарная запись оставляет ровно целевой файл, временных не остаётся."""
    path = plan_mode.state_file(project)
    plan_mode.write_mode(project, False)
    plan_mode.write_mode(project, True)

    assert sorted(p.name for p in path.parent.iterdir()) == ["plan_mode.json"]
    assert list(path.parent.glob("*.tmp-*")) == []


def test_broken_json_reads_on_and_warns(project: Path, capsys):
    """Битый файл читается как «включён», предупреждение — ровно одна строка в stderr."""
    path = plan_mode.state_file(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{не json", encoding="utf-8")

    assert plan_mode.read_mode(project) is True

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "не читается" in captured.err
    assert "считаю включённым" in captured.err
    assert "plan_mode.json" in captured.err
    assert len(captured.err.strip().splitlines()) == 1


def test_failed_replace_keeps_old_file(project: Path, monkeypatch):
    """Сбой подмены файла не портит старое состояние и не оставляет временных."""
    path = plan_mode.state_file(project)
    plan_mode.write_mode(project, True)
    before = path.read_text(encoding="utf-8")

    def boom(src, dst):
        raise OSError("диск отвалился")

    monkeypatch.setattr(plan_mode.os, "replace", boom)

    with pytest.raises(OSError):
        plan_mode.write_mode(project, False)

    assert path.read_text(encoding="utf-8") == before
    assert list(path.parent.glob("*.tmp-*")) == []
    assert plan_mode.read_mode(project) is True

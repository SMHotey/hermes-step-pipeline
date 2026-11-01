# -*- coding: utf-8 -*-
"""Тик для cron: разбор .env профиля и поиск gate.py.

Причина тестов: cron шлюза отдаёт HERMES_HOME профиля, но не переменные из его .env,
поэтому без собственного разбора уведомления движка молчат (см. 1-S17).
"""
from __future__ import annotations

import importlib.util
import os
import shutil
import sys
from pathlib import Path

import pytest

from engine_kit import ENGINE

TICK = ENGINE / "tick.py"


def load_tick(path: Path):
    """Импортировать копию тика из указанного пути (как отдельный модуль)."""
    spec = importlib.util.spec_from_file_location(f"tick_{abs(hash(str(path)))}", str(path))
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def profile_script(tmp_path: Path) -> Path:
    """Копия тика в структуре `profiles/<профиль>/scripts/pipeline_tick.py`."""
    scripts = tmp_path / "profiles" / "demo" / "scripts"
    scripts.mkdir(parents=True)
    target = scripts / "pipeline_tick.py"
    shutil.copy2(TICK, target)
    return target


def test_env_from_profile_is_loaded(profile_script: Path, monkeypatch):
    (profile_script.parents[1] / ".env").write_text(
        "# комментарий\nSTEP_TEST_TOKEN=abc\n\nexport STEP_TEST_CHAT=\"42\"\n", encoding="utf-8")
    monkeypatch.delenv("STEP_TEST_TOKEN", raising=False)
    monkeypatch.delenv("STEP_TEST_CHAT", raising=False)

    load_tick(profile_script).load_profile_env()

    assert os.environ["STEP_TEST_TOKEN"] == "abc"
    assert os.environ["STEP_TEST_CHAT"] == "42"


def test_existing_values_are_not_overwritten(profile_script: Path, monkeypatch):
    (profile_script.parents[1] / ".env").write_text("STEP_TEST_TOKEN=from_file\n", encoding="utf-8")
    monkeypatch.setenv("STEP_TEST_TOKEN", "from_env")

    load_tick(profile_script).load_profile_env()

    assert os.environ["STEP_TEST_TOKEN"] == "from_env"


def test_pipeline_env_overrides_profile_file(profile_script: Path, tmp_path: Path, monkeypatch):
    (profile_script.parents[1] / ".env").write_text("STEP_TEST_TOKEN=profile\n", encoding="utf-8")
    custom = tmp_path / "custom.env"
    custom.write_text("STEP_TEST_TOKEN=custom\n", encoding="utf-8")
    monkeypatch.delenv("STEP_TEST_TOKEN", raising=False)
    monkeypatch.setenv("PIPELINE_ENV", str(custom))

    load_tick(profile_script).load_profile_env()

    assert os.environ["STEP_TEST_TOKEN"] == "custom"


def test_missing_env_is_not_an_error(profile_script: Path, monkeypatch):
    monkeypatch.delenv("STEP_TEST_TOKEN", raising=False)
    load_tick(profile_script).load_profile_env()  # файла нет — просто ничего не делаем
    assert "STEP_TEST_TOKEN" not in os.environ


def test_find_gate_prefers_pipeline_repo(tmp_path: Path, monkeypatch):
    repo = tmp_path / "repo"
    (repo / ".pipeline").mkdir(parents=True)
    gate_file = repo / ".pipeline" / "gate.py"
    gate_file.write_text("# заглушка", encoding="utf-8")
    monkeypatch.setenv("PIPELINE_REPO", str(repo))

    assert load_tick(TICK).find_gate() == gate_file


def test_find_gate_uses_cwd(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("PIPELINE_REPO", raising=False)
    (tmp_path / ".pipeline").mkdir()
    gate_file = tmp_path / ".pipeline" / "gate.py"
    gate_file.write_text("# заглушка", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    assert load_tick(TICK).find_gate() == gate_file


def test_main_without_gate_reports_error(tmp_path: Path, monkeypatch, capsys):
    """Копия тика в пустом каталоге: gate.py нет ни рядом, ни выше — внятная ошибка."""
    lonely = tmp_path / "bin" / "pipeline_tick.py"
    lonely.parent.mkdir()
    shutil.copy2(TICK, lonely)
    monkeypatch.delenv("PIPELINE_REPO", raising=False)
    monkeypatch.chdir(tmp_path)

    module = load_tick(lonely)
    module.load_profile_env = lambda: None  # профиля нет, .env не нужен
    code = module.main()

    assert code == 1
    assert "gate.py не найден" in capsys.readouterr().err

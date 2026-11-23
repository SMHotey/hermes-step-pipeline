"""Сторож живёт в плагине: список конвейеров берётся из реестра, а не из кода.

Владелец 23.11.2026: «скрипт должен жить не в doorsystem, а в плагине». Отсюда два свойства,
которые держим тестами: список проектов — из `projects.json` (один источник на всю установку),
и проект, который владелец погасил, сторож не трогает.
"""
from __future__ import annotations

import json
from pathlib import Path

from step_pipeline import watchdog as wd


def _project(root: Path, *, enabled: bool | None = None) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    cfg: dict = {"project": root.name}
    if enabled is not None:
        cfg["watchdog"] = {"enabled": enabled}
    (root / "pipeline.json").write_text(json.dumps(cfg), encoding="utf-8")
    return root


def _registry(tmp_path: Path, projects: list[dict]) -> Path:
    path = tmp_path / "projects.json"
    path.write_text(json.dumps({"projects": projects}), encoding="utf-8")
    return path


def test_projects_from_registry(tmp_path: Path, monkeypatch) -> None:
    first = _project(tmp_path / "alpha")
    second = _project(tmp_path / "beta")
    registry = _registry(tmp_path, [
        {"id": "alpha", "name": "Alpha", "root": str(first)},
        {"id": "beta", "root": str(second)},
        {"root": ""},
        "мусор",
    ])
    monkeypatch.setenv("PIPELINE_PROJECTS_FILE", str(registry))

    assert wd.projects() == [("Alpha", str(first)), ("beta", str(second))]


def test_projects_skip_disabled_by_owner(tmp_path: Path, monkeypatch) -> None:
    live = _project(tmp_path / "live")
    dark = _project(tmp_path / "dark", enabled=False)
    registry = _registry(tmp_path, [
        {"id": "live", "name": "живой", "root": str(live)},
        {"id": "dark", "name": "погашен", "root": str(dark)},
    ])
    monkeypatch.setenv("PIPELINE_PROJECTS_FILE", str(registry))

    assert [name for name, _root in wd.projects()] == ["живой"]


def test_projects_fallback_without_registry(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PIPELINE_PROJECTS_FILE", str(tmp_path / "нет-такого.json"))

    assert wd.projects() == wd.DEFAULT_PROJECTS


def test_report_paths_do_not_belong_to_profile() -> None:
    """Отчёт и лог — в домашнем каталоге Hermes, без `profiles/<имя>` в пути."""
    assert "profiles" not in wd.LOG.parts
    assert "profiles" not in wd.REPORT.parts
    assert wd.LOG.name == "pipeline_watchdog.log"
    assert wd.REPORT.name == "pipeline_status.md"


def test_root_home_collapses_profile(monkeypatch, tmp_path: Path) -> None:
    """В кроне `HERMES_HOME` — это профиль; отчёт всё равно уходит в корень установки."""
    root = tmp_path / "hermes"
    monkeypatch.setenv("HERMES_HOME", str(root / "profiles" / "pipeline"))

    assert wd._root_home() == root.resolve()


def test_root_home_keeps_plain_home(monkeypatch, tmp_path: Path) -> None:
    plain = tmp_path / "hermes-home"
    monkeypatch.setenv("HERMES_HOME", str(plain))

    assert wd._root_home() == plain

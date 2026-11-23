"""Паузы работы переживают установку и обновление движка (владелец 23.11.2026).

Требование: обновление движка плагина не сбрасывает паузы проекта к умолчаниям шаблона —
свои значения остаются как есть, а проект без окон получает ТЕКУЩИЕ значения владельца
из реестра `projects.json` (`pause` верхнего уровня).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from step_pipeline import project as proj
from step_pipeline import setup_flow as sf


OWN = {"enabled": True, "windows": [{"from": "23:00", "to": "06:30"}]}


def _registry(tmp_path: Path, payload: dict) -> Path:
    path = tmp_path / "projects.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def test_own_pause_survives_rebuild(tmp_path, monkeypatch):
    """Своё значение проекта не переписывается ни шаблоном, ни реестром."""
    monkeypatch.setenv("PIPELINE_PROJECTS_FILE",
                       str(_registry(tmp_path, {"pause": {"enabled": True,
                                                         "windows": [{"from": "04:00",
                                                                      "to": "07:00"}]}})))
    cfg = sf.build_config(tmp_path / "demo", "demo", "drv", "prm", "rev",
                          {"quiet_hours": OWN}, scout="sco")
    assert cfg["quiet_hours"] == OWN


def test_project_without_windows_inherits_owner_values(tmp_path, monkeypatch):
    """Проект без окон получает текущие значения владельца из реестра, а не шаблон."""
    monkeypatch.setenv("PIPELINE_PROJECTS_FILE",
                       str(_registry(tmp_path, {"pause": {"enabled": True, "windows": [
                           {"from": "04:00", "to": "07:00"},
                           {"from": "09:00", "to": "13:00"}]}})))
    cfg = sf.build_config(tmp_path / "demo", "demo", "drv", "prm", "rev",
                          {"quiet_hours": {"enabled": False, "windows": []}}, scout="sco")
    assert cfg["quiet_hours"] == {"enabled": True, "windows": [
        {"from": "04:00", "to": "07:00"}, {"from": "09:00", "to": "13:00"}]}


def test_without_registry_template_default_stays(tmp_path, monkeypatch):
    """Реестра с паузами нет — остаётся умолчание шаблона, ничего не выдумываем."""
    monkeypatch.setenv("PIPELINE_PROJECTS_FILE", str(_registry(tmp_path, {"projects": []})))
    cfg = sf.build_config(tmp_path / "demo", "demo", "drv", "prm", "rev",
                          {"quiet_hours": {"enabled": False, "windows": []}}, scout="sco")
    assert cfg["quiet_hours"] == {"enabled": False, "windows": []}


def test_registry_pause_normalises_and_rejects_garbage(tmp_path):
    """Кривые окна отбрасываются: мусор в реестре не должен превращаться в паузу."""
    path = _registry(tmp_path, {"pause": {"enabled": True, "windows": [
        {"from": "4:00", "to": "7:00"},
        {"from": "когда", "to": "07:00"},
        {"from": "12:00", "to": "12:00"}]}})
    assert proj.registry_pause(path) == {"enabled": True,
                                        "windows": [{"from": "4:00", "to": "7:00"}]}
    empty_dir = tmp_path / "nil"
    empty_dir.mkdir()
    empty = _registry(empty_dir, {"pause": {"enabled": True, "windows": []}})
    assert proj.registry_pause(empty) is None


def test_upgrade_fills_pause_from_registry(tmp_path, monkeypatch):
    """`upgrade` проекта без окон выставляет паузы владельца и не трогает свои."""
    monkeypatch.setenv("PIPELINE_PROJECTS_FILE",
                       str(_registry(tmp_path, {"pause": {"enabled": True, "windows": [
                           {"from": "04:00", "to": "07:00"}]}})))
    fresh = tmp_path / "fresh"
    _fake_installed(fresh, {"project": "fresh", "board": "fresh"})
    assert sf.upgrade(fresh, sf.Log(), trust=False) == 0
    assert proj.read_config(fresh)["quiet_hours"] == {
        "enabled": True, "windows": [{"from": "04:00", "to": "07:00"}]}

    kept = tmp_path / "kept"
    _fake_installed(kept, {"project": "kept", "board": "kept", "quiet_hours": OWN})
    assert sf.upgrade(kept, sf.Log(), trust=False) == 0
    assert proj.read_config(kept)["quiet_hours"] == OWN


def _fake_installed(root: Path, config: dict) -> None:
    """Минимальный «поставленный» проект: конфиг, `.pipeline/` и пустые роли."""
    (root / ".pipeline").mkdir(parents=True)
    (root / proj.MARKER).write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
    monkeypatch_roles(root)


def monkeypatch_roles(root: Path) -> None:
    """Свои роли не нужны: копируем ровно те, что плагин поставляет, но в пустой каталог."""
    dest = root / ".hermes" / "skills"
    dest.mkdir(parents=True, exist_ok=True)


@pytest.fixture(autouse=True)
def _no_cron(monkeypatch):
    """`upgrade` не должен ходить в hermes за cron-джобами в тесте."""
    monkeypatch.setattr(sf, "ensure_cron_all", lambda *a, **k: None)
    monkeypatch.setattr(sf, "ensure_scout_profile", lambda *a, **k: None)
    monkeypatch.setattr(sf, "trust_roles", lambda *a, **k: [])
    monkeypatch.setattr(sf, "ensure_plugin_enabled", lambda *a, **k: None)
    monkeypatch.setattr(sf, "quarantine_stale_plugins", lambda *a, **k: None)
    monkeypatch.setattr(sf, "ensure_plans_legacy", lambda *a, **k: None)

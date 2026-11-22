# -*- coding: utf-8 -*-
"""Один профиль — один проект: умолчания ролей и запрет дележа (CONTEXT §8, план 17).

Замер 22.11.2026: пока постановку DoorSystem писал общий профиль `prompter`, постановка
движка ждала своей очереди — `kanban.max_in_progress_per_profile: 1` держится на профиль,
а не на проект. Поэтому умолчание считается от имени проекта (`<проект>-<роль>`), а явно
переданный чужой профиль `setup` не принимает.

Тесты зовут функции напрямую: подпроцессы `hermes` и сеть здесь не нужны, реестр проектов
подменяется переменной `PIPELINE_PROJECTS_FILE`.
"""
from __future__ import annotations

import json
from pathlib import Path

from step_pipeline import project as proj
from step_pipeline import setup_flow


def _make_project(root: Path, name: str, profiles: dict[str, str]) -> Path:
    """Каталог проекта с `pipeline.json` — ровно столько, сколько нужно проверке дележа."""
    path = root / name
    path.mkdir(parents=True)
    proj.write_config(path, {"project": name, "board": proj.slug(name), "profiles": profiles})
    return path


def _registry(tmp_path: Path, entries: list[tuple[str, Path]], monkeypatch) -> None:
    """Реестр панели: те же записи, что читает `projects_file()`."""
    file = tmp_path / "projects.json"
    file.write_text(json.dumps({"projects": [
        {"id": proj.slug(name), "name": name, "root": str(root)} for name, root in entries
    ]}, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv("PIPELINE_PROJECTS_FILE", str(file))


def test_role_profile_default_is_project_scoped() -> None:
    """Умолчание — `<проект>-<роль>`, а не общее слово: иначе два проекта делят замок."""
    assert setup_flow.role_profile_default(Path("/p/DoorSystem"), "prompter") == \
        "doorsystem-prompter"
    assert setup_flow.role_profile_default(Path("/p/self-analyzer"), "reviewer") == \
        "self-analyzer-reviewer"
    assert setup_flow.role_profile_default(Path("/p/My Site"), "scout") == "my-site-scout"


def test_setup_defaults_derive_profiles_from_project_name(tmp_path: Path, monkeypatch) -> None:
    """Проект без конфига получает собственные имена ролей, а не `prompter`/`reviewer`."""
    monkeypatch.setenv("PIPELINE_PROJECTS_FILE", str(tmp_path / "нет.json"))
    project = tmp_path / "doorsystem"
    project.mkdir()
    defaults = setup_flow.setup_defaults(project)
    assert defaults["prompter"] == "doorsystem-prompter"
    assert defaults["reviewer"] == "doorsystem-reviewer"
    assert defaults["scout"] == "doorsystem-scout"


def test_conflict_found_when_two_projects_share_a_profile(tmp_path: Path, monkeypatch) -> None:
    """Один и тот же профиль постановщика у двух проектов — дележ, он и назван."""
    first = _make_project(tmp_path, "DoorSystem", {"prompter": "prompter"})
    second = _make_project(tmp_path, "self-analyzer", {"prompter": "prompter"})
    _registry(tmp_path, [("DoorSystem", first), ("self-analyzer", second)], monkeypatch)

    conflicts = setup_flow.role_profile_conflicts(second, {"prompter": "prompter"})

    assert conflicts == [("prompter", "prompter", "DoorSystem")]


def test_conflict_is_empty_when_every_project_has_its_own(tmp_path: Path, monkeypatch) -> None:
    """После разведения дележа нет: проверка молчит и не мешает раскатке."""
    first = _make_project(tmp_path, "DoorSystem", {"prompter": "doorsystem-prompter"})
    second = _make_project(tmp_path, "self-analyzer", {"prompter": "self-analyzer-prompter"})
    _registry(tmp_path, [("DoorSystem", first), ("self-analyzer", second)], monkeypatch)

    profiles = {"prompter": "self-analyzer-prompter", "reviewer": "self-analyzer-reviewer"}

    assert setup_flow.role_profile_conflicts(second, profiles) == []


def test_project_does_not_conflict_with_itself(tmp_path: Path, monkeypatch) -> None:
    """Свой профиль в своём же конфиге — не дележ: раскатка `--upgrade` не должна падать."""
    only = _make_project(tmp_path, "DoorSystem", {"prompter": "doorsystem-prompter"})
    _registry(tmp_path, [("DoorSystem", only)], monkeypatch)

    assert setup_flow.role_profile_conflicts(only, {"prompter": "doorsystem-prompter"}) == []

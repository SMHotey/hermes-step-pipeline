# -*- coding: utf-8 -*-
"""Гигиена репозитория: имя манифеста, мёртвые ключи, лицензия, .gitignore, README.

Манифест читается как текст, а не через `import yaml`: в CI (`.github/workflows/ci.yml`)
PyYAML не ставится, а инварианты шага — текстовые.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PLUGIN = REPO / "step_pipeline"
MANIFEST = PLUGIN / "plugin.yaml"
README = REPO / "README.md"
GITIGNORE = REPO / ".gitignore"
LICENSE = REPO / "LICENSE"

REPO_URL = "SMHotey/hermes-step-pipeline"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def manifest_name(text: str) -> str:
    match = re.search(r"(?m)^name:[ \t]*(\S+)", text)
    assert match, "в plugin.yaml нет строки name:"
    return match.group(1)


def test_manifest_name_matches_directory():
    """`plugins update`/`remove` резолвят каталог, `list`/`show` — имя манифеста."""
    name = manifest_name(read(MANIFEST))
    assert name == "step_pipeline"
    assert (REPO / name).is_dir(), f"каталога {name}/ рядом с манифестом нет"


def test_manifest_has_no_dead_config_keys():
    """`project_root` и `ui_port` не читает ни один модуль плагина."""
    text = read(MANIFEST)
    for key in ("project_root", "ui_port"):
        assert not re.search(rf"(?m)^[ \t]+{key}:", text), f"мёртвый ключ {key} в манифесте"
    assert "telegram_bot_token:" in text
    assert "telegram_chat_id:" in text


def test_manifest_license_has_file():
    assert re.search(r"(?m)^license:[ \t]*MIT[ \t]*$", read(MANIFEST))
    text = read(LICENSE)
    assert "MIT License" in text
    assert "Copyright" in text


def test_gitignore_covers_install_artifacts():
    lines = {line.strip() for line in read(GITIGNORE).splitlines()}
    for entry in (".pipeline/", ".hermes/", "docs/templates/", ".idea/"):
        assert entry in lines, f"в .gitignore нет строки {entry}"


def test_readme_uses_single_plugin_name():
    text = read(README)
    rest = text.replace(REPO_URL, "")
    assert "step-pipeline" not in rest, "в README осталось дефисное имя плагина"
    for command in ("plugins/step_pipeline", "plugins enable step_pipeline",
                    "plugins remove step_pipeline"):
        assert command in text, f"в README нет команды {command}"

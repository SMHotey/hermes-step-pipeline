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
# Модули, которые читают значения схемы: ключ манифеста обязан встречаться в коде
# (или в env-имени), иначе он мёртвый — панель его не увидит.
PLUGIN_SOURCES = tuple(PLUGIN / name for name in
                       ("project.py", "setup_flow.py", "doctor.py", "ui_server.py", "cli.py"))


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


def config_schema_keys(text: str) -> dict[str, str]:
    """Ключи `config_schema` и их `env`: разбор по отступам, без PyYAML (в CI его нет).

    Схема — плоский словарь ключей на отступе 2; у ключа бывает `env:` на отступе 4.
    """
    keys: dict[str, str] = {}
    current = ""
    in_schema = False
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        stripped = line.strip()
        if indent == 0:
            in_schema = stripped.startswith("config_schema:")
            current = ""
            continue
        if not in_schema:
            continue
        if indent == 2 and re.match(r"^[A-Za-z_][\w]*:\s*$", stripped):
            current = stripped[:-1].strip()
            keys[current] = ""
        elif indent == 4 and current and stripped.startswith("env:"):
            keys[current] = stripped.split(":", 1)[1].strip().strip("'\"")
    return keys


def test_manifest_has_no_dead_config_keys():
    """`ui_port` мёртв; каждый ключ `config_schema` читается кодом плагина (или его env)."""
    text = read(MANIFEST)
    assert not re.search(r"(?m)^[ \t]+ui_port:", text), "мёртвый ключ ui_port в манифесте"
    keys = config_schema_keys(text)
    assert {"telegram_bot_token", "telegram_chat_id", "project_root"} <= set(keys)
    sources = "\n".join(read(path) for path in PLUGIN_SOURCES)
    for key, env in keys.items():
        assert key in sources or (env and env in sources), f"мёртвый ключ {key} в манифесте"


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
    # Репозиторий называется hermes-step-pipeline, плагин — step_pipeline: убираем оба
    # написания имени репозитория, иначе дефис в адресе установки читается как имя плагина.
    rest = text.replace(REPO_URL, "").replace("hermes-step-pipeline", "")
    assert "step-pipeline" not in rest, "в README осталось дефисное имя плагина"
    for command in ("plugins/step_pipeline", "plugins enable step_pipeline",
                    "plugins remove step_pipeline"):
        assert command in text, f"в README нет команды {command}"

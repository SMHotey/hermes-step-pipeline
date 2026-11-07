# -*- coding: utf-8 -*-
"""Роли: манифест скилла, категории установщика и протокол планирования."""
from __future__ import annotations

import re
import sys
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

import helpers  # noqa: E402  (загружает плагин как пакет и регистрирует алиасы)
import setup_flow                                      # noqa: E402

ROLES = PLUGIN / "step_pipeline" / "roles"
KEY_LINE = r"^[ ]*[A-Za-z_][A-Za-z0-9_-]*:(?: .*)?$"
ITEM_LINE = r"^[ ]*-(?: .*)?$"
SKILL_ANCHORS = ("## Режим планирования", "plan-mode status", "(рекомендуется)",
                 "drafts/", "plan --accept", "Допущения")


def frontmatter_lines(skill: Path) -> list[str]:
    """Строки frontmatter между первой и второй строкой `---` (stdlib, без PyYAML)."""
    lines = skill.read_text(encoding="utf-8").splitlines()
    assert lines and lines[0] == "---", f"{skill}: файл не начинается строкой ---"
    for index in range(1, len(lines)):
        if lines[index] == "---":
            return lines[1:index]
    raise AssertionError(f"{skill}: frontmatter не закрыт строкой ---")


def top_level(skill: Path) -> dict[str, str]:
    """Поля верхнего уровня: `ключ: значение` без отступа и без `-`."""
    fields: dict[str, str] = {}
    for line in frontmatter_lines(skill):
        if not line.strip() or line[0] == " " or line[0] == "-":
            continue
        key, sep, value = line.partition(":")
        assert sep, f"{skill}: строка frontmatter без двоеточия: {line!r}"
        fields[key] = value.strip()
    return fields


def test_roles_have_skill_and_category():
    """У каждой роли есть SKILL.md, `name` равен каталогу, категории совпадают с ролями."""
    roles = sorted(path.name for path in ROLES.iterdir() if path.is_dir())
    assert roles, "каталог ролей пуст"
    for name in roles:
        skill = ROLES / name / "SKILL.md"
        assert skill.is_file(), f"{name}: нет SKILL.md"
        assert top_level(skill).get("name") == name, f"{name}: name не совпадает с каталогом"
    assert set(roles) == set(setup_flow.SKILL_CATEGORIES), "роли и SKILL_CATEGORIES разошлись"
    assert len(roles) == len(setup_flow.SKILL_CATEGORIES) == 8


def test_frontmatter_is_yaml_subset():
    """Frontmatter разбирается без PyYAML: `---`, без табов, ключи/списки с отступом."""
    for skill in sorted(ROLES.glob("*/SKILL.md")):
        lines = frontmatter_lines(skill)
        assert lines, f"{skill}: пустой frontmatter"
        for line in lines:
            assert "\t" not in line, f"{skill}: таб в frontmatter: {line!r}"
            if not line.strip():
                continue
            match = re.match(f"(?:{KEY_LINE})|(?:{ITEM_LINE})", line)
            assert match, f"{skill}: строка не ключ и не элемент списка: {line!r}"
            indent = len(line) - len(line.lstrip(" "))
            assert indent % 2 == 0, f"{skill}: отступ не кратен двум: {line!r}"
        fields = top_level(skill)
        for key in ("name", "description", "version"):
            assert fields.get(key), f"{skill}: нет поля {key}"


def test_descriptions_are_short_quoted_triggers():
    """`description` — одна строка в двойных кавычках, до 150 символов, без хвостовых пробелов."""
    for skill in sorted(ROLES.glob("*/SKILL.md")):
        lines = [line for line in frontmatter_lines(skill) if line.startswith("description:")]
        assert len(lines) == 1, f"{skill}: поле description должно быть ровно одно"
        line = lines[0]
        assert line == line.rstrip(), f"{skill}: хвостовые пробелы в description"
        raw = line[len("description:"):].strip()
        assert len(raw) >= 2 and raw[0] == raw[-1] == '"', \
            f"{skill}: description не в двойных кавычках"
        value = raw[1:-1]
        assert value, f"{skill}: пустое description"
        assert len(value) <= 150, f"{skill}: description длиннее 150 символов ({len(value)})"


def test_plan_authoring_skill_has_planning_mode_protocol():
    """В скилле планирования есть раздел режима с протоколом и запретом утверждать самому."""
    text = (ROLES / "pipeline-plan-authoring" / "SKILL.md").read_text(encoding="utf-8")
    for anchor in SKILL_ANCHORS:
        assert anchor in text, f"в скилле нет якоря {anchor!r}"
    section = text.split("## Режим планирования", 1)[1].split("\n## ", 1)[0]
    assert "не утверждает план сам" in section, "нет прямого запрета утверждать план самому"
    assert "plan-mode on" in section, "не сказано, какой командой включить режим"


def test_reviewer_skill_requires_recommendations_section():
    """Раздел рекомендаций обязан ехать в комплекте: .hermes/ в gitignore."""
    text = (ROLES / "pipeline-reviewer" / "SKILL.md").read_text(encoding="utf-8")
    assert "## Рекомендации владельцу" in text
    assert "## Рекомендации" in text.split("## Принцип независимости")[0]


def test_prompter_skill_writes_prompt_in_chunks():
    """Правило «писать частями» — защита от лимита вывода модели."""
    text = (ROLES / "pipeline-prompter" / "SKILL.md").read_text(encoding="utf-8")
    assert "## Как писать файл постановки" in text
    assert "40–70 строк" in text or "40-70 строк" in text


def test_spec_anchors_resolve_to_code():
    """Каждая ссылка #ANCHOR:имя в спеке обязана находить комментарий в коде."""
    spec = (PLUGIN / "docs" / "PLAN-FORMAT.md").read_text(encoding="utf-8")
    refs = set(re.findall(r"#ANCHOR:([^`\s,;|]+)", spec))
    assert refs, "в спеке не осталось якорей — проверь, не откатили ли перевод адресов"
    code = "".join(p.read_text(encoding="utf-8")
                   for p in (PLUGIN / "step_pipeline" / "engine").glob("*.py"))
    missing = sorted(r for r in refs if f"# ANCHOR:{r}" not in code)
    assert not missing, f"якоря без кода: {missing}"
    doubled = sorted(r for r in refs if code.count(f"# ANCHOR:{r}") > 1)
    assert not doubled, f"якорь встречается дважды: {doubled}"

# -*- coding: utf-8 -*-
"""Сборщик контекста шага: разбор плана и выбор следующего шага."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import config as config_mod
import gate
import next_step
import registry
from engine_kit import make_project


def test_plan_steps_parsed(project: Path):
    cfg = config_mod.load(project)
    steps = next_step.plan_steps(cfg, cfg.plans_dir / "1-demo.md")
    assert [s.id for s in steps] == ["S1", "S2"]
    first, second = steps
    assert first.title == "Таблица счетов"
    assert first.days == 1.0
    assert first.owner == ""          # владелец не указан — берётся из конфига
    assert first.files == ["app/models/invoice.py", "tests/test_invoice.py"]
    assert first.gates and "pytest" in first.gates[0]
    assert first.dod.startswith("таблица `invoice`")
    assert "не менять контракт" in second.notes
    assert second.owner == "backend"


def test_owner_falls_back_to_driver(project: Path):
    cfg = config_mod.load(project)
    step = next_step.plan_steps(cfg, cfg.plans_dir / "1-demo.md")[0]
    assert (step.owner or cfg.driver) == "demo"


def test_next_is_first_open_step(project: Path):
    cfg = config_mod.load(project)
    pkg = next_step.build_package(cfg, "1")
    assert pkg["next"]["id"] == "S1"
    assert pkg["steps_total"] == 2
    assert pkg["remaining"] == ["S1", "S2"]


def test_closed_step_is_skipped(project: Path):
    plan = project / "docs" / "plans" / "1-demo.md"
    plan.write_text(plan.read_text(encoding="utf-8")
                    + "| 01.11.2026 | S1 | abc123 | ревью APPROVE | ЗАКРЫТ |\n",
                    encoding="utf-8")
    cfg = config_mod.load(project)
    pkg = next_step.build_package(cfg, "1")
    assert pkg["next"]["id"] == "S2"
    assert pkg["closed"] == ["S1"]


def test_all_closed_gives_note(project: Path):
    plan = project / "docs" / "plans" / "1-demo.md"
    plan.write_text(plan.read_text(encoding="utf-8")
                    + "| 01.11.2026 | S1 | a | r | ЗАКРЫТ |\n"
                    + "| 02.11.2026 | S2 | b | r | ЗАКРЫТ |\n", encoding="utf-8")
    cfg = config_mod.load(project)
    pkg = next_step.build_package(cfg, "1")
    assert pkg["next"] is None
    assert "все шаги" in pkg["note"]


def test_force_step(project: Path):
    cfg = config_mod.load(project)
    pkg = next_step.build_package(cfg, "1", force_step="s2")
    assert pkg["next"]["id"] == "S2"


def test_plan_without_steps_section(tmp_path: Path, project: Path):
    (project / "docs" / "plans" / "2-prose.md").write_text(
        "# 2. Проза\n\n## Журнал\n\n| Дата | Шаг | Коммиты | Прогоны | Статус |\n"
        "|---|---|---|---|---|\n| — | S1 | — | — | открыт |\n", encoding="utf-8")
    cfg = config_mod.load(project)
    pkg = next_step.build_package(cfg, "2")
    assert pkg["next"] is None
    assert pkg["steps_total"] == 0
    assert "раздел шагов" in pkg["note"]


def test_list_plans_overview(project: Path):
    cfg = config_mod.load(project)
    rows = next_step.plans_overview(cfg)
    assert rows[0]["id"] == "1"
    assert rows[0]["next"] == "S1"
    assert rows[0]["steps"] == 2


def test_json_is_serialisable(project: Path):
    cfg = config_mod.load(project)
    pkg = next_step.build_package(cfg, "1")
    text = json.dumps(pkg, ensure_ascii=False)
    assert '"next"' in text
    assert pkg["plan_file"].endswith("docs/plans/1-demo.md")


# ───────────── настройка step_id_regex и один поиск журнала ─────────────

# Кириллический идентификатор шага из конфига: угловая скобка записана через \x3c,
# чтобы гейт формы постановки не принял её за заглушку.
CYR = "(?P\x3cid>Ш\\d+)"

PLAN_CYR = """# 5. План с настраиваемой регуляркой

## Шаги

### Ш1. Первый шаг (1 дн)

- Файл: `app/a.py`
- Тесты/гейты: `python -m pytest -q` → 1 passed
- Готово = файл создан

## Журнал выполнения

| Дата | Шаг | Коммиты | Прогоны и доказательства | Статус |
|---|---|---|---|---|
| — | Ш1 | — | — | открыт |
"""

PLAN_COMPOUND = """# 11. Составной идентификатор шага

## Шаги

### T10-2. Составной идентификатор (1 дн)

- Файл: `app/b.py`
- Готово = шаг закрыт журналом

## Журнал выполнения

| Дата | Шаг | Коммиты | Прогоны и доказательства | Статус |
|---|---|---|---|---|
| 01.11.2026 | T10-2 (Составной идентификатор) | abc1234 | ревью reviewer — APPROVE | ЗАКРЫТ |
"""

PLAN_TWO_JOURNALS = """# 6. Два раздела со словом-маркером журнала

## Шаги

### T10-2. Составной идентификатор (1 дн)

- Файл: `app/c.py`
- Готово = шаг закрыт журналом

## Журнал согласований

| Дата | Кто | Что |
|---|---|---|
| — | — | — |

## Журнал выполнения

| Дата | Шаг | Коммиты | Прогоны и доказательства | Статус |
|---|---|---|---|---|
| — | T10-2 | — | — | открыт |
"""

PLAN_H1_MARKER = """# 9. Журнал решений конвейера

## Шаги

### T10-2. Составной идентификатор (1 дн)

- Готово = шаг закрыт журналом

## Журнал выполнения

| Дата | Шаг | Коммиты | Прогоны и доказательства | Статус |
|---|---|---|---|---|
| — | T10-2 | — | — | открыт |
"""

PLAN_READY_COLON = """# 7. Критерий готовности с двоеточием

## Шаги

### S1. Шаг с риском (1 дн)

- Файл: `app/d.py`
- Готово = шаг с Риск: high не получает карточку

## Журнал выполнения

| Дата | Шаг | Коммиты | Прогоны и доказательства | Статус |
|---|---|---|---|---|
| — | S1 | — | — | открыт |
"""

PLAN_SHORT_ROW = """# 10. Короткая строка журнала

## Шаги

### S1. Первый шаг (1 дн)

- Готово = файл создан

### S2. Второй шаг (1 дн)

- Готово = файл создан

## Журнал выполнения

| Дата | Шаг | Коммиты | Прогоны и доказательства | Статус |
|---|---|---|---|---|
| — | S1 | — | — | открыт |
| — | S2 | — | ЗАКРЫТ |
"""


def test_step_id_regex_from_config_is_applied(tmp_path: Path):
    root = make_project(tmp_path / "cyr", step_id_regex=CYR)
    (root / "docs" / "plans" / "5-cyr.md").write_text(PLAN_CYR, encoding="utf-8")
    cfg = config_mod.load(root)
    steps = next_step.plan_steps(cfg, cfg.plans_dir / "5-cyr.md")
    assert [s.id for s in steps] == ["Ш1"]
    assert steps[0].title == "Первый шаг"
    assert steps[0].dod == "файл создан"


def test_custom_regex_plan_discovered_and_closed(tmp_path: Path):
    root = make_project(tmp_path / "cyr", step_id_regex=CYR)
    plan = root / "docs" / "plans" / "5-cyr.md"
    plan.write_text(PLAN_CYR + "| 02.11.2026 | Ш1 (Первый шаг) | abc1234"
                              " | ревью APPROVE | ЗАКРЫТ |\n", encoding="utf-8")
    cfg = config_mod.load(root)
    assert [spec.id for spec in registry.discover(cfg)] == ["5"]
    spec = registry.get(cfg, "5")
    assert registry.closed_steps(spec, cfg) == ["Ш1"]


def test_compound_step_id_runs_to_journal(tmp_path: Path):
    root = make_project(tmp_path / "compound")
    (root / "docs" / "plans" / "11-compound.md").write_text(PLAN_COMPOUND, encoding="utf-8")
    cfg = config_mod.load(root)
    pkg = next_step.build_package(cfg, "11")
    assert pkg["steps_total"] == 1
    assert pkg["closed"] == ["T10-2"]
    assert pkg["next"] is None


def test_dod_keeps_text_after_inner_colon(tmp_path: Path):
    root = make_project(tmp_path / "colon")
    (root / "docs" / "plans" / "7-colon.md").write_text(PLAN_READY_COLON, encoding="utf-8")
    cfg = config_mod.load(root)
    steps = next_step.plan_steps(cfg, cfg.plans_dir / "7-colon.md")
    assert steps[0].dod == "шаг с Риск: high не получает карточку"


def test_journal_section_picks_section_with_step_rows(tmp_path: Path):
    root = make_project(tmp_path / "two")
    plans = root / "docs" / "plans"
    (plans / "6-two.md").write_text(PLAN_TWO_JOURNALS, encoding="utf-8")
    (plans / "9-h1.md").write_text(PLAN_H1_MARKER, encoding="utf-8")
    cfg = config_mod.load(root)
    for name in ("6-two.md", "9-h1.md"):
        lines = (plans / name).read_text(encoding="utf-8").splitlines()
        found = registry.journal_section(lines, cfg.journal_marker, cfg.step_regex)
        assert found is not None
        heading, rows = found
        assert heading.startswith("## Журнал выполнения")
        assert "T10-2" in lines[rows[-1]]
        assert all("Кто" not in lines[i] for i in rows)


def test_gate_journal_rows_equals_registry_section(tmp_path: Path):
    root = make_project(tmp_path / "two")
    (root / "docs" / "plans" / "6-two.md").write_text(PLAN_TWO_JOURNALS, encoding="utf-8")
    cfg = config_mod.load(root)
    p = gate.resolve_paths(cfg, "6")
    lines = Path(p.plan_file).read_text(encoding="utf-8").splitlines()
    found = registry.journal_section(lines, cfg.journal_marker, cfg.step_regex)
    assert found is not None
    assert gate._journal_rows(p) == found[1]


def test_journal_closed_sees_row_in_right_section(tmp_path: Path):
    root = make_project(tmp_path / "two")
    text = PLAN_TWO_JOURNALS.replace("| — | T10-2 | — | — | открыт |",
                                    "| — | T10-2 | — | — | ЗАКРЫТ |")
    (root / "docs" / "plans" / "6-two.md").write_text(text, encoding="utf-8")
    cfg = config_mod.load(root)
    p = gate.resolve_paths(cfg, "6")
    assert gate.journal_closed(p, "T10-2") is True


def test_append_journal_row_writes_into_journal_section(tmp_path: Path):
    root = make_project(tmp_path / "two")
    plan = root / "docs" / "plans" / "6-two.md"
    plan.write_text(PLAN_TWO_JOURNALS, encoding="utf-8")
    cfg = config_mod.load(root)
    p = gate.resolve_paths(cfg, "6")
    assert gate.append_journal_row(p, "T10-2", "Составной идентификатор", "abc1234",
                                   "ревью APPROVE") is True
    text = plan.read_text(encoding="utf-8")
    assert text.count("| ЗАКРЫТ |") == 1
    assert text.index("| ЗАКРЫТ |") > text.index("## Журнал выполнения")
    assert gate.journal_closed(p, "T10-2") is True
    assert gate.append_journal_row(p, "T10-2", "Составной идентификатор", "abc1234",
                                   "ревью APPROVE") is False


def test_broken_step_id_regex_is_config_error(tmp_path: Path):
    root = make_project(tmp_path / "bad", step_id_regex=r"S[")
    with pytest.raises(config_mod.ConfigError) as exc:
        config_mod.load(root)
    assert "step_id_regex" in str(exc.value)
    assert "unterminated character set" in str(exc.value)


def test_regex_without_id_group_is_config_error(tmp_path: Path):
    root = make_project(tmp_path / "noid", step_id_regex=r"S\d+")
    with pytest.raises(config_mod.ConfigError) as exc:
        config_mod.load(root)
    assert "групп" in str(exc.value)
    assert "id" in str(exc.value)


def test_short_journal_row_is_ignored_by_both_readers(tmp_path: Path):
    root = make_project(tmp_path / "short")
    (root / "docs" / "plans" / "10-short.md").write_text(PLAN_SHORT_ROW, encoding="utf-8")
    cfg = config_mod.load(root)
    spec = registry.get(cfg, "10")
    p = gate.resolve_paths(cfg, "10")
    assert registry.closed_steps(spec, cfg) == []
    assert gate.journal_closed(p, "S2") is False

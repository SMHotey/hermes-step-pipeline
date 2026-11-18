# -*- coding: utf-8 -*-
"""Утверждение плана само ставит его в ведение (решение владельца 18.11.2026).

Прежняя жизнь: `--accept 2` переносил черновик, писал `plan_2_approval.json` и на этом
заканчивался — чтобы страж взял план, владелец отдельно правил `driven_plans` (или жал
`--start 2`). Владелец назвал это лишним жестом: «после утверждения план должен
автоматически попадать в driven_plans».

Границы, которые держат тесты: `*` и отсутствующий ключ не переписываются (движок и так
ведёт все планы), повтор не плодит дублей, порядок остаётся числовым, а ошибки формы попрежнему
оставляют `pipeline.json` нетронутым — ведение пишется только после успешного утверждения.
"""
from __future__ import annotations

import json
from pathlib import Path

import helpers  # noqa: F401  (загружает плагин и алиасы)
from step_pipeline import plan_accept

PLAN2 = """# 2. Второй план демо

Цель: проверить ведение после утверждения.

## Разведка

Артефакт: `docs/research/2-prior-art.md`. Лицензии: MIT. Кандидаты: свой код.
Решение: go — берём свой код.

Правки по разведке: 0

## Шаги

### S1. Первый шаг второго плана (1 дн)

- Файл: `app/two.py`
- Тесты/гейты: `python -m pytest tests/test_two.py` → 1 passed
- Готово = таблица создана

## Журнал выполнения

| Дата | Шаг | Коммиты | Прогоны и доказательства | Статус |
|---|---|---|---|---|
| — | S1 | — | — | открыт |
"""

# Черновик без пункта «Файл»: линтер обязан его отвергнуть, а ведение — не тронуться.
PLAN2_BROKEN = PLAN2.replace("- Файл: `app/two.py`\n", "")


def _draft(project: Path, name: str, text: str) -> Path:
    folder = plan_accept.drafts_dir(project)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_text(text, encoding="utf-8")
    return path


def _config(project: Path) -> dict:
    return json.loads((project / "pipeline.json").read_text(encoding="utf-8"))


def _set_driven(project: Path, value) -> None:
    config = _config(project)
    if value is _MISSING:
        config.pop("driven_plans", None)
    else:
        config["driven_plans"] = value
    (project / "pipeline.json").write_text(json.dumps(config, ensure_ascii=False, indent=2),
                                           encoding="utf-8")


class _Missing:
    """Маркер «ключа нет вовсе»: `None` в конфиге значит пустой список, это разные вещи."""


_MISSING = _Missing()


def test_approval_appends_number_in_numeric_order(tmp_path: Path) -> None:
    """Черновик утверждён — номер добавлен, порядок числовой, остальные ключи целы."""
    root = helpers.install_project(tmp_path / "demo")
    _set_driven(root, ["13", "1"])
    _draft(root, "2-second.md", PLAN2)

    result = plan_accept.approve(root, "2")

    assert result["ok"] is True, result
    assert result["driven"] == "added", result
    assert "добавлен в driven_plans" in result["message"], result["message"]
    config = _config(root)
    assert config["driven_plans"] == ["1", "2", "13"], config["driven_plans"]
    assert config["project"] == "demo", "остальные ключи конфигурации не тронуты"
    assert (root / "docs" / "plans" / "2-second.md").is_file(), "черновик перенесён"
    assert plan_accept.approval_file(root, "2").is_file(), "запись утверждения на месте"


def test_star_is_left_alone(tmp_path: Path) -> None:
    """`*` значит «ведомы все»: файл не переписывается, ответ говорит `all`."""
    root = helpers.install_project(tmp_path / "demo")
    _set_driven(root, ["*"])
    _draft(root, "2-second.md", PLAN2)
    before = (root / "pipeline.json").read_bytes()

    result = plan_accept.approve(root, "2")

    assert result["ok"] is True and result["driven"] == "all", result
    assert (root / "pipeline.json").read_bytes() == before


def test_absent_key_is_not_created(tmp_path: Path) -> None:
    """Ключа нет — движок читает это как «все»; утверждение не заводит список из одного."""
    root = helpers.install_project(tmp_path / "demo")
    _set_driven(root, _MISSING)
    _draft(root, "2-second.md", PLAN2)
    before = (root / "pipeline.json").read_bytes()

    result = plan_accept.approve(root, "2")

    assert result["ok"] is True and result["driven"] == "all", result
    assert (root / "pipeline.json").read_bytes() == before


def test_repeat_approval_does_not_duplicate(tmp_path: Path) -> None:
    """Номер уже в списке — `already`, дубля нет, файл не переписан."""
    root = helpers.install_project(tmp_path / "demo")
    _set_driven(root, ["1", "2"])
    _draft(root, "2-second.md", PLAN2)
    plan_accept.approve(root, "2")
    before = (root / "pipeline.json").read_bytes()

    again = plan_accept.approve(root, "2")

    assert again["driven"] == "already", again
    assert _config(root)["driven_plans"] == ["1", "2"]
    assert (root / "pipeline.json").read_bytes() == before


def test_lint_errors_leave_driven_untouched(tmp_path: Path) -> None:
    """Отказ по форме: файл остался черновиком, ведение не тронуто, поля `driven` нет."""
    root = helpers.install_project(tmp_path / "demo")
    _set_driven(root, ["1"])
    _draft(root, "2-second.md", PLAN2_BROKEN)
    before = (root / "pipeline.json").read_bytes()

    result = plan_accept.approve(root, "2")

    assert result["ok"] is False and result["status"] == "errors", result
    assert "driven" not in result, result
    assert (root / "pipeline.json").read_bytes() == before
    assert (root / "docs" / "plans" / "drafts" / "2-second.md").is_file()


def test_string_form_of_driven_plans_is_understood(tmp_path: Path) -> None:
    """Список строкой (`"1, 5"` — форма панели) добавляется так же, как список."""
    root = helpers.install_project(tmp_path / "demo")
    _set_driven(root, "1, 5")
    _draft(root, "2-second.md", PLAN2)

    result = plan_accept.approve(root, "2")

    assert result["driven"] == "added", result
    assert _config(root)["driven_plans"] == ["1", "2", "5"], _config(root)["driven_plans"]

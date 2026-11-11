# -*- coding: utf-8 -*-
"""Golden-вердикты: эталонные состояния проекта и ожидаемые решения стража.

Каждый случай — каталог `tests/engine/golden/<случай>/` с тремя вещами: файлами
проекта (`project/`), описанием сборки (`case.json`) и записанным вердиктом
(`expected.json`). Раннер прогоняет настоящий `gate.py` на копии движка из репозитория,
поэтому эталон ловит любую перемену поведения: перенос кода, забытую ветку, смену
текста причины, порядок действий.

Перезапись эталонов — только после осознанного изменения поведения:
    GOLDEN_UPDATE=1 python -m pytest tests/engine/test_golden_verdicts.py
Дифф при падении печатается путями (`ticks[0].action`, `cards[1].status`), чтобы было
видно, что именно поехало.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import golden_kit as kit

CASES = kit.case_dirs()


def pytest_generate_tests(metafunc):
    """Корпус — данные: новый каталог с `case.json` попадает в прогон сам."""
    if "case_dir" in metafunc.fixturenames:
        metafunc.parametrize("case_dir", CASES, ids=[path.name for path in CASES])


@pytest.fixture()
def case_dir(request) -> Path:
    """Каталог случая: `project/`, `case.json`, `expected.json`."""
    return request.param


def test_golden_verdict(case_dir, tmp_path, hermes_stub):
    """Страж на зафиксированном состоянии решает ровно то, что записано в эталоне."""
    case = kit.load_case(case_dir)
    root = tmp_path / case_dir.name
    root.mkdir()

    kit.build_project(case_dir, root, case, hermes_stub)
    ticks = kit.run_ticks(root, case.get("ticks", 1))
    actual = kit.record(case_dir, root, hermes_stub, ticks)

    path = kit.expected_path(case_dir)
    if kit.update_mode():
        path.write_text(kit.dump(actual), encoding="utf-8")
        return
    assert path.is_file(), (f"нет эталона {path}: запиши его через {kit.UPDATE_ENV}=1 "
                            f"и проверь diff глазами")

    expected = json.loads(path.read_text(encoding="utf-8"))
    problems = kit.diff_paths(expected, actual)
    assert not problems, ("поведение стража изменилось относительно эталона "
                          f"{case_dir.name}:\n  " + "\n  ".join(problems))


def test_corpus_is_not_empty():
    """Пустой корпус — тихий отказ от проверки: так ловить регрессии нельзя."""
    assert CASES, "в tests/engine/golden нет ни одного случая"


def test_every_case_documents_its_verdict():
    """У случая есть `note`: эталон без объяснения через месяц не читается."""
    for path in CASES:
        assert kit.load_case(path).get("note"), f"{path.name}: нет поля note в case.json"

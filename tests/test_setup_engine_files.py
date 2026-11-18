# -*- coding: utf-8 -*-
"""Доставка линтера плана: `setup --upgrade` привозит `plan_lint.py` в `.pipeline/`.

Шаг 15-S3: линтер стал файлом движка, и список копируемых файлов (`ENGINE_FILES`)
остался единственным. Три случая — копирование (`copy_engine`), маркер целостности
(`write_engine_marker` + `engine_marker_problem`), хеш (`engine_digest`): модуль обязан не
только лежать рядом, но и входить в хеш — иначе правка линтера не сдвинет маркер.

Живые проекты не трогаем (Р7): копия строится на `tmp_path`.
"""
from __future__ import annotations

import sys
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

import helpers  # noqa: F401,E402  (загружает плагин как пакет и регистрирует алиасы)
import project as proj                          # noqa: E402
import setup_flow                               # noqa: E402

#: Имя модуля, ради которого написан шаг.
LINT = "plan_lint.py"


def test_upgrade_brings_plan_lint(tmp_path: Path) -> None:
    """`copy_engine` кладёт линтер байт в байт и возвращает его после удаления."""
    assert LINT in setup_flow.ENGINE_FILES

    root = tmp_path / "proj"
    (root / ".pipeline").mkdir(parents=True)
    copied = setup_flow.copy_engine(root, force=False)

    assert f".pipeline/{LINT}" in copied
    assert (root / ".pipeline" / LINT).read_bytes() == (proj.ENGINE_SRC / LINT).read_bytes()

    # Проект, поставленный до шага: в его копии движка линтера нет.
    (root / ".pipeline" / LINT).unlink()
    copied = setup_flow.copy_engine(root, force=False)

    assert f".pipeline/{LINT}" in copied
    assert (root / ".pipeline" / LINT).read_bytes() == (proj.ENGINE_SRC / LINT).read_bytes()


def test_marker_names_missing_lint(tmp_path: Path) -> None:
    """Пропавший линтер назван по имени: маркер и дрейф видят именно `plan_lint.py`."""
    root = tmp_path / "proj"
    (root / ".pipeline").mkdir(parents=True)
    setup_flow.copy_engine(root, force=False)
    setup_flow.write_engine_marker(root)
    assert setup_flow.engine_marker_problem(root) == ""

    (root / ".pipeline" / LINT).unlink()

    assert setup_flow.engine_marker_problem(root) == f"в копии нет файла движка: {LINT}"
    assert LINT in setup_flow.engine_drift(root)


def test_digest_covers_plan_lint(tmp_path: Path) -> None:
    """Правка линтера в копии меняет хеш движка: файл входит в хеш, а не лежит рядом."""
    root = tmp_path / "proj"
    (root / ".pipeline").mkdir(parents=True)
    setup_flow.copy_engine(root, force=False)

    base = root / ".pipeline"
    before = setup_flow.engine_digest(base)
    path = base / LINT
    path.write_bytes(path.read_bytes() + b"\n# pravka\n")

    assert setup_flow.engine_digest(base) != before
    assert LINT in setup_flow.engine_drift(root)

# -*- coding: utf-8 -*-
"""Маркер `slow`: объявлен, помеченные файлы выпадают из точечного прогона, полный цел.

Три инварианта шага 8-S2, по тесту на каждый:
1. `slow` объявлен в `pytest.ini` — иначе каждый помеченный тест даёт
   `PytestUnknownMarkWarning`, а необъявленный маркер держится только на дисциплине;
2. файлы с `pytestmark = pytest.mark.slow` целиком отсеиваются `-m "not slow"` —
   ровно этого ждёт точечный прогон исполнителя и ревьюера;
3. `-m` не закреплён в `addopts`: полный прогон и CI обязаны остаться полными.

Числа не хардкодятся: собираемое количество берётся из самого pytest, поэтому новый
golden-кейс или новый тест в этих файлах тест не роняет.
"""
from __future__ import annotations

import configparser
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Файлы шага, помеченные маркером на весь модуль (решение D1 постановки).
SLOW_FILES = ("tests/engine/test_gate_e2e.py", "tests/engine/test_golden_verdicts.py")


def run_pytest(*args: str) -> subprocess.CompletedProcess:
    """Подпроцесс pytest — с той же формой чтения вывода, что у движка.

    `encoding`/`errors` обязательны: без них на cp1251-консоли чтение падает.
    """
    return subprocess.run([sys.executable, "-m", "pytest", *args], cwd=ROOT,
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=300)


def test_slow_marker_is_declared():
    """`--markers` содержит объявление `slow` (строка `@pytest.mark.slow: описание`)."""
    proc = run_pytest("--markers")
    assert proc.returncode == 0, f"--markers упал: {proc.stdout[-400:]}{proc.stderr[-400:]}"
    assert re.search(r"^@pytest\.mark\.slow:", proc.stdout, re.MULTILINE), (
        "маркер slow не объявлен в pytest.ini: в выводе --markers нет строки "
        f"«@pytest.mark.slow:»\n{proc.stdout[:400]}")


def test_slow_files_are_deselected_whole():
    """Помеченные файлы целиком отсеиваются `-m "not slow"`.

    Разбираются обе формы строки итога pytest 9.2.0: частичный отсев
    (`138/142 tests collected (4 deselected)`) и полный (`no tests collected (14 deselected)`);
    формы `0/14` у него нет. Якорь `^` не ставится: без `-q` строка обрамлена знаками `=`.
    """
    out_a = run_pytest("--collect-only", *SLOW_FILES)
    assert out_a.returncode == 0, f"сбор не удался: {out_a.stdout[-400:]}"
    m_a = re.search(r"(\d+) tests collected", out_a.stdout)
    assert m_a, f"нет строки сбора в выводе: {out_a.stdout[-400:]}"
    collected = int(m_a.group(1))

    out_b = run_pytest("--collect-only", "-m", "not slow", *SLOW_FILES)
    m_b = re.search(r"(?:no tests collected|(\d+)/(\d+) tests collected) "
                    r"\((\d+) deselected\)", out_b.stdout)
    assert m_b, f"нет строки итога в выводе (б): {out_b.stdout[-400:]}"
    selected = int(m_b.group(1)) if m_b.group(1) is not None else 0
    deselected = int(m_b.group(3))

    assert collected > 0, f"в помеченных файлах нечего собирать: {out_a.stdout[-400:]}"
    assert selected == 0, f"из помеченных файлов осталось {selected} тестов: {out_b.stdout[-400:]}"
    assert deselected == collected, (f"отсеяно {deselected}, а собрано {collected}: "
                                     "пометка не покрывает файл целиком")


def test_full_run_stays_full():
    """В `addopts` нет `-m`: закрепление урезало бы полный прогон и CI (стоп-сигнал плана)."""
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(ROOT / "pytest.ini")
    addopts = parser.get("pytest", "addopts", fallback="")
    tokens = addopts.split()
    assert "-m" not in tokens, f"в addopts закреплён -m: {addopts!r}"
    assert not any(token.startswith("-m") for token in tokens), (
        f"в addopts есть токен фильтра -m: {addopts!r}")

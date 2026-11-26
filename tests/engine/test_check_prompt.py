# -*- coding: utf-8 -*-
"""Гейт формы постановки (check_task_prompt)."""
from __future__ import annotations

import check_task_prompt as ctk

GOOD = """# Промт: 1 / S1 — таблица счетов

## 1. Роль и цель

Ты — бэкенд-разработчик. Результат: модель Invoice и её миграция.

## 2. Контекст (факты)

- Репозиторий: `demo`, ветка `main`, HEAD `abc1234`.
- Окружение: bash, интерпретатор `.venv/bin/python`, тесты `python -m pytest`.
- План: `docs/plans/1-demo.md` → раздел S1.

## 3. Границы и запреты

Можно менять: `app/models/invoice.py`, `tests/test_invoice.py`.
Запрещено: менять существующие модели, править данные.
Не входит в задачу: API-эндпоинт.

## 4. Что сделать

1. `app/models/invoice.py` — добавить модель Invoice → таблица `invoice`.
2. `tests/test_invoice.py` — тесты создания, границ и уникальности.

## 5. Как проверить (гейты)

```
python -m pytest tests/test_invoice.py -q   # ожидается: 3 passed
```

Вывод в файл, `EXIT` смотреть у pytest.

## 6. Стоп-сигналы

- нужна миграция с NOT NULL или drop;
- факт в коде расходится с этим промтом;
- гейт красный по посторонней причине.

## 7. Формат отчёта

`diff stat` → диффы → решения → строки гейтов (команда + число + EXIT) → отклонения → вопросы.

## 8. Вопросы

Нет вопросов: решения записаны в блоке 4.

## 9. Ревью (обязательно)

Шаг закрывает роль `reviewer` по плану и коду; вердикт APPROVE или REQUEST_CHANGES.
При REQUEST_CHANGES шаг остаётся открытым.
"""


def codes(text: str, **kwargs) -> dict[str, str]:
    return {code: level for level, code, _msg in ctk.check_prompt(text, **kwargs)}


def replace_section(text: str, title: str, body: str) -> str:
    """Заменить тело блока `## …title…` (до следующего `## `)."""
    out: list[str] = []
    skipping = False
    for line in text.splitlines():
        if line.startswith("## "):
            skipping = title in line
            out.append(line)
            if skipping:
                out.append("")
                out.append(body)
            continue
        if not skipping:
            out.append(line)
    return "\n".join(out) + "\n"


def test_good_prompt_passes():
    results = ctk.check_prompt(GOOD, "reviewer", "pipeline-reviewer")
    assert all(level != "FAIL" for level, _c, _m in results), results


def test_missing_block_fails_c1():
    text = GOOD.replace("## 6. Стоп-сигналы", "## 6. Что-то другое")
    assert codes(text)["C1"] == "FAIL"


def test_empty_block_fails_c2():
    text = GOOD.replace(
        "- нужна миграция с NOT NULL или drop;\n- факт в коде расходится с этим промтом;\n"
        "- гейт красный по посторонней причине.",
        "")
    assert codes(text)["C2"] == "FAIL"


def test_placeholder_fails_c3():
    text = GOOD.replace("Нет вопросов: решения записаны в блоке 4.", "TBD — спросить позже.")
    assert codes(text)["C3"] == "FAIL"


def test_no_command_fails_c4():
    text = replace_section(GOOD, "Как проверить", "просто посмотреть глазами.")
    assert codes(text)["C4"] == "FAIL"


def test_no_expected_fails_c5():
    text = replace_section(GOOD, "Как проверить",
                           "```\npython -m pytest tests/test_invoice.py -q\n```")
    assert codes(text)["C5"] == "FAIL"


def test_short_prompt_fails_c6():
    assert codes("## 1. Роль и цель\n\nx\n")["C6"] == "FAIL"


def test_review_role_required_c7():
    text = GOOD.replace("роль `reviewer`", "роль `кто-то`")
    assert codes(text)["C7"] == "FAIL"


def test_custom_role_name():
    text = GOOD.replace("роль `reviewer`", "роль `inspector`")
    assert codes(text, reviewer_role="inspector")["C7"] == "PASS"


def test_too_many_questions_warns():
    text = GOOD.replace("Нет вопросов: решения записаны в блоке 4.",
                        "Что делать? Как? Почему? Когда? Где?")
    assert codes(text)["W1"] == "WARN"


# ───────────────── спека высокорискового шага: C7 (8-S2B) ─────────────────

SPEC_BODY = """### Замысел

Шаг закрывает расхождение между примерами конфигурации и разбором: сейчас примеры
описывают ключи, которых разбор не знает, и потребитель узнаёт об этом на запуске.
Нужно, чтобы набор ключей примера и набора ключей разбора совпадал, а расхождение
показывал гейт.

### Границы

Входит: сверка ключей примеров с разбором и тест на расхождение. Не входит: чистка
самих примеров от устаревших значений и правка разбора под новые ключи.

### Критерии готовности

`python -m pytest tests/test_config_example.py -q` → 3 passed; в тесте есть случай
«ключ есть в примере, нет в разборе» и обратный; `git diff` не трогает gate.py.

### Риски

Расхождение может оказаться намеренным (ключ читается из окружения): тогда тест
фиксирует исключение списком, а не ослабляется до «проверять нечего».
"""


def with_spec(text: str, body: str = SPEC_BODY) -> str:
    """Промт с блоком «Спека» прямо перед «3. Границы и запреты»."""
    return text.replace("## 3. Границы и запреты",
                        f"## Спека (у шага с «Риск: high»)\n\n{body}\n\n## 3. Границы и запреты")


def test_spec_not_required_for_normal_risk():
    """Без `Риск: high` спека не требуется: постановка проходит как раньше."""
    assert ctk.spec_problems(GOOD) != []          # спеки в GOOD нет
    assert codes(GOOD)["C7"] == "PASS"
    assert codes(GOOD, risk="normal")["C7"] == "PASS"
    assert codes(GOOD, risk="low")["C7"] == "PASS"


def test_spec_missing_fails_c7_at_high_risk():
    results = {code: (level, msg) for level, code, msg in
               ctk.check_prompt(GOOD, "reviewer", "", risk="high")}
    level, msg = results["C7"]
    assert level == "FAIL", results
    assert "Спека" in msg
    # Строк результата столько же, сколько было: C7 совмещает ревью и спеку (D1).
    # `9` вместо `8` — отдельная строка W2 про артефакт шага (17-S6): она про постановку,
    # а не про C7, и проверка «C7 не плодит строк» держится вторым утверждением.
    rows = ctk.check_prompt(GOOD, "reviewer", "", risk="high")
    assert len(rows) == 9
    assert [code for _level, code, _msg in rows].count("C7") == 1


def test_full_spec_passes_c7():
    text = with_spec(GOOD)
    assert ctk.spec_problems(text) == []
    assert codes(text, risk="high")["C7"] == "PASS"


def test_short_spec_fails_c7():
    short = with_spec(GOOD, "### Замысел\n\nКоротко.\n\n### Границы\n\nНет.\n"
                            "\n### Критерии готовности\n\nНе проверяются.\n"
                            "\n### Риски\n\nПочти нет.\n")
    problems = ctk.spec_problems(short)
    assert any("символов из" in item for item in problems), problems


def test_spec_without_subsections_fails_c7():
    body = "Одна вода без подразделов. " * 40
    problems = ctk.spec_problems(with_spec(GOOD, body))
    assert any("Критерии готовности" in item for item in problems), problems


def test_spec_not_required_in_hold_mode():
    """`risk_gate: hold`: high-шаг держит ручной допуск, спека не спрашивается."""
    assert codes(GOOD, risk="high", risk_gate="hold")["C7"] == "PASS"


def test_spec_checked_by_cli_flag(tmp_path, capsys):
    """CLI: `--risk high` включает проверку спеки и говорит об этом кодом 1."""
    path = tmp_path / "prompt.md"
    path.write_text(GOOD, encoding="utf-8")
    assert ctk.main([str(path)]) == 0
    assert ctk.main([str(path), "--risk", "high"]) == 1
    out = capsys.readouterr().out
    assert "C7" in out and "Спека" in out

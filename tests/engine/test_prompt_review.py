# -*- coding: utf-8 -*-
"""Ревью постановки перед стартом шага (план 2, §S6, правило 3).

Свежая постановка (`recon_commit` совпадает с HEAD, кода после снятия recon не меняли)
стартует без ревью; устаревшая блокирует старт до вердикта ревьюера; вердикт без сверки
версии постановки не принимается; режимы `auto/always/never` читаются из `pipeline.json`,
а неизвестный режим — ошибка конфигурации. Свежесть проверяется на настоящем git-репозитории.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import config as config_mod
import check_task_prompt as ctk
import gate
from engine_kit import cards, complete, write_approval


def plan_body(count: int = 3) -> str:
    """План из `count` шагов с журналом (все строки «открыт»)."""
    parts = ["# 1. Демо-план конвейера", "", "Цель: проверить ревью постановки.", "",
             "## Шаги", ""]
    for i in range(1, count + 1):
        parts += [f"### S{i}. Шаг номер {i} (1 дн)", "",
                  f"- Файл: `app/s{i}.py`",
                  f"- Тесты/гейты: `python -m pytest tests/test_s{i}.py -q` → 1 passed",
                  f"- Готово = шаг S{i} сделан", ""]
    parts += ["## Журнал выполнения", "",
              "| Дата | Шаг | Коммиты | Прогоны и доказательства | Статус |",
              "|---|---|---|---|---|"]
    parts += [f"| — | S{i} | — | — | открыт |" for i in range(1, count + 1)]
    return "\n".join(parts) + "\n"


def write_plan(root: Path, text: str) -> Path:
    """Переписать план проекта и переутвердить его (запись хранит sha файла плана)."""
    path = Path(root) / "docs" / "plans" / "1-demo.md"
    path.write_text(text, encoding="utf-8")
    write_approval(root, path)
    return path


def write_prompt(root: Path, step: str, recon: str | None = None) -> Path:
    """Постановка шага; `recon` — отметка `recon_commit:` в шапке."""
    folder = Path(root) / "docs" / "prompts"
    folder.mkdir(parents=True, exist_ok=True)
    head = f"recon_commit: {recon}\n" if recon else ""
    path = folder / f"2026-11-04_1-{step}_prompt.md"
    path.write_text(f"# Постановка {step}\n\n{head}\n## Ревью\n\nроль `reviewer`.\n",
                    encoding="utf-8")
    return path


def write_report(root: Path, verdict: str, recon: str | None = "1111111") -> Path:
    """Отчёт ревью постановки: отметка версии + одна строка вердикта.

    `recon=None` пишет отчёт без строки `recon_commit:` — отчёт старого образца,
    который D6 не обязан принимать.
    """
    path = Path(root) / "docs" / "reviews" / "1-S1-prompt.md"
    head = f"recon_commit: {recon}\n" if recon else ""
    path.write_text(f"{head}\n## ВЕРДИКТ: {verdict}\n\nпроверено\n",
                    encoding="utf-8")
    return path


def keys_of(db: Path) -> set[str]:
    """Ключи идемпотентности всех карточек поддельной борды."""
    return {c["key"] for c in cards(db)}


def quiet(_text: str) -> None:
    """Заглушка уведомлений: тексты проверяются там, где они обязательны."""


def git(root: Path, *args: str) -> str:
    res = subprocess.run(["git", *args], cwd=str(root), capture_output=True, text=True,
                         encoding="utf-8", errors="replace")
    assert res.returncode == 0, res.stderr
    return res.stdout.strip()


def init_repo(root: Path) -> str:
    """Настоящий git-репозиторий проекта; возвращает sha HEAD."""
    git(root, "init", "-b", "main")
    git(root, "config", "user.email", "test@example.com")
    git(root, "config", "user.name", "Test")
    git(root, "add", "-A")
    git(root, "commit", "-m", "init")
    return git(root, "rev-parse", "HEAD")


def set_config(root: Path, **keys) -> None:
    """Записать ключи в `pipeline.json` проекта (как это сделал бы владелец)."""
    path = Path(root) / "pipeline.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    for key, value in keys.items():
        if value is None:
            raw.pop(key, None)
        else:
            raw[key] = value
    path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")


def paths(root: Path):
    """Пути плана 1 после правки `pipeline.json`."""
    return gate.resolve_paths(config_mod.load(root), "1")


@pytest.fixture()
def stand(project: Path, hermes_stub: Path, monkeypatch):
    """Пути плана 1 на временном проекте; сборщик шагов смотрит в PIPELINE_REPO."""
    monkeypatch.setenv("PIPELINE_REPO", str(project))
    return project, paths(project), hermes_stub


def test_fresh_prompt_starts_without_review(stand):
    """`recon_commit` равен HEAD — постановка свежая, ревью не нужно (режим auto).

    После recon закоммичено только в `docs/`: `git diff` (код 0) отсекает эти правки
    исключением `:(exclude)docs` — без него постановка ложно считалась бы устаревшей.
    """
    root, p, db = stand
    write_plan(root, plan_body(3))
    head = init_repo(root)
    # docs-коммит после recon: другого кода в диапазоне нет
    (root / "docs" / "notes.md").write_text("заметка\n", encoding="utf-8")
    git(root, "add", "-A")
    git(root, "commit", "-m", "docs only")
    prompt = write_prompt(root, "S1", recon=head)
    res = gate.run_gate(paths(root), execute=True, notify_fn=quiet)
    assert res["decision"]["action"] == "create_step", res["decision"]
    assert "planloop:1:S1:step" in keys_of(db)
    assert not any(c["key"].endswith(":promptreview") for c in cards(db))
    assert gate.prompt_fresh(p, gate.prompt_recon(prompt)) is True


def test_stale_prompt_waits_for_prompt_review(stand):
    """После recon закоммичен код: `git diff` вернул 1 — без ревью постановки шаг не стартует.

    Это ветка кода 1 в `prompt_fresh`; путь кода 128 (git не нашёл sha) покрыт в
    `test_prompt_review_verdict_controls_start`, там постановка без репозитория.
    """
    root, p, db = stand
    write_plan(root, plan_body(3))
    head = init_repo(root)
    # кодовый коммит после recon: в диапазоне есть правка вне docs/
    (root / "app").mkdir()
    (root / "app" / "s1.py").write_text("x = 1\n", encoding="utf-8")
    git(root, "add", "-A")
    git(root, "commit", "-m", "code after recon")
    write_prompt(root, "S1", recon=head)

    first = gate.run_gate(p, execute=True, notify_fn=quiet)
    assert first["decision"]["action"] == "create_prompt_review", first["decision"]
    assert first["applied"] is True
    card = next(c for c in cards(db) if c["key"] == "planloop:1:S1:promptreview")
    assert card["assignee"] == "reviewer"
    assert "planloop:1:S1:step" not in keys_of(db)      # шаг не начат

    second = gate.run_gate(p, execute=True, notify_fn=quiet)
    assert second["decision"]["action"] == "idle", second["decision"]
    assert "ревью постановки S1 идёт" in second["decision"]["reason"]
    assert "planloop:1:S1:step" not in keys_of(db)
    assert sum(1 for c in cards(db) if c["key"] == "planloop:1:S1:promptreview") == 1


def test_prompt_review_verdict_controls_start(stand):
    """REQUEST_CHANGES → автопереписывание постановки и повторное ревью; APPROVE — старт.

    Автоматика (правило 3, дополнение): постановку переписывает роль prompter карточкой
    `:promptfix:{N}`, после правки ревью ставится заново (`:promptreview:{N}`). Стоп —
    только на исчерпанном бюджете кругов правок (у пересборок свой лимит
    `max_stale_rebuilds`).
    """
    root, p, db = stand
    write_plan(root, plan_body(3))
    write_prompt(root, "S1", recon="1111111")
    gate.run_gate(p, execute=True, notify_fn=quiet)     # карточка ревью постановки
    complete(db, ":promptreview")

    write_report(root, "REQUEST_CHANGES")
    fixing = gate.run_gate(p, execute=True, notify_fn=quiet)
    assert fixing["decision"]["action"] == "create_prompt_fix", fixing["decision"]
    assert fixing["decision"]["round"] == 1
    assert "planloop:1:S1:promptfix:1" in keys_of(db)
    assert "planloop:1:S1:step" not in keys_of(db)
    assert gate.load_stop(p) is None                    # конвейер не встал
    row = next(c for c in cards(db) if c["key"] == "planloop:1:S1:promptfix:1")
    assert row["assignee"] == "prompter"
    assert "переписать постановку" in row["title"]

    complete(db, ":promptfix:1")
    again = gate.run_gate(p, execute=True, notify_fn=quiet)
    assert again["decision"]["action"] == "create_prompt_review", again["decision"]
    assert again["decision"]["round"] == 2
    assert "planloop:1:S1:promptreview:2" in keys_of(db)
    complete(db, ":promptreview:2")

    write_report(root, "APPROVE", recon="2222222")      # ревью не про эту версию (D6)
    rebuilt = gate.run_gate(p, execute=True, notify_fn=quiet)
    assert rebuilt["decision"]["action"] == "create_prompt_fix", rebuilt["decision"]
    assert rebuilt["decision"]["stale"] is True
    assert rebuilt["decision"]["rebuild"] == 1
    assert "planloop:1:S1:promptfix-stale:1" in keys_of(db)
    assert "planloop:1:S1:step" not in keys_of(db)
    assert gate.load_stop(p) is None                    # конвейер не встал

    complete(db, ":promptfix-stale:1")                  # промптер пересобрал постановку
    again2 = gate.run_gate(p, execute=True, notify_fn=quiet)
    assert again2["decision"]["action"] == "create_prompt_review", again2["decision"]
    assert again2["decision"]["round"] == 3             # 1 круг правок + 1 пересборка + 1
    assert "planloop:1:S1:promptreview:3" in keys_of(db)
    complete(db, ":promptreview:3")

    write_report(root, "APPROVE", recon="1111111")      # та же редакция постановки
    started = gate.run_gate(p, execute=True, notify_fn=quiet)
    assert started["decision"]["action"] == "create_step", started["decision"]
    assert "planloop:1:S1:step" in keys_of(db)


def test_prompt_fix_rounds_exhausted_stops(stand):
    """Третий REQUEST_CHANGES постановки — стоп: круги автопереписывания исчерпаны.

    Отчёт пишется ПОСЛЕ карточки ревью, как это делает ревьюер: страж сверяет, что отчёт
    переписан в этом круге (`_report_written_after`, допуск 1 с). Отчёт, записанный до
    создания карточки, на медленной машине валит тик стопом «вердикт может быть от прошлого
    круга», и тест краснел не по существу.
    """
    root, p, db = stand
    write_plan(root, plan_body(3))
    write_prompt(root, "S1", recon="1111111")
    gate.run_gate(p, execute=True, notify_fn=quiet)     # карточка ревью круга 1
    write_report(root, "REQUEST_CHANGES")
    complete(db, ":promptreview")

    gate.run_gate(p, execute=True, notify_fn=quiet)     # круг 1: переписать
    complete(db, ":promptfix:1")
    gate.run_gate(p, execute=True, notify_fn=quiet)     # ревью круга 2
    write_report(root, "REQUEST_CHANGES")               # ревьюер переписал отчёт
    complete(db, ":promptreview:2")

    gate.run_gate(p, execute=True, notify_fn=quiet)     # круг 2: переписать
    complete(db, ":promptfix:2")
    gate.run_gate(p, execute=True, notify_fn=quiet)     # ревью круга 3
    write_report(root, "REQUEST_CHANGES")
    complete(db, ":promptreview:3")

    stopped = gate.run_gate(p, execute=True, notify_fn=quiet)
    assert stopped["decision"]["action"] == "stop", stopped["decision"]
    assert "исчерпаны" in stopped["detail"]
    assert "planloop:1:S1:step" not in keys_of(db)
    assert "planloop:1:S1:promptfix:3" not in keys_of(db)


def test_prompt_review_modes(stand):
    """`always` ревьюит свежую постановку, `never` пропускает заведомо устаревшую.

    Режимы проверяются так, чтобы каждый отличался от `auto`: `always` — на свежей
    постановке (её `auto` ревью не требует), `never` — на устаревшей (`recon_commit:
    1111111`, §4.7 п.10: без ветки `never` тут остался бы `auto` с ревью).
    """
    root, p, db = stand
    write_plan(root, plan_body(3))
    head = init_repo(root)
    write_prompt(root, "S1", recon=head)                # постановка свежая

    set_config(root, prompt_review="always")
    always = gate.run_gate(paths(root), execute=True, notify_fn=quiet)
    assert always["decision"]["action"] == "create_prompt_review", always["decision"]
    assert sum(1 for c in cards(db) if c["key"] == "planloop:1:S1:promptreview") == 1

    set_config(root, prompt_review="never")
    write_prompt(root, "S1", recon="1111111")           # устаревшая (§4.7 п.10)
    never = gate.run_gate(paths(root), execute=True, notify_fn=quiet)
    assert never["decision"]["action"] == "create_step", never["decision"]
    assert "planloop:1:S1:step" in keys_of(db)
    # `never` новых ревью не заказывает: карточка, открытая в `always`, не считается
    assert sum(1 for c in cards(db) if c["key"] == "planloop:1:S1:promptreview") == 1

    # Файл без новых ключей читается на умолчаниях (старые проекты не ломаются).
    set_config(root, prefetch_prompt=None, prompt_review=None)
    defaults = config_mod.load(root)
    assert defaults.prefetch_prompt is True
    assert defaults.prompt_review == "auto"

    set_config(root, prompt_review="sometimes")
    with pytest.raises(config_mod.ConfigError) as exc:
        config_mod.load(root)
    assert "prompt_review" in str(exc.value)


# --- Адрес факта в постановке: символ и команда вместо `файл:строка` (план 8, §S2A) ---

STEP = {"id": "S1", "epoch": "", "title": "таблица счетов", "days": 1.0, "owner": "",
        "goal": "таблица счетов", "files": ["app/s1.py"],
        "gates": ["python -m pytest tests/test_s1.py -q"], "dod": "сделано",
        "read": [], "notes": ""}

PROMPT_REL = "docs/prompts/2026-11-04_1-S1_prompt.md"
REPORT_REL = "docs/reviews/1-S1-prompt.md"


def prompt_cards(root: Path, p) -> dict[str, str]:
    """Тела трёх карточек постановки: ревью, переписывание, пересборка протухшей."""
    (root / "docs" / "reviews").mkdir(parents=True, exist_ok=True)
    (root / REPORT_REL).write_text(
        "Находки\n\n- [блокирующая] раздел 4 — символ `Invoice.total` не находится в коде\n",
        encoding="utf-8")
    decision = {"rebuild": 1, "limit": 2, "prev_recon": "1111111", "review_round": 2,
                "reason": "постановка протухла: вход изменился после ревью"}
    return {
        "ревью постановки": gate.prompt_review_card_body(p, STEP, REPORT_REL, PROMPT_REL),
        "переписывание": gate.prompt_fix_card_body(p, STEP, 1, REPORT_REL),
        "пересборка": gate.prompt_stale_card_body(p, STEP, decision, REPORT_REL),
    }


def test_prompt_review_card_drops_line_addresses(stand):
    """Ни одна карточка постановки не учит паре «файл и номер строки»."""
    root, p, _db = stand
    write_plan(root, plan_body(3))
    write_prompt(root, "S1", recon="1111111")
    for name, body in prompt_cards(root, p).items():
        assert "path:line" not in body, f"карточка «{name}» всё ещё требует path:line:\n{body}"


def test_symbols_block_but_line_shift_does_not(stand):
    """Символ проверяется и блокирует, сдвиг строки — нет: оба правила в «ЧТО ПРОВЕРИТЬ».

    Место правила важно: пункт, уехавший в хвост карточки, ревьюер прочтёт после вердикта.
    Там же проверяется реестр: фактом закрытия служит адрес-символ, а не пара
    «файл и номер строки».
    """
    root, p, _db = stand
    write_plan(root, plan_body(3))
    write_prompt(root, "S1", recon="1111111")
    body = prompt_cards(root, p)["ревью постановки"]

    head = "ЧТО ПРОВЕРИТЬ"
    tail = "РЕЕСТР НАХОДОК ПОСТАНОВКИ (обязательно)"
    assert head in body and tail in body, body
    checks = " ".join(body.split(head, 1)[1].split(tail, 1)[0].split())
    assert "Несуществующий символ" in checks, "символ не назван блокирующей находкой"
    assert "блокирующая находка" in checks, checks
    assert "сдвиг строки находкой не считается" in checks, "сдвиг строки не исключён из находок"

    registry = " ".join(body.split(tail, 1)[1].split("РЕЕСТР СЕЙЧАС", 1)[0].split())
    assert "`файл.py::символ`" in registry, "реестр не называет адрес-символ"
    assert "файл:строка" not in registry, "реестр всё ещё требует пару «файл и номер строки»"


PROMPT_SYMBOLS = """# Промт: 1 / S1 — таблица счетов

## 1. Роль и цель

Ты — бэкенд-разработчик. Результат: модель Invoice с полем total и её миграция.

## 2. Контекст (факты)

- Репозиторий: `demo`, ветка `main`, HEAD `abc1234`.
- Сумма счёта — `app/invoice.py::Invoice.total`; найти: `grep -n 'def total' app/invoice.py`.
- Ключ конфига — `app/config.py::Settings.currency`; найти: `grep -n 'currency' app/config.py`.
- План: `docs/plans/1-demo.md` → раздел S1. Журнал шага: не начат.

## 3. Границы и запреты

Можно менять: `app/invoice.py`, `tests/test_invoice.py`.
Запрещено: трогать существующие модели, править данные приложения.
Не входит в задачу: API-эндпоинт и сериализаторы.

## 4. Что сделать

1. `app/invoice.py::Invoice` — добавить поле `total` → в таблице есть колонка.
2. `app/invoice.py::Invoice.total` — проверка неотрицательности → минус отвергается.
3. Тесты: `tests/test_invoice.py::test_total` — happy path, граница (ноль), отрицательное.

Решения, принятые за тебя (не переигрывать): сумма хранится в копейках целым числом.

## 5. Как проверить (гейты)

```
python -m pytest tests/test_invoice.py -m "not slow"   # ожидается: 3 passed, EXIT=0
```

Вывод писать в файл, код возврата смотреть у pytest.

## 6. Стоп-сигналы

- нужна миграция с NOT NULL или drop;
- факт в коде расходится с этим промтом;
- гейт красный по посторонней причине.

## 7. Формат отчёта

`diff stat` → диффы → решения → строки гейтов (команда + число + EXIT) → вопросы.

## 8. Вопросы

Нет вопросов: решения записаны в блоке 4.

## 9. Ревью (обязательно)

Шаг закрывает роль `reviewer` по плану и коду; вердикт APPROVE или REQUEST_CHANGES.
"""


def test_prompt_without_line_numbers_passes_form_gate():
    """Гейт формы нейтрален к виду адреса: обе редакции дают пустой список FAIL.

    Правило «символ, а не строка» держится скиллом и ревью, а не формой постановки; тест
    фиксирует эту нейтральность, а не маскирует её. Адреса в обеих редакциях литеральные:
    запись в угловых скобках уронила бы текст по C3 (`PLACEHOLDER_ANGLE`) и проверяла бы не то.
    """
    with_line = PROMPT_SYMBOLS.replace("`app/invoice.py::Invoice.total`",
                                       "`app/models/invoice.py:42`")
    assert "app/models/invoice.py:42" in with_line, "подмена адреса не сработала"
    for text in (PROMPT_SYMBOLS, with_line):
        failed = [(code, msg) for level, code, msg in ctk.check_prompt(text)
                  if level == "FAIL"]
        assert not failed, failed

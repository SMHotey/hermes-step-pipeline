# -*- coding: utf-8 -*-
"""Черновики и утверждение плана: `hermes pipeline plan --list|--check|--accept`.

Путь «черновик → проверка → утверждение»: агент пишет план в `docs/plans/drafts/`, движок
его там не видит, а переносит файл в каталог планов только владелец. Вместе с переносом
появляется запись `plan_<N>_approval.json` с sha256 — её читает страж (шаг S7), поэтому
тесты проверяют и файл, и запись, и то, что повторное утверждение не сдвигает дату.

Обработчик зовётся напрямую (`cli.cmd_plan`), а не через `cli.run`: `run` переключает
настоящие потоки через `proj.setup_stdio()`, и `capsys` становится бесполезен (так же
сделано в `tests/test_cli_plan_mode.py`). Запасной путь проверяется подменой
`plan_accept.plan_lint` на `None` — это единственная точка ветвления.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from argparse import Namespace
from pathlib import Path

import helpers  # noqa: F401  (загружает плагин и алиасы)
import cli
from step_pipeline import plan_accept

# Шаг S1 без пункта «Файл» и с пустыми гейтами: запасной путь обязан это увидеть.
BROKEN_STEP = """# 5. Шаг без файла

## Шаги

### S1. Шаг без файла (1 дн)

- Тесты/гейты: —
- Готово = шаг всё равно считается готовым

## Журнал выполнения

| Дата | Шаг | Коммиты | Прогоны и доказательства | Статус |
|---|---|---|---|---|
| — | S1 | — | — | открыт |
"""

# Журнал: строка из четырёх колонок и повтор шага S1.
BROKEN_JOURNAL = """# 5. Журнал с ошибками

## Шаги

### S1. Нормальный шаг (1 дн)

- Файл: `app/x.py`
- Тесты/гейты: `python -m pytest tests/test_x.py` → 1 passed
- Готово = тест зелёный

## Журнал выполнения

| Дата | Шаг | Коммиты | Прогоны и доказательства | Статус |
|---|---|---|---|---|
| — | S1 | — | — | открыт |
| — | S1 | — | — | открыт |
| — | S1 | — | мало колонок |
"""

# Заголовок шага без точки после идентификатора: такую ошибку даёт только линтер.
BAD_HEADING = """# 5. Заголовок не по формату

## Шаги

### S1 Название без точки

- Файл: `app/x.py`
- Тесты/гейты: `python -m pytest` → 1 passed
- Готово = тест зелёный

## Журнал выполнения

| Дата | Шаг | Коммиты | Прогоны и доказательства | Статус |
|---|---|---|---|---|
| — | S1 | — | — | открыт |
"""

# Ни журнала, ни контракта шага: утверждать нечего.
NO_JOURNAL = """# 5. Совсем без журнала

## Шаги

### S1. Шаг (1 дн)

- Файл: `app/x.py`
"""


def _draft(project: Path, name: str, text: str) -> Path:
    """Положить черновик в `docs/plans/drafts` и вернуть путь."""
    folder = plan_accept.drafts_dir(project)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_text(text, encoding="utf-8")
    return path


def _engine_approval():
    """Модуль движка: версия плана (файл без журнала) считается там же, где у стража."""
    from step_pipeline.engine import approval as engine_approval
    return engine_approval


def _names(project: Path) -> list[str]:
    """Имена файлов каталога планов (черновики сюда не попадают)."""
    return sorted(path.name for path in plan_accept.plans_dir(project).glob("*.md"))


def _cmd(project: Path, *, listing: bool = False, check: str = "", accept: str = "") -> int:
    """Вызвать обработчик так, как его позвал бы `hermes pipeline plan ...`."""
    return cli.cmd_plan(Namespace(list=listing, check=check, accept=accept,
                                  project=str(project)))


def test_list_shows_drafts_and_approved_plans_with_numbers(project: Path, capsys):
    """--list печатает оба раздела с номерами из имён файлов, код 0."""
    _draft(project, "5-billing.md", "# 5. Счета\n")

    assert _cmd(project, listing=True) == 0
    captured = capsys.readouterr()
    assert "Черновики (docs/plans/drafts):" in captured.out
    assert "5  5-billing.md" in captured.out
    assert "Утверждённые планы (docs/plans):" in captured.out
    assert "1  1-demo.md" in captured.out
    assert captured.err == ""

    parser = argparse.ArgumentParser(prog="hermes pipeline")
    cli.build_parser(parser)
    args = parser.parse_args(["plan", "--list"])
    assert args.cmd == "plan"
    assert args.list is True
    assert args.handler is cli.cmd_plan
    assert args.project == ""


def test_list_without_drafts_dir_reports_none(project: Path, capsys):
    """Нет каталога черновиков — «Черновики: нет» с путём, утверждённые перечислены."""
    assert plan_accept.drafts_dir(project).exists() is False

    assert _cmd(project, listing=True) == 0
    captured = capsys.readouterr()
    assert "Черновики: нет" in captured.out
    assert "docs/plans/drafts" in captured.out
    assert "1-demo.md" in captured.out
    assert plan_accept.drafts_dir(project).exists() is False


def test_check_prints_stale_address_as_warning(project: Path, capsys):
    """`plan --check` показывает уехавший адрес предупреждением и не мешает утверждению.

    0.25.6: адресную проверку сняли с ревьюера и отдали линтеру, но стоп она не держит —
    иначе номер строки снова стал бы способом остановить план.
    """
    (project / "app" / "api").mkdir(parents=True, exist_ok=True)
    (project / "app" / "api" / "invoices.py").write_text("x = 1\n", encoding="utf-8")
    draft = _draft(project, "5-demo.md",
                   helpers.PLAN.replace("`app/api/invoices.py`", "`app/api/invoices.py:1-999`"))

    assert plan_accept.check_draft(project, draft) == []
    assert _cmd(project, check=str(draft)) == 0
    captured = capsys.readouterr()
    assert "уехал" in captured.out
    assert "ИТОГ: ошибок нет, предупреждений" in captured.out


def test_check_without_lint_accepts_valid_draft(project: Path, capsys, monkeypatch):
    """Без линтера валидный черновик проходит: разбор движком и контракт шагов чисты."""
    monkeypatch.setattr(plan_accept, "plan_lint", None)
    draft = _draft(project, "5-demo.md", helpers.PLAN)
    before = draft.read_bytes()

    assert plan_accept.check_draft(project, draft) == []

    assert _cmd(project, check=str(draft)) == 0
    captured = capsys.readouterr()
    assert "ИТОГ: ошибок нет" in captured.out
    assert draft.read_bytes() == before
    assert plan_accept.approval_file(project, "5").exists() is False


def test_check_without_lint_reports_missing_file_and_gates(project: Path, monkeypatch):
    """Без линтера видны пустые пункты «Файл» и «Тесты/гейты» (прочерк — пусто)."""
    monkeypatch.setattr(plan_accept, "plan_lint", None)
    draft = _draft(project, "5-nofile.md", BROKEN_STEP)

    errors = plan_accept.check_draft(project, draft)
    joined = "\n".join(errors)
    assert "шаг S1" in joined
    assert "«Файл»" in joined
    assert "«Тесты/гейты»" in joined


def test_check_without_lint_reports_bad_journal(project: Path, monkeypatch):
    """Без линтера журнал проверяется сам: колонки, шаг во второй, повторы."""
    monkeypatch.setattr(plan_accept, "plan_lint", None)
    draft = _draft(project, "5-journal.md", BROKEN_JOURNAL)

    errors = plan_accept.check_draft(project, draft)
    joined = "\n".join(errors)
    assert "без 5 колонок" in joined
    assert "повторяется в журнале" in joined


def test_check_with_lint_reports_heading_format(project: Path, capsys):
    """С настоящим линтером видна ошибка формата заголовка — значит, вызов делегирован."""
    draft = _draft(project, "5-bad.md", BAD_HEADING)

    assert _cmd(project, check=str(draft)) == 1
    captured = capsys.readouterr()
    assert "заголовок шага не по формату" in captured.out
    assert "ИТОГ: ошибок" in captured.out


def test_accept_refuses_draft_with_errors_and_missing_number(project: Path, capsys):
    """С ошибками accept ничего не переносит; без черновика — код 1 и причина."""
    draft = _draft(project, "5-bad.md", NO_JOURNAL)

    assert _cmd(project, accept="5") == 1
    captured = capsys.readouterr()
    assert "ИТОГ: ошибок" in captured.out
    assert draft.exists() is True
    assert "5-bad.md" not in _names(project)
    assert plan_accept.approval_file(project, "5").exists() is False

    assert _cmd(project, accept="7") == 1
    captured = capsys.readouterr()
    assert "черновика с номером 7 нет" in captured.err


def test_accept_moves_draft_and_writes_sha256_record(project: Path, capsys, monkeypatch):
    """Успешный accept переносит файл и пишет запись с хэшем содержимого."""
    monkeypatch.setenv("HERMES_PROFILE", "prompter")
    monkeypatch.setattr(plan_accept, "now_iso", lambda: "2026-01-01T00:00:00")
    draft = _draft(project, "5-demo.md", helpers.PLAN)
    before = draft.read_bytes()

    assert _cmd(project, accept="5") == 0
    captured = capsys.readouterr()
    assert "утверждён" in captured.out
    assert draft.exists() is False

    final = project / "docs" / "plans" / "5-demo.md"
    assert final.is_file()
    assert final.read_bytes() == before

    record = json.loads(plan_accept.approval_file(project, "5").read_text(encoding="utf-8"))
    assert record["sha256"] == hashlib.sha256(final.read_bytes()).hexdigest()
    # 0.22.1: вместе с сырым хэшем пишется sha256 версии плана (файл без содержимого
    # журнала) — по нему страж сверяет утверждение, чтобы закрытие шага его не сбрасывало.
    engine_approval = _engine_approval()
    assert record["version_sha256"] == engine_approval.plan_version_sha(final)
    assert record["version_sha256"] != record["sha256"]
    assert record["approved_at"] == "2026-01-01T00:00:00"
    assert record["profile"] == "prompter"
    assert record["file"] == "docs/plans/5-demo.md"
    assert record["plan"] == "5"


def test_accept_reapproves_live_plan_when_stale_draft_differs(project: Path, capsys,
                                                              monkeypatch):
    """`--accept N` переутверждает действующий план, даже если рядом лежит старый черновик.

    Именно эту команду просит страж после правки плана по разведке (0.20.0); отказ
    «файл уже есть и отличается содержимым» запирал цикл: план правлен карточкой,
    а переутвердить его владелец не мог.
    """
    monkeypatch.setattr(plan_accept, "now_iso", lambda: "2026-01-01T00:00:00")
    _draft(project, "5-demo.md", helpers.PLAN)
    assert _cmd(project, accept="5") == 0
    capsys.readouterr()
    final = project / "docs" / "plans" / "5-demo.md"
    final.write_text(helpers.PLAN + "\nПравки по разведке: 1\n", encoding="utf-8")
    stale = _draft(project, "5-demo.md", helpers.PLAN)
    before = stale.read_bytes()

    monkeypatch.setattr(plan_accept, "now_iso", lambda: "2026-02-02T00:00:00")
    assert _cmd(project, accept="5") == 0
    captured = capsys.readouterr()
    assert "переутверждён" in captured.out

    record = json.loads(plan_accept.approval_file(project, "5").read_text(encoding="utf-8"))
    assert record["sha256"] == hashlib.sha256(final.read_bytes()).hexdigest()
    assert record["approved_at"] == "2026-02-02T00:00:00"
    # черновик не переносится и план не перезаписывает
    assert stale.exists() is True and stale.read_bytes() == before
    assert final.read_text(encoding="utf-8").endswith("Правки по разведке: 1\n")


def test_second_accept_keeps_first_approval_date(project: Path, capsys, monkeypatch):
    """Повторный accept того же содержимого дату первого утверждения не переписывает."""
    monkeypatch.setattr(plan_accept, "now_iso", lambda: "2026-01-01T00:00:00")
    _draft(project, "5-demo.md", helpers.PLAN)
    assert _cmd(project, accept="5") == 0
    capsys.readouterr()

    monkeypatch.setattr(plan_accept, "now_iso", lambda: "2026-02-02T00:00:00")
    assert _cmd(project, accept="5") == 0
    captured = capsys.readouterr()
    assert "уже утверждён" in captured.out

    record = json.loads(plan_accept.approval_file(project, "5").read_text(encoding="utf-8"))
    assert record["approved_at"] == "2026-01-01T00:00:00"


def test_accept_rewrites_record_inherited_by_gate(project: Path, capsys, monkeypatch):
    """--accept перебивает запись, унаследованную стражем, и начинает бюджет заново.

    `gate.inherit_approval` подтверждает новое содержимое плана молча, без решения
    владельца, и сохраняет прежнюю метку `review_reset_at`. Явный `--accept` по такой
    записи отвечал «уже утверждён», метка не обновлялась, и при исчерпанном лимите кругов
    ревью владелец не мог разблокировать план вообще.
    """
    monkeypatch.setattr(plan_accept, "now_iso", lambda: "2026-01-01T00:00:00")
    _draft(project, "5-demo.md", helpers.PLAN)
    assert _cmd(project, accept="5") == 0
    capsys.readouterr()

    path = plan_accept.approval_file(project, "5")
    record = json.loads(path.read_text(encoding="utf-8"))
    record.update({"inherited": True, "review_reset_at": 1, "profile": "default"})
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")

    monkeypatch.setattr(plan_accept, "now_iso", lambda: "2026-02-02T00:00:00")
    monkeypatch.setattr(plan_accept.time, "time", lambda: 1794489999)
    assert _cmd(project, accept="5") == 0
    captured = capsys.readouterr()
    assert "переутверждён владельцем" in captured.out

    again = json.loads(path.read_text(encoding="utf-8"))
    assert again["approved_at"] == "2026-02-02T00:00:00"
    assert again["review_reset_at"] == 1794489999
    assert "inherited" not in again


def test_list_drafts_marks_approved_draft(project: Path, monkeypatch):
    """Черновик утверждённого плана виден панели как «утверждён», а не вечный «черновик».

    `_accept_existing` не переносит черновик (действует файл в каталоге планов), поэтому
    без признака `approved` кнопка «Утвердить» на панели висела всегда и ничего не меняла.
    """
    monkeypatch.setattr(plan_accept, "now_iso", lambda: "2026-01-01T00:00:00")
    _draft(project, "5-demo.md", helpers.PLAN)
    assert _cmd(project, accept="5") == 0
    # Планировщик пишет черновик заново уже после утверждения — так он и остаётся в
    # `drafts/` рядом с утверждённым планом (случай gapaudit).
    _draft(project, "5-demo.md", helpers.PLAN)

    rows = plan_accept.list_drafts(project)
    row = next(item for item in rows if item["id"] == "5")
    assert row["approved"] is True
    assert row["approved_at"] == "2026-01-01T00:00:00"
    assert row["changed"] is False
    assert row["draft_differs"] is False


def test_list_drafts_sees_changed_plan(project: Path, monkeypatch):
    """План правили после утверждения: черновик остаётся, но помечен «изменён»."""
    monkeypatch.setattr(plan_accept, "now_iso", lambda: "2026-01-01T00:00:00")
    _draft(project, "5-demo.md", helpers.PLAN)
    assert _cmd(project, accept="5") == 0
    final = project / "docs" / "plans" / "5-demo.md"
    final.write_text(helpers.PLAN + "\n- правка после утверждения\n", encoding="utf-8")
    _draft(project, "5-demo.md", helpers.PLAN)

    row = next(item for item in plan_accept.list_drafts(project) if item["id"] == "5")
    assert row["approved"] is False
    assert row["changed"] is True
    assert row["draft_differs"] is True


def test_list_drafts_keeps_approved_after_journal_line(project: Path, monkeypatch):
    """Строка журнала (закрытие шага) — не правка плана: черновик остаётся «утверждён».

    Страж сверяет утверждение по версии плана (`version_sha256`, содержимое журнала
    исключено), а сверка панели шла по сырым байтам: после первого закрытого шага строка
    черновика возвращалась в «изменён после утверждения», и один план был виден дважды —
    «утверждён» в списке планов и «изменён» в черновиках (gapaudit, 12.11.2026).
    """
    monkeypatch.setattr(plan_accept, "now_iso", lambda: "2026-01-01T00:00:00")
    _draft(project, "5-demo.md", helpers.PLAN)
    assert _cmd(project, accept="5") == 0
    final = project / "docs" / "plans" / "5-demo.md"
    # Закрытие шага: строка таблицы внутри раздела «## Журнал выполнения».
    before = final.read_text(encoding="utf-8")
    after = before.replace("| — | S1 | — | — | открыт |",
                           "| 01.01 | S1 | abc1234 | прогон | закрыт |")
    assert after != before                      # замена состоялась, иначе тест пустой
    final.write_text(after, encoding="utf-8")
    _draft(project, "5-demo.md", helpers.PLAN)

    row = next(item for item in plan_accept.list_drafts(project) if item["id"] == "5")
    assert row["approved"] is True
    assert row["changed"] is False

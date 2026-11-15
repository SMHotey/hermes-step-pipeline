# -*- coding: utf-8 -*-
"""Реестр находок постановки (план 12, S2): вердикт по закрытому списку.

До шага «круги исчерпаны» был счётчиком: каждая новая находка ревьюера стоила круга,
даже если она повторяла уже принятую. Здесь проверяется, что вердикт ревью постановки
ставится по реестру `prompt-<N>-<шаг>-findings.md` — как для плана в 7-S1/S2, — и что
порядок ветвей жёсткий: свежесть (S1) → реестр (S2) → бюджет кругов.

Тесты держат две вещи: разбор реестра (шапка, статусы, сквозная нумерация) и поведение
стража — конфликт «отчёт против реестра», битый и пропавший реестр, правило «0 закрытых».
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import config as config_mod
import gate
from engine_kit import cards, complete, write_approval

FINDINGS_REL = "docs/reviews/prompt-1-S1-findings.md"
REPORT_REL = "docs/reviews/1-S1-prompt.md"
HEADER = "| ID | Класс | Находка | Что сделать | Статус |"

STEP = {"id": "S1", "epoch": "", "title": "Шаг номер 1", "days": 1.0, "owner": "",
        "goal": "g", "files": [], "gates": [], "dod": "d", "read": [], "notes": ""}


def plan_body(count: int = 3) -> str:
    """План из `count` шагов с журналом (все строки «открыт»)."""
    parts = ["# 1. Демо-план конвейера", "", "Цель: проверить реестр постановки.", "",
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
    """Отчёт ревью постановки: отметка версии + одна строка вердикта."""
    path = Path(root) / REPORT_REL
    head = f"recon_commit: {recon}\n" if recon else ""
    path.write_text(f"{head}\n## ВЕРДИКТ: {verdict}\n\nпроверено\n", encoding="utf-8")
    return path


def write_findings(root: Path, rows: list[tuple[str, str, str, str, str]],
                   step: str = "S1") -> Path:
    """Реестр постановки готовой таблицей: ID, класс, находка, что сделать, статус."""
    path = Path(root) / "docs" / "reviews" / f"prompt-1-{step}-findings.md"
    lines = [f"# Реестр находок постановки 1-{step}", "", HEADER, "|---|---|---|---|---|"]
    lines += ["| " + " | ".join(row) + " |" for row in rows]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def open_row(num: int = 1, text: str = "тест 3 не изолирует профиль") -> tuple:
    """Строка реестра со статусом `открыто`."""
    return (f"F{num}", "гейт-исполнение", text, "выделить каталог профиля", "открыто")


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


def spy_cards(monkeypatch) -> list[tuple[str, str]]:
    """Перехват тел карточек: `run_gate(create_card=…)` пишет ту же карточку на борду.

    Тело в поддельной борде не хранится (у неё колонки настоящего Hermes без описания),
    а проверять надо именно текст, который увидит ревьюер.
    """
    seen: list[tuple[str, str]] = []

    def spy(p, title, body, *args, **kwargs):
        seen.append((title, body))
        return gate._create_card(p, title, body, *args, **kwargs)  # noqa: SLF001 - стенд теста

    return seen, spy


def first_fix(stand, rows: list[tuple]) -> tuple[Path, object, Path]:
    """Довести стенд до состояния «круг правок 1 заказан и закрыт».

    Постановка устаревшая (`recon_commit` не равен HEAD и git нет) — ревью заказывается
    в режиме `auto`, реестр открыт, отчёт REQUEST_CHANGES.
    """
    root, p, db = stand
    write_plan(root, plan_body(3))
    write_prompt(root, "S1", recon="1111111")
    write_findings(root, rows)
    res = gate.run_gate(p, execute=True, notify_fn=quiet)
    assert res["decision"]["action"] == "create_prompt_review", res["decision"]
    complete(db, ":promptreview")
    write_report(root, "REQUEST_CHANGES")
    res = gate.run_gate(p, execute=True, notify_fn=quiet)
    assert res["decision"]["action"] == "create_prompt_fix", res["decision"]
    assert not res["decision"].get("stale"), "круг правок не должен выглядеть пересборкой"
    complete(db, ":promptfix:1")
    return root, p, db


def second_review(root: Path, p, db: Path, verdict: str = "REQUEST_CHANGES") -> dict:
    """Заказать и закрыть ревью круга 2, вернуть решение следующего тика."""
    res = gate.run_gate(p, execute=True, notify_fn=quiet)
    assert res["decision"]["action"] == "create_prompt_review", res["decision"]
    assert res["decision"]["round"] == 2, res["decision"]
    close_second_review(root, db, verdict)
    return gate.run_gate(p, execute=True, notify_fn=quiet)


def close_second_review(root: Path, db: Path, verdict: str = "REQUEST_CHANGES") -> None:
    """Закрыть уже заказанное ревью круга 2 и переписать отчёт (свежий mtime)."""
    complete(db, ":promptreview:2")
    write_report(root, verdict)


def ctx_with(report: str | None, rows: list[dict], exists: bool = True,
             error: str = "") -> dict:
    """Синтетический `ctx` решения: отчёт ревью + реестр постановки."""
    return {"prompt_verdict": ({"verdict": report, "path": REPORT_REL, "recon": "1111111"}
                               if report else None),
            "prompt_findings": rows, "prompt_findings_exists": exists,
            "prompt_findings_error": error}


# ───────────────────────────── реестр как файл ─────────────────────────────

def test_registry_path_and_title(project):
    """Путь реестра постановки строится из префикса и шага, заголовок — из имени файла."""
    p = paths(project)
    assert gate.prompt_findings_rel(p, "S2") == "docs/reviews/prompt-1-S2-findings.md"
    assert gate.prompt_findings_rel(p, "S1") == FINDINGS_REL
    path = gate.prompt_findings_path(p, "S2")
    assert path == Path(p.review_dir) / "prompt-1-S2-findings.md"

    gate.write_findings(path, [{"id": "F1", "kind": "замысел", "text": "шаг без входа",
                                "action": "добавить вход", "status": "открыто"}],
                        gate.PROMPT_FINDINGS_HEADER, gate.PROMPT_FINDINGS_KEYS,
                        "постановки", "prompt-")
    text = path.read_text(encoding="utf-8")
    assert text.splitlines()[0] == "# Реестр находок постановки 1-S2", text
    assert HEADER in text, "шапка реестра постановки должна быть без колонки «Шаг»"
    loaded = gate.load_findings(path, gate.PROMPT_FINDINGS_HEADER, gate.PROMPT_FINDINGS_KEYS)
    assert loaded == [{"id": "F1", "kind": "замысел", "text": "шаг без входа",
                       "action": "добавить вход", "status": "открыто"}]


def test_registry_header_and_parse_errors(project):
    """Шапка не та, ячеек не пять, дубль ID, чужой статус или класс — `GateError`."""
    p = paths(project)
    path = gate.prompt_findings_path(p, "S1")
    path.parent.mkdir(parents=True, exist_ok=True)

    # Шапка плана (шесть колонок) в реестре постановки — не реестр постановки.
    path.write_text("# Реестр\n\n| ID | Шаг | Класс | Находка | Что сделать | Статус |\n"
                    "|---|---|---|---|---|---|\n"
                    "| F1 | S1 | замысел | т | д | открыто |\n", encoding="utf-8")
    with pytest.raises(gate.GateError) as exc:
        gate.load_findings(path, gate.PROMPT_FINDINGS_HEADER, gate.PROMPT_FINDINGS_KEYS)
    assert str(path) in str(exc.value), "в ошибке должен быть путь файла"

    write_findings(project, [("F1", "замысел", "т", "д", "открыто", "лишнее")])
    with pytest.raises(gate.GateError) as exc:
        gate.load_findings(path, gate.PROMPT_FINDINGS_HEADER, gate.PROMPT_FINDINGS_KEYS)
    assert "ячеек 6" in str(exc.value), str(exc.value)

    write_findings(project, [open_row(1), open_row(1)])
    with pytest.raises(gate.GateError) as exc:
        gate.load_findings(path, gate.PROMPT_FINDINGS_HEADER, gate.PROMPT_FINDINGS_KEYS)
    assert "F1" in str(exc.value), "дубль ID обязан называться в ошибке"

    write_findings(project, [("F1", "замысел", "т", "д", "принято")])
    with pytest.raises(gate.GateError) as exc:
        gate.load_findings(path, gate.PROMPT_FINDINGS_HEADER, gate.PROMPT_FINDINGS_KEYS)
    assert "статус" in str(exc.value)

    write_findings(project, [("F1", "хотелка", "т", "д", "открыто")])
    with pytest.raises(gate.GateError) as exc:
        gate.load_findings(path, gate.PROMPT_FINDINGS_HEADER, gate.PROMPT_FINDINGS_KEYS)
    assert "класс" in str(exc.value)

    # Реестр плана читается прежними вызовами: обобщение шапки его не сломало.
    plan_path = gate.plan_findings_path(p)
    gate.add_findings(plan_path, [{"step": "S1", "kind": "замысел", "text": "т",
                                   "action": "д", "status": "открыто"}])
    assert gate.load_findings(plan_path)[0]["step"] == "S1"
    assert gate.load_findings(plan_path)[0]["id"] == "F1"


# ───────────────────────────── действующий вердикт ─────────────────────────────

def test_effective_verdict_by_registry():
    """Вердикт считается по реестру: открытая находка сильнее APPROVE отчёта."""
    open_row_ctx = [{"id": "F1", "status": "открыто"}]
    closed = [{"id": "F1", "status": "закрыто"}, {"id": "F2", "status": "не блокирует"}]

    view = gate.effective_prompt_verdict(ctx_with("APPROVE", open_row_ctx))
    assert view["verdict"] == "REQUEST_CHANGES" and view["report"] == "APPROVE"
    assert view["open"] == ["F1"] and view["total"] == 1
    assert view["discrepancy"] is True, "расхождение с отчётом обязано быть видно"

    view = gate.effective_prompt_verdict(ctx_with("REQUEST_CHANGES", closed))
    assert view["verdict"] == "APPROVE" and view["discrepancy"] is True
    assert view["open"] == []

    # Отчёта нет — вердикта нет: реестр его не выдумывает.
    assert gate.effective_prompt_verdict(ctx_with(None, open_row_ctx)) is None

    # Файла реестра нет — переопределять нечего, действует отчёт.
    view = gate.effective_prompt_verdict(ctx_with("REQUEST_CHANGES", [], exists=False))
    assert view["verdict"] == "REQUEST_CHANGES" and view["registry"] is False

    # Нераспознанный вердикт реестром не «лечится».
    view = gate.effective_prompt_verdict(ctx_with("AMBIGUOUS", closed))
    assert view["verdict"] == "AMBIGUOUS" and view["discrepancy"] is False

    # Битый реестр уходит строкой ошибки, вердикт не подменяется.
    view = gate.effective_prompt_verdict(ctx_with("APPROVE", [], exists=False, error="битый"))
    assert view["error"] == "битый" and view["verdict"] == "APPROVE"


# ───────────────────────────── живые тики ─────────────────────────────

def test_conflict_falls_through_to_registry(stand, monkeypatch):
    """Отчёт APPROVE + открытая находка: круг правок, а не старт шага.

    И в теле карточки названы и открытый ID, и предел кругов из `max_fix_rounds`.
    """
    root, p, db = stand
    seen, spy = spy_cards(monkeypatch)
    write_plan(root, plan_body(3))
    write_prompt(root, "S1", recon="1111111")
    write_findings(root, [open_row(1)])
    res = gate.run_gate(p, execute=True, notify_fn=quiet, create_card=spy)
    assert res["decision"]["action"] == "create_prompt_review", res["decision"]
    complete(db, ":promptreview")
    write_report(root, "APPROVE")

    res = gate.run_gate(p, execute=True, notify_fn=quiet, create_card=spy)
    assert res["decision"]["action"] == "create_prompt_fix", res["decision"]
    assert "planloop:1:S1:step" not in keys_of(db), "шаг не должен стартовать по отчёту"
    assert gate.load_stop(p) is None

    title, body = [item for item in seen if "переписать постановку" in item[0]][-1]
    assert "F1: тест 3 не изолирует профиль" in body, body
    assert "(круг 1 из 2)" in body, "предел кругов из конфигурации не попал в карточку"


def test_conflict_same_commit_falls_through(stand):
    """Отметки разные текстом, но один коммит — вердикт относится к этой редакции.

    Раньше такой отчёт уходил в пересборку; после S2 — к реестру, иначе открытая
    находка прошла бы мимо вердикта.
    """
    root, p, db = stand
    write_plan(root, plan_body(3))
    write_prompt(root, "S1", recon="1111111")
    head = init_repo(root)
    write_prompt(root, "S1", recon=head[:7])            # короткая отметка в постановке
    git(root, "add", "-A")
    git(root, "commit", "-m", "отметка версии")
    write_findings(root, [open_row(1)])
    set_config(root, prompt_review="always")            # ревью заказывается и на свежей

    res = gate.run_gate(paths(root), execute=True, notify_fn=quiet)
    assert res["decision"]["action"] == "create_prompt_review", res["decision"]
    complete(db, ":promptreview")
    write_report(root, "APPROVE", recon=head)           # полная отметка

    res = gate.run_gate(paths(root), execute=True, notify_fn=quiet)
    assert res["decision"]["action"] == "create_prompt_fix", res["decision"]
    assert not res["decision"].get("stale"), "путаница отметок не повод для пересборки"
    assert "planloop:1:S1:step" not in keys_of(db)


def test_closed_registry_starts_step(stand):
    """Отчёт REQUEST_CHANGES, а реестр закрыт — шаг стартует: вердикт по реестру."""
    root, p, db = stand
    write_plan(root, plan_body(3))
    write_prompt(root, "S1", recon="1111111")
    write_findings(root, [("F1", "стиль", "формулировка", "уточнить", "закрыто"),
                          ("F2", "стиль", "дубль", "убрать", "не блокирует")])
    res = gate.run_gate(p, execute=True, notify_fn=quiet)
    assert res["decision"]["action"] == "create_prompt_review", res["decision"]
    complete(db, ":promptreview")
    write_report(root, "REQUEST_CHANGES")

    res = gate.run_gate(p, execute=True, notify_fn=quiet)
    assert res["decision"]["action"] == "create_step", res["decision"]
    assert "planloop:1:S1:step" in keys_of(db)
    assert not any(":promptfix" in key for key in keys_of(db)), "кругов правок быть не должно"


def test_broken_registry_stops_then_recovers(stand):
    """Битый реестр — стоп-инцидент, а не исключение; починили — конвейер едет дальше."""
    root, p, db = stand
    write_plan(root, plan_body(3))
    write_prompt(root, "S1", recon="1111111")
    res = gate.run_gate(p, execute=True, notify_fn=quiet)
    assert res["decision"]["action"] == "create_prompt_review", res["decision"]
    complete(db, ":promptreview")
    write_report(root, "REQUEST_CHANGES")
    (Path(root) / FINDINGS_REL).write_text("# Реестр\n\nмусор без таблицы\n", encoding="utf-8")

    res = gate.run_gate(p, execute=True, notify_fn=quiet)
    assert res["decision"]["action"] == "stop", res["decision"]
    assert "не читается" in res["decision"]["reason"], res["decision"]
    assert "не читается" in (gate.load_stop(p) or {}).get("reason", "")
    assert "planloop:1:S1:step" not in keys_of(db)

    write_findings(root, [open_row(1)])
    res = gate.run_gate(p, execute=True, notify_fn=quiet)
    assert res["decision"]["action"] == "create_prompt_fix", res["decision"]
    assert gate.load_stop(p) is None, "инцидент снимается сам, когда файл починили"


def test_report_without_recon_starts_step(stand):
    """Отчёт без строки `recon_commit:` — находка `не блокирует`, шаг стартует."""
    root, p, db = stand
    write_plan(root, plan_body(3))
    write_prompt(root, "S1", recon="1111111")
    res = gate.run_gate(p, execute=True, notify_fn=quiet)
    assert res["decision"]["action"] == "create_prompt_review", res["decision"]
    complete(db, ":promptreview")
    write_report(root, "APPROVE", recon=None)

    res = gate.run_gate(p, execute=True, notify_fn=quiet)
    assert res["decision"]["action"] == "create_step", res["decision"]
    assert "planloop:1:S1:step" in keys_of(db)


# ───────────────────────────── правило «0 закрытых» ─────────────────────────────

def test_zero_fix_stops_once_then_owner(stand):
    """Круг, закрывший 0 находок, нового круга не открывает: стоп владельцу один раз."""
    root, p, db = first_fix(stand, [open_row(1), open_row(2, "нет входа у шага")])
    state = json.loads((Path(p.state_file)).read_text(encoding="utf-8"))
    assert state["prompt_fix_baseline"]["open_ids"] == ["F1", "F2"], state
    assert state["prompt_fix_baseline"]["round"] == 1

    # (а) правка закрыта, реестр не тронут — сначала новое ревью, а не стоп.
    res = gate.run_gate(p, execute=True, notify_fn=quiet)
    assert res["decision"]["action"] == "create_prompt_review", res["decision"]
    assert res["decision"]["round"] == 2
    assert gate.load_stop(p) is None

    # (б) ревью снова отказ, находки открыты — стоп владельцу, второго круга нет.
    close_second_review(root, db)
    res = gate.run_gate(p, execute=True, notify_fn=quiet)
    assert res["decision"]["action"] == "stop", res["decision"]
    assert res["decision"]["kind"] == "owner", res["decision"]
    assert res["decision"]["zero_fix"] == 1, res["decision"]
    assert "не закрыла ни одной находки" in res["decision"]["reason"]
    assert "F1" in res["decision"]["reason"] and "F2" in res["decision"]["reason"]
    assert "planloop:1:S1:promptfix:2" not in keys_of(db), "второго круга быть не должно"
    assert "не закрыла ни одной находки" in (gate.load_stop(p) or {}).get("reason", "")

    # (в) владелец снял стоп — следующий круг открывается, стоп не повторяется.
    Path(p.stop_file).unlink()
    res = gate.run_gate(p, execute=True, notify_fn=quiet)
    assert res["decision"]["action"] == "create_prompt_fix", res["decision"]
    assert "planloop:1:S1:promptfix:2" in keys_of(db)


def test_zero_fix_closed_finding_opens_next_round(stand):
    """Одна закрытая находка — круг был не пустым: второй круг открывается."""
    root, p, db = first_fix(stand, [open_row(1), open_row(2, "нет входа у шага")])
    write_findings(root, [("F1", "гейт-исполнение", "тест 3 не изолирует профиль",
                           "выделить каталог профиля", "закрыто"), open_row(2)])
    res = second_review(root, p, db)
    assert res["decision"]["action"] == "create_prompt_fix", res["decision"]
    assert gate.load_stop(p) is None
    assert "planloop:1:S1:promptfix:2" in keys_of(db)


def test_zero_fix_missing_registry_is_incident(stand):
    """Реестр пропал между кругами — инцидент, а не «0 закрытых»: базу не с чем сверить."""
    root, p, db = first_fix(stand, [open_row(1)])
    (Path(root) / FINDINGS_REL).unlink()
    res = second_review(root, p, db)
    assert res["decision"]["action"] == "stop", res["decision"]
    assert "исчез" in res["decision"]["reason"], res["decision"]
    assert "kind" not in res["decision"], "пропавший файл — инцидент, а не решение владельца"


def test_budget_counts_effective_verdict(stand):
    """Бюджет считает действующий вердикт: отчёт APPROVE при открытой находке круги тратит."""
    root, p, db = stand
    set_config(root, max_fix_rounds=1)
    p = paths(root)                      # конфигурация перечитана после правки ключа
    write_plan(root, plan_body(3))
    write_prompt(root, "S1", recon="1111111")
    write_findings(root, [open_row(1)])
    res = gate.run_gate(p, execute=True, notify_fn=quiet)
    assert res["decision"]["action"] == "create_prompt_review", res["decision"]
    complete(db, ":promptreview")
    write_report(root, "REQUEST_CHANGES")
    res = gate.run_gate(p, execute=True, notify_fn=quiet)
    assert res["decision"]["action"] == "create_prompt_fix", res["decision"]
    complete(db, ":promptfix:1")

    res = second_review(root, p, db, verdict="APPROVE")
    assert res["decision"]["action"] == "stop", res["decision"]
    assert "исчерпаны" in res["decision"]["reason"], res["decision"]
    assert "kind" not in res["decision"], "исчерпанный бюджет — инцидент, не решение владельца"
    assert "planloop:1:S1:promptfix:2" not in keys_of(db)


# ───────────────────────────── реестр и карточка правок ─────────────────────────────

def test_registry_survives_prompt_rewrite(project):
    """Переписывание постановки статусы сохраняет, новая находка встаёт со следующим ID."""
    p = paths(project)
    path = gate.prompt_findings_path(p, "S1")
    path.parent.mkdir(parents=True, exist_ok=True)
    gate.add_findings(path, [{"kind": "замысел", "text": "нет цели", "action": "дописать"},
                             {"kind": "стиль", "text": "дубль", "action": "убрать"}],
                      gate.PROMPT_FINDINGS_HEADER, gate.PROMPT_FINDINGS_KEYS,
                      "постановки", "prompt-")
    rows = gate.load_findings(path, gate.PROMPT_FINDINGS_HEADER, gate.PROMPT_FINDINGS_KEYS)
    assert [row["id"] for row in rows] == ["F1", "F2"]

    # Ревьюер закрыл F1, промптер переписал постановку, ревью нашло ещё одно.
    rows[0]["status"] = "закрыто"
    gate.write_findings(path, rows, gate.PROMPT_FINDINGS_HEADER, gate.PROMPT_FINDINGS_KEYS,
                        "постановки", "prompt-")
    write_prompt(project, "S1", recon="1111111")
    gate.add_findings(path, [{"kind": "двусмысленность", "text": "не сказано, чем мерить",
                              "action": "назвать метрику"}],
                      gate.PROMPT_FINDINGS_HEADER, gate.PROMPT_FINDINGS_KEYS,
                      "постановки", "prompt-")

    rows = gate.load_findings(path, gate.PROMPT_FINDINGS_HEADER, gate.PROMPT_FINDINGS_KEYS)
    assert [(row["id"], row["status"]) for row in rows] == [
        ("F1", "закрыто"), ("F2", "открыто"), ("F3", "открыто")]
    text = path.read_text(encoding="utf-8")
    assert text.count(HEADER) == 1, "вторая шапка в реестре — дефект записи"


def test_fix_card_names_open_ids_and_limit(project):
    """Карточка правок называет предел кругов; без числа вид строки прежний."""
    p = paths(project)
    write_report(project, "REQUEST_CHANGES")
    body = gate.prompt_fix_card_body(p, STEP, 2, REPORT_REL, max_rounds=4)
    assert "(круг 2 из 4)" in body, body
    plain = gate.prompt_fix_card_body(p, STEP, 2, REPORT_REL)
    assert "(круг 2)" in plain and "из 4" not in plain
    with_ids = gate.prompt_fix_card_body(p, STEP, 2, REPORT_REL,
                                         findings=["F7: нет входа — добавить вход"])
    assert "F7: нет входа — добавить вход" in with_ids, with_ids

# -*- coding: utf-8 -*-
"""Артефакт первым (план 17, S6): путь из постановки, проба на 60 % лимита, карточка.

Зачем файл: шаг, у которого заявлен файл-артефакт, а файла нет, виден только глазами по
логу. Замер основания — ревью постановки 2-S6 шло 118 минут и не создало ни одного файла:
работа целиком жила в контексте, а владелец узнал об этом по концу лимита. Правило даёт
предупреждение на середине шага: строка «артефакта нет» в карточку и уведомление.

Часы подменяются (`gate._now`), борда — поддельная sqlite, файл постановки лежит там, где
его ищет `prompt_rel`. Внутри файла три уровня: разбор строки (`check_task_prompt`), проба
(`gate.artifact_first_report`) и хук с решением тика (`gate.run_gate`).
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

import check_task_prompt as ctk
import config as config_mod
import gate

#: Схема поддельной борды: у карточки есть `started_at` и `max_runtime_seconds`.
#: Без них правило молчит, и стенд обязан их давать (`read_cards` берёт их, если есть).
TASKS = ("CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, title TEXT,"
         " status TEXT, assignee TEXT, idempotency_key TEXT, completed_at INTEGER,"
         " result TEXT, last_failure_error TEXT, created_at INTEGER,"
         " started_at INTEGER, max_runtime_seconds INTEGER)")

#: Момент «сейчас»: один на файл — иначе «36 мин из 60» в двух тестах означали бы разное.
NOW = 1_900_000_000.0
#: Лимит карточки; 60 % от него — 2160 с, то есть 36 минут.
LIMIT = 3600
SHARE = int(gate.ARTIFACT_FIRST_SHARE * LIMIT)
ARTIFACT = "docs/reviews/17-S6-findings.md"

STEP = {"id": "S1", "epoch": "", "title": "Артефакт первым", "days": 0.25, "owner": "",
        "goal": "предупреждение о шаге без файла", "files": [], "gates": [], "dod": "тест",
        "read": [], "notes": ""}

PROMPT = """# Промт: 1 / S1 — артефакт первым

## 1. Роль и цель

Ты — разработчик. Результат: предупреждение о шаге, который ещё ничего не записал.

Артефакт: `{artifact}`

## 2. Что сделать

- создать первым делом `первый-из-списка.md`, а не объявленный артефакт

## 3. Вопросы

Нет.
""".format(artifact=ARTIFACT)


# ───────────────────────────────── стенд ─────────────────────────────────

def write_prompt(root: Path, text: str = PROMPT) -> Path:
    """Постановка шага там, где её ищет `prompt_rel`: `<prompts_dir>/<дата>_1-S1_prompt.md`."""
    path = Path(root) / "docs" / "prompts" / "2026-11-26_1-S1_prompt.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def paths(root: Path) -> gate.Paths:
    """Пути стража по конфигурации проекта теста (план 1 из `make_project`)."""
    return gate.resolve_paths(config_mod.load(root), "1")


def seed_card(db: Path, card_id: str = "t_art1", *, status: str = "running",
              started_ago: int = SHARE, limit: int = LIMIT) -> None:
    """Карточка шага в поддельной борде: запущена `started_ago` секунд назад."""
    con = sqlite3.connect(db)
    try:
        con.executescript(TASKS)
        con.execute("INSERT OR REPLACE INTO tasks (id, title, status, assignee,"
                    " idempotency_key, created_at, started_at, max_runtime_seconds)"
                    " VALUES (?,?,?,?,?,?,?,?)",
                    (card_id, "шаг S1", status, "demo", "planloop:1:S1:step",
                     int(NOW) - LIMIT, int(NOW) - started_ago, limit))
        con.commit()
    finally:
        con.close()


def comments(db: Path) -> list[dict]:
    """Комментарии поддельной борды в порядке записи."""
    if not db.is_file():
        return []
    con = sqlite3.connect(db)
    try:
        try:
            rows = con.execute("SELECT task_id, body FROM comments ORDER BY id").fetchall()
        except sqlite3.Error:
            return []
    finally:
        con.close()
    return [{"task_id": row[0], "body": row[1]} for row in rows]


def artifact_comments(db: Path) -> list[dict]:
    return [item for item in comments(db) if "артефакта нет" in (item["body"] or "")]


def live_card(card_id: str = "t_art1", *, status: str = "running",
              started_ago: int = SHARE, limit: int = LIMIT) -> dict:
    """Карточка в форме `read_cards` — вход пробы."""
    return {"id": card_id, "status": status, "key": "planloop:1:S1:step",
            "started_at": int(NOW) - started_ago, "max_runtime_seconds": limit}


def ctx(cards: list[dict], **kw) -> dict:
    """Синтетическое состояние тика — как в `test_gate_decide.py`."""
    base = {"next": dict(STEP), "cards": cards, "prev_cards": [], "closed": [],
            "prev_closed": None, "prev_verdict": None, "stop": None, "hold": [],
            "verdict": None, "journal_closed": False, "plan_id": "1", "state": {}}
    base.update(kw)
    return base


@pytest.fixture()
def stand(project: Path, hermes_stub: Path, monkeypatch, tmp_path: Path) -> Path:
    """Стенд тика: проект с движком, поддельная борда, часы и пустой дом профилей.

    `HERMES_HOME`/`LOCALAPPDATA` подменяются: иначе страж читал бы логи настоящего хоста
    (бюджет попытки), и числа теста зависели бы от машины.
    """
    for name in ("PIPELINE_CONFIG", "PIPELINE_PLANS", "PIPELINE_STATE_DIR", "PIPELINE_BOARD"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PIPELINE_REPO", str(project))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
    (tmp_path / "hermes-home").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    (tmp_path / "local").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(gate, "_now", lambda: NOW)
    raw = json.loads((project / "pipeline.json").read_text(encoding="utf-8"))
    raw["require_prompt"] = False
    raw["notify"] = {"enabled": False}
    (project / "pipeline.json").write_text(json.dumps(raw, ensure_ascii=False),
                                           encoding="utf-8")
    return project


# ─────────────────── §4.1: строка «Артефакт:» из блока «Роль и цель» ───────────────────

def test_line_from_declaration():
    """Путь читается из объявления: кавычки, маркер списка, жирное слово, точка в конце."""
    assert ctk.artifact_line(PROMPT) == ARTIFACT
    assert ctk.artifact_line("## Роль и цель\n\nАртефакт: docs/x.md.\n") == "docs/x.md"
    assert ctk.artifact_line("## Роль и цель\n\n- **Артефакт:** `docs/x.md`\n") == "docs/x.md"
    assert ctk.artifact_line("## 1. Роль и цель\n\nартефакт: `docs/x.md`\n") == "docs/x.md"


def test_line_absent_or_prose_is_none():
    """Нет строки, проза перед словом, чужой блок — объявления нет."""
    assert ctk.artifact_line("## Роль и цель\n\nТы — исполнитель шага.\n") is None
    assert ctk.artifact_line("## Роль и цель\n\nАртефакт шага — файл отчёта.\n") is None
    assert ctk.artifact_line("## Что сделать\n\nАртефакт: `docs/x.md`\n") is None
    assert ctk.artifact_line("") is None


# ───────────────────────── §4.2: предупреждение W2 ─────────────────────────

def w2(text: str) -> tuple[str, str]:
    return next((level, message) for level, code, message in ctk.check_prompt(text)
                if code == "W2")


def test_w2_warns_without_line():
    """Строки нет — предупреждение, а не провал постановки."""
    level, message = w2("## Роль и цель\n\nТы — исполнитель шага.\n")
    assert level == "WARN"
    assert "Артефакт" in message


def test_w2_passes_with_path_and_with_refusal():
    """Объявленный путь и отказ — оба PASS; во втором случае шаг файла не создаёт."""
    level, message = w2(f"## Роль и цель\n\nАртефакт: `{ARTIFACT}`\n")
    assert (level, ARTIFACT in message) == ("PASS", True)
    level, message = w2("## Роль и цель\n\nАртефакт: нет\n")
    assert level == "PASS" and "файла не создаёт" in message


# ─────────────────────── §4.3: путь берётся из постановки ───────────────────────

def test_path_comes_from_prompt_only(stand: Path):
    """Постановки нет — пути нет; появилась — путь равен объявленному."""
    p = paths(stand)
    assert gate.artifact_path(p, "S1") is None
    write_prompt(stand)
    assert gate.artifact_path(p, "S1") == ARTIFACT


def test_path_is_none_on_refusal_and_unknown_step(stand: Path):
    """Отказ постановки и чужой шаг — `None`: проверять нечего."""
    write_prompt(stand, PROMPT.replace(f"`{ARTIFACT}`", "нет"))
    p = paths(stand)
    assert gate.artifact_path(p, "S1") is None
    assert gate.artifact_path(p, "S9") is None
    assert gate.artifact_path(p, "") is None


# ───────────────────── §4.4: проба «шаг идёт, файла нет» ─────────────────────

def test_report_silent_before_share(stand: Path):
    """До 60 % лимита проба молчит: предупреждать пока не о чем."""
    write_prompt(stand)
    assert gate.artifact_first_report(paths(stand),
                                      ctx([live_card(started_ago=SHARE - 1)])) is None


def test_report_at_share_names_path_and_minutes(stand: Path):
    """На 60 % — проба с путём и минутами: «прошло 36 мин из 60»."""
    write_prompt(stand)
    report = gate.artifact_first_report(paths(stand), ctx([live_card(started_ago=SHARE)]))
    assert report is not None
    assert report["text"] == (f"артефакта нет: {ARTIFACT}, прошло 36 мин из 60")
    assert report["path"] == ARTIFACT and report["card"] == "t_art1"


def test_report_silent_when_file_exists(stand: Path):
    """Файл создан раньше — проверка молчит, даже когда лимит уже перейден."""
    write_prompt(stand)
    target = Path(stand) / ARTIFACT
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("находки", encoding="utf-8")
    assert gate.artifact_first_report(paths(stand),
                                      ctx([live_card(started_ago=LIMIT)])) is None


def test_report_silent_without_declaration(stand: Path):
    """Постановка не объявила путь — правило молчит (перебора кандидатов нет)."""
    write_prompt(stand, PROMPT.replace(f"Артефакт: `{ARTIFACT}`", "Ты — исполнитель."))
    assert gate.artifact_first_report(paths(stand),
                                      ctx([live_card(started_ago=LIMIT)])) is None


def test_report_silent_without_live_card(stand: Path):
    """Нет карточки, нет времени старта, нет лимита, карточка закрыта — проба молчит."""
    write_prompt(stand)
    p = paths(stand)
    assert gate.artifact_first_report(p, ctx([])) is None
    assert gate.artifact_first_report(p, ctx([live_card(started_ago=LIMIT, limit=0)])) is None
    done = live_card(started_ago=LIMIT, status="done")
    assert gate.artifact_first_report(p, ctx([done])) is None
    no_start = live_card(limit=LIMIT)
    no_start["started_at"] = 0
    assert gate.artifact_first_report(p, ctx([no_start])) is None


# ─────────────── §4.5: строка в карточку и уведомление — один раз ───────────────

def test_hook_comments_once_and_notifies(stand: Path, hermes_stub: Path):
    """Хук пишет строку в карточку, уведомляет владельца и второй раз не повторяет."""
    write_prompt(stand)
    seed_card(hermes_stub)
    p = paths(stand)
    state: dict = {}
    said: list[str] = []

    report = gate.artifact_first_hook(p, ctx([live_card()], state=state), said.append)
    assert report is not None
    found = artifact_comments(hermes_stub)
    assert len(found) == 1 and found[0]["task_id"] == "t_art1"
    assert found[0]["body"] == f"артефакта нет: {ARTIFACT}, прошло 36 мин из 60"
    assert state["artifact_reported"] == ["t_art1"]
    assert said and "артефакта нет" in said[0] and "t_art1" in said[0]

    gate.artifact_first_hook(p, ctx([live_card()], state=state), said.append)
    assert len(artifact_comments(hermes_stub)) == 1
    assert len(said) == 1


def test_hook_silent_without_state(stand: Path, hermes_stub: Path):
    """Состояния нет — адресовать отметку некуда: хук молчит (как `report_stalls`)."""
    write_prompt(stand)
    seed_card(hermes_stub)
    said: list[str] = []
    assert gate.artifact_first_hook(paths(stand), ctx([live_card()], state=None)) is None
    assert artifact_comments(hermes_stub) == [] and said == []


# ──────────────────── §4.7: блок «ПЕРВЫЙ АРТЕФАКТ» в карточке ────────────────────

def test_body_carries_block_before_goal(stand: Path):
    """Тело карточки называет файл до «Цель:» и до списка файлов; без постановки — молчит."""
    p = paths(stand)
    assert "ПЕРВЫЙ АРТЕФАКТ" not in gate.step_card_body(p, STEP)

    write_prompt(stand)
    body = gate.step_card_body(p, STEP)
    assert "ПЕРВЫЙ АРТЕФАКТ" in body
    assert body.index("ПЕРВЫЙ АРТЕФАКТ") < body.index("Цель:")
    assert body.index("ПЕРВЫЙ АРТЕФАКТ") < body.index("Файлы (править/создавать):")
    assert ARTIFACT in body


def test_body_silent_on_refusal(stand: Path):
    """Постановка отказалась от артефакта — блока в теле нет."""
    write_prompt(stand, PROMPT.replace(f"`{ARTIFACT}`", "нет"))
    assert "ПЕРВЫЙ АРТЕФАКТ" not in gate.step_card_body(paths(stand), STEP)


# ───────────────────────── §4.6: тик целиком ─────────────────────────

def test_tick_comments_and_decides_as_before(stand: Path, hermes_stub: Path):
    """Тик с живой карточкой: строка в карточку есть, стоп-флага нет, решение то же."""
    write_prompt(stand)
    seed_card(hermes_stub)
    p = paths(stand)
    said: list[str] = []

    first = gate.run_gate(p, notify_fn=said.append)
    assert first["decision"]["action"] == "idle", first["decision"]
    assert len(artifact_comments(hermes_stub)) == 1
    assert any("артефакта нет" in text for text in said)
    assert gate.load_stop(p) is None

    second = gate.run_gate(p, notify_fn=said.append)
    assert second["decision"]["action"] == first["decision"]["action"]
    assert len(artifact_comments(hermes_stub)) == 1

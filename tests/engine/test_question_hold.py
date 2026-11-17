# -*- coding: utf-8 -*-
"""Вопросы шага (план 8, S5): ожидание по вопросу, единственный путь ответа, автовыбор.

Проверяется не текст функции, а путь целиком: строка вопроса в отчёте → файл вопросов →
ожидание с причиной → ответ (чат, панель, таймаут) → шаг едет дальше. Вопрос — повод
ожидания из файла, а не запись в `hold_steps` (допущение 6): после ответа `hold_released`
остаётся пустым.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

import config as config_mod
import gate
from engine_kit import KIT, PLAN, complete, make_project, set_risk_gate, write_approval

sys.path.insert(0, str(KIT / "tests"))

from helpers import run_gate, stub_cards  # noqa: E402  (мост CLI и борды для демо-сценария)

QUESTION_LINE = ("Q1: поднять ли лимит карточек до трёх — A) да, три (цена: больше карточек "
                 "в работе) / B) нет, две (цена: простой) — рекомендация A")
QUESTION_TEXT = "поднять ли лимит карточек до трёх"
QUESTION_OPTIONS = "A) да, три (цена: больше карточек в работе) / B) нет, две (цена: простой)"
PLAN_HIGH = PLAN.replace(
    "### S1. Таблица счетов (1 дн)\n\n- Файл:",
    "### S1. Таблица счетов (1 дн)\n\n- Риск: high\n- Файл:")


def fake_card(created: list[str]):
    """Подпись `_create_card`, которая только запоминает ключи: борда не нужна."""

    def create(p, title, body, assignee, key, skills, parent=None, max_runtime=None):
        created.append(key)
        return "t_fake0001"

    return create


def write_prompt(project: Path, body: str = "# Постановка S1\n\nЧто сделать: таблицу.\n") -> Path:
    path = project / "docs" / "prompts" / "2026-11-16_1-S1_prompt.md"
    path.write_text(body, encoding="utf-8")
    return path


def questions_file(project: Path) -> Path:
    return project / "docs" / "reviews" / "plan-1-questions.md"


def rows_of(project: Path) -> list[dict]:
    return gate.load_questions(questions_file(project))


def state_of(cfg) -> dict:
    return gate.load_state(gate.resolve_paths(cfg, "1"))


def tick(p, messages: list[str] | None = None, created: list[str] | None = None) -> dict:
    """Один тик без борды: карточки записываются ключами, уведомления — списком."""
    return gate.run_gate(p, execute=True, create_card=fake_card(created if created is not None
                                                                else []),
                         notify_fn=(messages.append if messages is not None else
                                    (lambda text: None)))


def clock(monkeypatch, start: int = 1_000_000) -> list[int]:
    """Подменить часы движка: таймаут проверяется без сна и без ожидания минуты."""
    now = [int(start)]
    monkeypatch.setattr(gate, "_now", lambda: float(now[0]))
    return now


@pytest.fixture()
def stand(project: Path, hermes_stub: Path, monkeypatch):
    """Проект плана 1: конфиг, пути плана и поддельный `hermes` в PATH."""
    monkeypatch.setenv("PIPELINE_REPO", str(project))
    cfg = config_mod.load(project)
    return cfg, gate.resolve_paths(cfg, "1")


@pytest.fixture()
def stand_high(project: Path, hermes_stub: Path, monkeypatch):
    """Стенд `risk_gate: hold` с риском `high` у S1: вопрос и допуск живут одновременно."""
    path = project / "docs" / "plans" / "1-demo.md"
    path.write_text(PLAN_HIGH, encoding="utf-8")
    write_approval(project, path)
    set_risk_gate(project, "hold")
    monkeypatch.setenv("PIPELINE_REPO", str(project))
    cfg = config_mod.load(project)
    return cfg, gate.resolve_paths(cfg, "1")


@pytest.fixture()
def stand_spec(project: Path, hermes_stub: Path, monkeypatch):
    """Тот же high-шаг при умолчании `risk_gate: spec`: держит не допуск, а спека."""
    path = project / "docs" / "plans" / "1-demo.md"
    path.write_text(PLAN_HIGH, encoding="utf-8")
    write_approval(project, path)
    monkeypatch.setenv("PIPELINE_REPO", str(project))
    cfg = config_mod.load(project)
    return cfg, gate.resolve_paths(cfg, "1")


def test_question_line_registers_once(project: Path, hermes_stub: Path, stand):
    """S5 п.2: строка `Q1:` из отчёта даёт одну строку в файле вопросов со шапкой S5."""
    cfg, p = stand
    write_prompt(project, f"# Постановка S1\n\n## 8. Вопросы\n\n{QUESTION_LINE}\n")
    tick(p)

    rows = rows_of(project)
    assert len(rows) == 1
    assert rows[0]["id"] == "Q-1" and rows[0]["step"] == "S1"
    assert rows[0]["text"] == QUESTION_TEXT
    assert rows[0]["options"] == QUESTION_OPTIONS
    assert rows[0]["status"] == "открыт" and rows[0]["recommend"] == "A"
    assert list(rows[0]) == list(gate.QUESTIONS_KEYS)
    assert gate.QUESTIONS_HEADER == ("ID", "Шаг", "Вопрос", "Варианты", "Рекомендация",
                                     "Статус", "Ответ")
    header = questions_file(project).read_text(encoding="utf-8").splitlines()[2]
    assert [cell.strip() for cell in header.strip().strip("|").split("|")] == list(
        gate.QUESTIONS_HEADER)
    assert "Q-1" in state_of(cfg)["questions"]

    # Второй тик строку не дублирует: ID ведёт автор отчёта.
    tick(p)
    assert len(rows_of(project)) == 1


def test_second_step_question_gets_free_id(project: Path, hermes_stub: Path, stand):
    """S5 п.2/ревью: номер `Q1` второй постановки не совпадает с ID — берётся новый ID.

    Нумерация в отчётах своя у каждого автора: каждая постановка пишет `Q1`. Дубликат
    считается парой (шаг, текст), поэтому вопрос шага S2 при уже закрытом `Q-1` шага S1
    получает свободный номер и держит свой шаг.
    """
    cfg, p = stand
    gate.write_questions(questions_file(project),
                         [{"id": "Q-1", "step": "S1", "text": "какой лимит карточек",
                           "options": "", "recommend": "", "status": "закрыт", "answer": "A"}],
                         "1")
    state = gate.load_state(p)
    write_prompt(project, f"# Постановка S1\n\n{QUESTION_LINE}\n")
    prompt = project / "docs" / "prompts" / "2026-11-16_1-S2_prompt.md"
    prompt.write_text("# Постановка S2\n\nQ1: поднимать ли версию плагина — "
                      "A) да, минор / B) нет (цена: релиз ждёт) — рекомендация A\n",
                      encoding="utf-8")
    ctx = {"next": {"id": "S2"}, "state": state, "cards": []}
    added = gate.register_questions(p, ctx, state)

    assert [(row["step"], row["text"], row["status"]) for row in added] == [
        ("S2", "поднимать ли версию плагина", "открыт")]
    rows = rows_of(project)
    assert [(row["id"], row["step"], row["status"]) for row in rows] == [
        ("Q-1", "S1", "закрыт"), ("Q-2", "S2", "открыт")]
    assert rows[1]["recommend"] == "A"
    # Решение того же тика видит новый вопрос: файл перечитан, а не «запомнен».
    assert [item["id"] for item in ctx["step_questions"]] == ["Q-2"]
    assert "Q-2" in (state.get("questions") or {})


def test_foreign_step_card_question_not_registered(project: Path, hermes_stub: Path, stand):
    """S5 п.2/ревью: вопрос из карточки чужого шага текущему шагу не заводится."""
    cfg, p = stand
    state = gate.load_state(p)
    ctx = {"next": {"id": "S1"}, "state": state, "cards": [
        {"key": "planloop:1:S2:step",
         "result": "Q7: старый вопрос шага S2 — A) да / B) нет — рекомендация A"},
        {"key": "planloop:1:S1:step",
         "result": "Q8: вопрос текущего шага — A) да / B) нет — рекомендация A"},
    ]}
    added = gate.register_questions(p, ctx, state)

    assert [(row["step"], row["text"]) for row in added] == [("S1", "вопрос текущего шага")]
    assert [(row["id"], row["step"]) for row in rows_of(project)] == [("Q-1", "S1")]


def test_registry_skips_columns_absent_in_keys(tmp_path: Path):
    """S5 §4.2: проверки `id`/`status` идут только по колонкам, которые есть в `keys`.

    Третья таблица того же разбора может не иметь статуса вовсе — это «проверки нет»,
    а не `KeyError` и не отказ записи.
    """
    path = tmp_path / "registry.md"
    header, keys = ("ID", "Текст"), ("id", "text")
    gate.write_findings(path, [{"id": "F1", "text": "что-то"}], header, keys,
                        title="# Реестр без статуса")
    assert gate.load_findings(path, header, keys) == [{"id": "F1", "text": "что-то"}]
    # Присвоение ID идёт теми же параметрами, что разбор: у вопросов это `Q-N`.
    gate.write_questions(path, [{"id": "Q-1", "step": "S1", "text": "т", "status": "закрыт",
                                 "options": "", "recommend": "", "answer": ""}], "7")
    added = gate.add_findings(path, [{"step": "S2", "text": "второй"}],
                              gate.QUESTIONS_HEADER, gate.QUESTIONS_KEYS,
                              title="# Вопросы плана 7",
                              **gate._question_rules())
    assert [row["id"] for row in added] == ["Q-1", "Q-2"]
    assert added[1]["status"] == "открыт"


def test_open_question_holds_step(project: Path, hermes_stub: Path, stand):
    """S5 п.3: ожидание с номером вопроса в причине, `hold_steps` пуст."""
    cfg, p = stand
    write_prompt(project, f"# Постановка S1\n\n{QUESTION_LINE}\n")
    decision = tick(p)["decision"]

    assert decision["action"] == "idle"
    assert decision["question"] == "Q-1"
    assert "Q-1" in decision["reason"] and QUESTION_TEXT in decision["reason"]
    assert "рекомендация A" in decision["reason"] and "/answer Q-1" in decision["reason"]
    # Вопрос — повод из файла, а не ручной допуск: в hold_steps его нет.
    assert (state_of(cfg).get("hold_steps") or []) == []


def test_open_question_blocks_prompt_prefetch(project: Path, hermes_stub: Path, stand):
    """S5 п.3/4.5: пока вопрос открыт, карточки постановки и шага не заказываются."""
    cfg, p = stand
    write_prompt(project, f"# Постановка S1\n\n{QUESTION_LINE}\n")
    messages: list[str] = []
    res = tick(p, messages)

    keys = {row["key"] for row in stub_cards(hermes_stub)}
    assert not any(key.endswith((":prompt", ":step")) for key in keys), keys
    assert res["decision"]["action"] == "idle" and res["decision"]["question"] == "Q-1"
    # Уведомление — одна строка, в ней номер, шаг и способ ответить.
    notices = [text for text in messages if "❓ Q-1" in text]
    assert len(notices) == 1, messages
    assert "шаг S1" in notices[0] and "/answer Q-1" in notices[0]


def test_open_question_does_not_move_verdict(project: Path, hermes_stub: Path, stand):
    """S5 п.3: отчёт с APPROVE при открытом вопросе не закрывает шаг и не даёт правок."""
    cfg, p = stand
    write_prompt(project)
    # Карточка шага ставится на настоящую (поддельную) борду: её и закрывает исполнитель.
    gate.run_gate(p, execute=True, notify_fn=lambda text: None)
    complete(hermes_stub, ":step")
    (project / "docs" / "reviews" / "1-S1.md").write_text(
        f"# Ревью S1\n\n{QUESTION_LINE}\n\n## ВЕРДИКТ: APPROVE\n", encoding="utf-8")

    res = gate.run_gate(p, execute=True, notify_fn=lambda text: None)
    assert res["decision"]["action"] == "idle", res["decision"]
    assert res["decision"]["question"] == "Q-1"
    keys = {row["key"] for row in stub_cards(hermes_stub)}
    assert not any(key.endswith((":fix", ":review")) for key in keys), keys
    assert "[ЗАКРЫТ]" not in (project / "docs" / "plans" / "1-demo.md").read_text(encoding="utf-8")
    assert (state_of(cfg).get("hold_steps") or []) == []


def test_answer_closes_question_and_lifts_wait(project: Path, hermes_stub: Path, stand):
    """S5 п.4: `/answer` закрывает вопрос, снимает ожидание и не пополняет `hold_released`."""
    cfg, p = stand
    write_prompt(project, "# Постановка S1\n\n"
                 f"{QUESTION_LINE}\n"
                 "Q2: какой формат отчёта? — A) таблица / B) список — рекомендация B\n")
    tick(p)
    assert [row["id"] for row in rows_of(project)] == ["Q-1", "Q-2"]

    answer = gate.handle_command(cfg, "/answer Q-1 A")
    assert "Q-1" in answer and "закрыт" in answer
    assert rows_of(project)[0]["status"] == "закрыт"
    assert rows_of(project)[0]["answer"] == "A"
    assert "Q-1" not in (state_of(cfg).get("questions") or {})

    # Короткая форма номера — тот же путь: `Q2` пишется в файл как `Q-2`.
    assert "закрыт" in gate.handle_command(cfg, "/answer Q2 B")
    assert rows_of(project)[1]["status"] == "закрыт"
    assert rows_of(project)[1]["answer"] == "B"
    assert (state_of(cfg).get("hold_released") or []) == []
    assert (state_of(cfg).get("hold_steps") or []) == []

    created: list[str] = []
    res = tick(p, created=created)
    assert res["decision"]["action"] == "create_step", res["decision"]
    assert created and created[0].endswith(":S1:step")


def test_answer_unknown_step_and_question_refused(project: Path, hermes_stub: Path, stand):
    """S5 п.4: чужой шаг и чужой номер — отказ с перечнем открытых, файл не меняется."""
    cfg, p = stand
    write_prompt(project, f"# Постановка S1\n\n{QUESTION_LINE}\n")
    tick(p)
    rows = rows_of(project)
    gate.write_questions(questions_file(project), [dict(rows[0], step="S9")], "1")
    before = questions_file(project).read_bytes()

    foreign_step = gate.answer_question(cfg, "1", "Q-1", "A")
    assert foreign_step["ok"] is False
    assert "нет в плане" in foreign_step["message"]
    assert "Q-1 · шаг S9" in foreign_step["message"]
    unknown = gate.answer_question(cfg, "1", "Q-7", "A")
    assert unknown["ok"] is False
    assert "нет вопроса Q-7" in unknown["message"]
    assert "Q-1 · шаг S9" in unknown["message"]
    assert questions_file(project).read_bytes() == before


def test_auto_answer_by_timeout(project: Path, hermes_stub: Path, stand, monkeypatch):
    """S5 п.5: по истечении ожидания (умолчание — час) рекомендация принимается с «авто»."""
    cfg, p = stand
    now = clock(monkeypatch)
    write_prompt(project, f"# Постановка S1\n\n{QUESTION_LINE}\n")
    tick(p)
    now[0] += 3601

    messages: list[str] = []
    res = tick(p, messages)
    assert res["applied"] is True
    assert res["decision"]["action"] == "question_auto", res["decision"]
    assert rows_of(project)[0]["status"] == "закрыт"
    assert rows_of(project)[0]["answer"] == "A — авто: рекомендация по таймауту 60 мин"
    assert (state_of(cfg).get("hold_steps") or []) == []
    assert (state_of(cfg).get("hold_released") or []) == []
    assert any("Q-1 принят автоматически: A (рекомендация)" in text for text in messages), \
        messages


def test_auto_answer_next_tick_creates_card(project: Path, hermes_stub: Path, stand,
                                            monkeypatch):
    """S5 п.5: после автоответа карточка шага заказывается следующим тиком, не тем же."""
    cfg, p = stand
    now = clock(monkeypatch)
    write_prompt(project, f"# Постановка S1\n\n{QUESTION_LINE}\n")
    tick(p)
    now[0] += 3601     # умолчание ожидания — час
    auto: list[str] = []
    tick(p, created=auto)
    assert auto == [], auto

    created: list[str] = []
    res = tick(p, created=created)
    assert res["decision"]["action"] == "create_step", res["decision"]
    assert created and created[0].endswith(":S1:step")


def test_auto_answer_keeps_risk_hold(project: Path, hermes_stub: Path, stand_high, monkeypatch):
    """S5 п.5: автоответ не выдаёт допуск — стенд `risk_gate: hold`, шаг `high`.

    Симметрия в том же тесте: при умолчании `risk_gate: spec` тот же шаг после автоответа
    уходит в проверку спеки, а не в ручной допуск.
    """
    cfg, p = stand_high
    now = clock(monkeypatch)
    write_prompt(project, f"# Постановка S1\n\n{QUESTION_LINE}\n")
    tick(p)
    now[0] += 3601     # умолчание ожидания — час
    tick(p)
    assert rows_of(project)[0]["status"] == "закрыт"

    created: list[str] = []
    res = tick(p, created=created)
    assert res["decision"]["action"] == "hold_risk", res["decision"]
    assert created == []
    state = state_of(cfg)
    assert state.get("hold_steps") == ["S1"]
    assert (state.get("hold_released") or []) == []

    # Симметрия `spec`: второй проект, тот же high-шаг, умолчание шаблона.
    spec_root = project.parent / "spec-proj"
    make_project(spec_root)
    spec_plan = spec_root / "docs" / "plans" / "1-demo.md"
    spec_plan.write_text(PLAN_HIGH, encoding="utf-8")
    write_approval(spec_root, spec_plan)
    monkeypatch.setenv("PIPELINE_REPO", str(spec_root))
    spec_p = gate.resolve_paths(config_mod.load(spec_root), "1")
    write_prompt(spec_root, f"# Постановка S1\n\n{QUESTION_LINE}\n")
    tick(spec_p)
    now[0] += 3601     # умолчание ожидания — час
    tick(spec_p)

    spec_created: list[str] = []
    spec_res = tick(spec_p, created=spec_created)
    assert spec_res["decision"]["action"] != "hold_risk", spec_res["decision"]
    assert (gate.load_state(spec_p).get("hold_steps") or []) == []


def test_timeout_without_recommendation_waits(project: Path, hermes_stub: Path, stand,
                                              monkeypatch):
    """S5 п.6: вопрос без рекомендации не автовыбирается — ждёт человека."""
    cfg, p = stand
    now = clock(monkeypatch)
    write_prompt(project, "# Постановка S1\n\nQ1: какой лимит карточек? — A) две / B) три\n")
    tick(p)
    now[0] += 86400

    messages: list[str] = []
    res = tick(p, messages)
    assert rows_of(project)[0]["status"] == "открыт"
    assert rows_of(project)[0]["answer"] == ""
    assert res["decision"]["action"] == "idle" and res["decision"]["question"] == "Q-1"
    assert not any("принят автоматически" in text for text in messages), messages


def test_answer_timeout_zero_disables(project: Path, hermes_stub: Path, stand, monkeypatch):
    """S5 п.5: `notify.answer_timeout: 0` выключает автовыбор — час ожидания ничего не решает."""
    now = clock(monkeypatch)
    config_data = json.loads((project / "pipeline.json").read_text(encoding="utf-8"))
    config_data["notify"]["answer_timeout"] = 0
    (project / "pipeline.json").write_text(json.dumps(config_data, ensure_ascii=False,
                                                      indent=2), encoding="utf-8")
    cfg = config_mod.load(project)
    p = gate.resolve_paths(cfg, "1")
    write_prompt(project, f"# Постановка S1\n\n{QUESTION_LINE}\n")
    tick(p)
    now[0] += 3600

    res = tick(p)
    assert rows_of(project)[0]["status"] == "открыт"
    assert res["decision"]["action"] == "idle" and res["decision"]["question"] == "Q-1"


def test_notice_not_oftener_than_hour(project: Path, hermes_stub: Path, stand, monkeypatch):
    """S5 п.4б: на вопрос — одно уведомление, повтор не чаще раза в час."""
    cfg, p = stand
    now = clock(monkeypatch)
    # Без рекомендации: автовыбор не закроет вопрос и не подменит собою напоминание.
    write_prompt(project, "# Постановка S1\n\nQ1: какой лимит карточек? — A) две / B) три\n")

    first: list[str] = []
    tick(p, first)
    assert sum("❓ Q-1" in text for text in first) == 1, first

    for moment in (300, 301):
        now[0] = 1_000_000 + moment
        again: list[str] = []
        tick(p, again)
        assert not any("❓ Q-1" in text for text in again), again

    now[0] = 1_000_000 + 3601
    third: list[str] = []
    tick(p, third)
    assert any("❓ Q-1" in text for text in third), third


def test_free_text_answer_and_empty_variants(project: Path, hermes_stub: Path, stand):
    """S5 п.2/4: вопрос без вариантов валиден, ответ приходит свободным текстом."""
    cfg, p = stand
    # Авторский номер в отчёт — не ID реестра (ID присваивает страж), поэтому `Q1` в постановке
    # и `Q-1` в файле: строка проверяет вопрос без вариантов и свободный ответ.
    write_prompt(project, "# Постановка S1\n\nQ1: как оформляем отчёт — рекомендации нет\n")
    res = tick(p)
    row = rows_of(project)[0]
    assert row["id"] == "Q-1" and row["options"] == "" and row["recommend"] == ""
    assert "рекомендации нет" in res["decision"]["reason"]
    # Автовыбора у такого вопроса не будет, поэтому и дедлайна в панели нет (S5, п.4.8).
    assert res["questions"][0]["asked_at"] and res["questions"][0]["deadline_at"] is None

    answer = gate.handle_command(cfg, "/answer Q-1 делаем по-своему")
    assert "закрыт" in answer
    assert rows_of(project)[0]["status"] == "закрыт"
    assert rows_of(project)[0]["answer"] == "делаем по-своему"


@pytest.mark.slow
def test_demo_question_flow_cli(project: Path, hermes_stub: Path):
    """Г5: «Готово =» шага одной командой — ожидание → ответ → карточка шага через CLI."""
    write_prompt(project, f"# Постановка S1\n\n{QUESTION_LINE}\n")
    # Тик регистрирует вопрос и встаёт на ожидании; ожидание видно в состоянии, а карточек нет.
    assert run_gate(project, "--run", "--plan", "1").returncode == 0
    shown = run_gate(project, "--status", "--plan", "1")
    assert shown.returncode == 0, shown.stderr
    assert "ждёт ответа на вопрос Q-1" in shown.stdout
    assert [row["id"] for row in gate.load_questions(questions_file(project))] == ["Q-1"]
    assert not any(row["key"].endswith((":prompt", ":step"))
                   for row in stub_cards(hermes_stub))

    answered = run_gate(project, "--plan", "1", "--answer", "Q-1", "A")
    assert answered.returncode == 0, answered.stderr
    assert "вопрос Q-1 закрыт" in answered.stdout
    assert gate.load_questions(questions_file(project))[0]["status"] == "закрыт"

    third = run_gate(project, "--run", "--plan", "1")
    assert third.returncode == 0, third.stderr
    assert any(row["key"].endswith(":S1:step") for row in stub_cards(hermes_stub))


# Абзац автора, перенесённый по ширине страницы: так вопрос выглядит в живом отчёте
# (`docs/reviews/13-S2-prompt.md`). Прежний разбор читал только первую строку и терял хвост
# абзаца — вместе с вариантами и рекомендацией.
MULTILINE_QUESTION = (
    "Q1: Ветка реестра шага и стопы формата S1 — кто первый? — A) стопы формата S1 выше:\n"
    "нечитаемый отчёт и отчёт без меток класса останавливают шаг, даже когда в реестре есть\n"
    "открытые строки (цена: один тик простоя) / B) реестр выше: открытая строка заказывает\n"
    "круг правок при любом отчёте (цена: круг по нечитаемому отчёту повторяется) — "
    "рекомендация A")
MULTILINE_TEXT = "Ветка реестра шага и стопы формата S1 — кто первый?"


def test_multiline_question_reads_whole_paragraph(project: Path, hermes_stub: Path, stand):
    """S5 п.2: вопрос — абзац. Переносы строк его не рвут, тире внутри текста остаётся текстом."""
    cfg, p = stand
    write_prompt(project, "# Постановка S1\n\n" + MULTILINE_QUESTION + "\n\n## Как проверено\n\n"
                 "Прогонов не было.\n")
    res = tick(p)

    row = rows_of(project)[0]
    assert row["text"] == MULTILINE_TEXT
    assert row["recommend"] == "A"
    assert "стопы формата S1 выше" in row["options"]
    assert "реестр выше" in row["options"]
    view = res["questions"][0]
    assert [item["letter"] for item in view["options"]] == ["A", "B"]
    assert "один тик простоя" in view["options"][0]["text"]
    # Рекомендация названа — дедлайн автовыбора у вопроса есть.
    assert view["deadline_at"]


def test_truncated_open_question_is_migrated_in_place(project: Path, hermes_stub: Path, stand):
    """S5 п.2: уже записанный обрезанный вопрос приводится к полному тексту на месте.

    Второй строки не заводится, ID и время вопроса сохраняются: это тот же вопрос, а не новый.
    """
    cfg, p = stand
    gate.write_questions(questions_file(project), [{
        "id": "Q-1", "step": "S1", "text": "Ветка реестра шага и стопы формата S1",
        "options": "кто первый? — A) стопы формата S1 выше: нечитаемый",
        "recommend": "", "status": "открыт", "answer": ""}], "1")
    state = gate.load_state(p)
    asked = 1_000_000
    state["questions"] = {"Q-1": {"asked_at": asked, "notified_at": asked}}
    gate.save_state(p, state)
    write_prompt(project, "# Постановка S1\n\n" + MULTILINE_QUESTION + "\n")

    ctx = {"next": {"id": "S1"}, "state": state, "cards": []}
    assert gate.register_questions(p, ctx, state) == []

    rows = rows_of(project)
    assert [row["id"] for row in rows] == ["Q-1"]
    assert rows[0]["text"] == MULTILINE_TEXT
    assert rows[0]["recommend"] == "A"
    assert rows[0]["status"] == "открыт"
    assert (state.get("questions") or {})["Q-1"]["asked_at"] == asked


def test_status_lists_questions_of_every_step(project: Path, hermes_stub: Path, stand):
    """S5 п.4: вьюха вопросов — по всем шагам плана, шаг остаётся полем строки.

    Вкладка «Шаг» показывает вопрос выбранного шага, поэтому страже отдаёт все открытые
    вопросы плана: иначе вопрос следующего шага в ней не виден вовсе.
    """
    cfg, p = stand
    write_prompt(project, f"# Постановка S1\n\n{QUESTION_LINE}\n")
    tick(p)
    gate.write_questions(questions_file(project), rows_of(project) + [{
        "id": "Q-2", "step": "S2", "text": "как проверяем следующий шаг",
        "options": "A) тестом / B) глазами", "recommend": "A", "status": "открыт",
        "answer": ""}], "1")

    shown = gate.run_gate(p, execute=False)
    assert {item["id"]: item["step"] for item in shown["questions"]} == {"Q-1": "S1", "Q-2": "S2"}
    # Удержание шага по-прежнему считается по вопросу текущего шага.
    assert shown["decision"]["question"] == "Q-1"


def test_same_answer_twice_is_not_an_error(project: Path, hermes_stub: Path, stand):
    """S5 п.4: повтор того же ответа — не отказ (двойной клик в окне панели)."""
    cfg, p = stand
    write_prompt(project, f"# Постановка S1\n\n{QUESTION_LINE}\n")
    tick(p)

    assert gate.answer_question(cfg, "1", "Q-1", "A")["ok"] is True
    second = gate.answer_question(cfg, "1", "Q-1", "A")
    assert second["ok"] is True and "уже закрыт тем же ответом" in second["message"]
    other = gate.answer_question(cfg, "1", "Q-1", "B")
    assert other["ok"] is False and "уже закрыт" in other["message"]
    # Закрытая строка не переписана другим ответом.
    assert rows_of(project)[0]["answer"] == "A"


def test_question_notice_reads_like_a_message_not_a_log_line(project, hermes_stub, stand):
    """Уведомление о вопросе: варианты списком, рекомендация и срок человеческим языком.

    Однострочная склейка «варианты: A) … / B) …» в телефоне не читалась, а без срока
    владелец не понимал, что через час вопрос решат за него.
    """
    cfg, p = stand
    write_prompt(project, f"# Постановка S1\n\n{MULTILINE_QUESTION}\n")
    tick(p)
    item = rows_of(project)[0]
    text = gate.question_notice_text(p, item, 3600)
    lines = [line for line in text.splitlines() if line.strip()]

    assert lines[0].startswith("❓ Q-1")
    assert "план 1, шаг S1" in lines[1]
    assert "стопы формата S1 выше" in text and "B) реестр выше" in text
    assert "Рекомендую A." in text
    assert "60 мин" in text and "приму A сам" in text
    assert "/answer Q-1 A" in text

# -*- coding: utf-8 -*-
"""Порог подробности (S2B плана 10): мелкое — строка реестра и замер, а не стоп.

Две боли, ради которых шаг заказан: вердикт ревью плана поднимала находка, которая ничего
не меняет (класс `стиль` со статусом `открыто` заказывал круг правок и тратил
`max_fix_rounds`), и вопрос шага с машинным ответом ждал человека (план 15 стоял на Q-1
«копия не равна выпуску» 1 ч 30 мин — ответ даёт `sha256sum` плюс `grep -c`).

Правило блокировки — конъюнкция статуса и класса (`gate.PROMPT_FINDING_STATUS`), а не
разбор текста находки: класс берётся из колонки «Класс». Замер вопроса — команда из белого
списка в обратных кавычках абзаца, исполнение через `gate._run` (argv без shell, таймаут
60 с), отметка в состоянии плана, строка `замерил: …` в истории тиков.

Ни один случай не зависит от бинаря на PATH: `gate._run` подменён, команда в тексте вопроса
— только повод для замера. Иначе прогон зависел бы от окружения (на Windows `grep` и
`sha256sum` живут в MSYS и нативному PATH не видны).
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import time
from pathlib import Path

import pytest

import config as config_mod
import gate
from engine_kit import cards, write_approval

# План действующего формата: конституция, оформленная разведка (P1-P3 пройдены) и журнал,
# где все строки открыты: закрытый шаг сделал бы план «идущим», и ревью плана не заказалось бы.
PLAN_READY = """# 1. Демо-план

Конституция: docs/CONSTITUTION.md

## Разведка

Артефакт: `docs/research/1-prior-art.md`

| Кандидат | Ссылка | Вердикт |
|---|---|---|
| demo/spec-kit | https://example.com/spec-kit | берём |
| demo/agent-factory | https://example.com/agent-factory | своё |

Решение: go — берём практики demo/spec-kit
Правки по разведке: 2

## Шаги

### S1. Старт

- Цель: заложить цикл.

### S2. Интеграция demo/spec-kit

- Цель: подключить готовое.

## Журнал выполнения

| Дата | Шаг | Коммиты | Прогоны и доказательства | Статус |
|---|---|---|---|---|
| — | S1 | — | — | открыт |
| — | S2 | — | — | открыт |
"""

# Вопрос с командой замера: абзац автора, команда — в обратных кавычках. Первый токен
# (`grep`) в белом списке, значит вопрос измерим и владельцу не идёт.
QUESTION_LINE = ("Q1: копия равна выпуску — A) да, равна (цена: полминуты) / "
                 "B) нет (цена: разбор) — рекомендация A — замер: "
                 "`grep -c 'v0.37' plugin.yaml`")


def quiet(_text: str) -> None:
    """Заглушка уведомлений: тексты проверяются там, где они обязательны."""


# ───────────────────────────── стенд ревью плана ─────────────────────────────

@pytest.fixture()
def stand(project: Path, hermes_stub: Path, monkeypatch):
    """Стенд плана 1: план нового формата, утверждение, разведка, постановка S1."""
    path = project / "docs" / "plans" / "1-demo.md"
    path.write_text(PLAN_READY, encoding="utf-8")
    write_approval(project, path)
    research = project / "docs" / "research"
    research.mkdir(parents=True, exist_ok=True)
    (research / "1-prior-art.md").write_text("Итог: берём demo/spec-kit\n", encoding="utf-8")
    (project / "docs" / "prompts" / "2026-11-07_1-S1_prompt.md").write_text(
        "# Постановка S1\n\n## Что сделать\n\nвзять цикл.\n", encoding="utf-8")
    monkeypatch.setenv("PIPELINE_REPO", str(project))
    return project, gate.resolve_paths(config_mod.load(project), "1"), hermes_stub


def review_card(status: str = "done") -> dict:
    """Карточка ревью плана в `ctx`: форма `gate.read_cards`, `created_at` в прошлом."""
    now = int(time.time())
    return {"id": "t_card01", "title": "[ревью плана 1]", "status": status,
            "assignee": "reviewer", "key": "planloop:1:planreview", "completed_at": now,
            "result": "", "error": "", "created_at": now - 60, "started_at": now - 60}


def ctx_with_card(p) -> dict:
    ctx = gate.build_ctx(p)
    ctx["plan_review_cards"] = [review_card()]
    return ctx


def write_report(root: Path, verdict: str) -> Path:
    """Отчёт ревью плана: метка версии плана, конституция и ровно одна строка вердикта."""
    plan = Path(root) / "docs" / "plans" / "1-demo.md"
    path = Path(root) / "docs" / "reviews" / "plan-1.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "# Ревью плана 1\n\n- находок нет\n\nКонституция: п.1 — нарушений нет\n"
        f"plan_sha256: {hashlib.sha256(plan.read_bytes()).hexdigest()}\n\n"
        f"## ВЕРДИКТ: {verdict}\n", encoding="utf-8")
    return path


def write_registry(p, *rows: tuple[str, str, str]) -> Path:
    """Реестр находок плана: `(ID, класс, статус)` на строку, текст и действие — шаблоном."""
    items = [{"id": fid, "step": "S1", "kind": kind, "text": f"находка {fid}",
              "action": f"учесть {fid}", "status": status}
             for fid, kind, status in rows]
    path = gate.plan_findings_path(p)
    gate.write_findings(path, items)
    return path


def registry_status(p) -> list[str]:
    """Статусы строк реестра плана так, как они лежат в файле."""
    return [row["status"] for row in gate.load_findings(gate.plan_findings_path(p))]


def spy_cards(bodies: list[str]):
    """`create_card`, который кладёт тела карточек в копилку (образец `test_plan_review`)."""

    def create(p, title, body, assignee, key, skills, parent=None, max_runtime=None):
        bodies.append(body)
        return gate._create_card(p, title, body, assignee, key, skills, parent, max_runtime)

    return create


# ───────────────────────────── 1-3. порог реестра плана ─────────────────────────────

def test_style_row_in_plan_registry_does_not_block(stand):
    """Строка `стиль` + `открыто` вердикт не поднимает, а свой статус в файле не меняет."""
    root, p, _db = stand
    write_report(root, "REQUEST_CHANGES")
    write_registry(p, ("F1", "стиль", "открыто"))

    view = gate.effective_plan_verdict(ctx_with_card(p))
    assert view["verdict"] == "APPROVE", view
    assert view["open"] == [] and view["total"] == 1
    assert view["report"] == "REQUEST_CHANGES" and view["discrepancy"] is True

    gate.run_gate(p, create_card=spy_cards([]), notify_fn=quiet)
    assert registry_status(p) == ["открыто"]      # статусы не пересчитываются


def test_forma_strazha_row_does_not_block(stand):
    """Класс `формат-стража` — тот же порог: строка остаётся, вердикт по ней не поднимается."""
    root, p, _db = stand
    write_report(root, "REQUEST_CHANGES")
    write_registry(p, ("F1", "формат-стража", "открыто"))

    view = gate.effective_plan_verdict(ctx_with_card(p))
    assert view["verdict"] == "APPROVE", view
    assert view["open"] == [] and view["total"] == 1


def test_zamysel_row_still_blocks(stand):
    """`замысел` + `открыто` держит вердикт: понижение не превратилось в отключение."""
    root, p, _db = stand
    write_report(root, "APPROVE")
    write_registry(p, ("F1", "замысел", "открыто"), ("F2", "стиль", "открыто"))

    view = gate.effective_plan_verdict(ctx_with_card(p))
    assert view["verdict"] == "REQUEST_CHANGES", view
    assert view["open"] == ["F1"]           # блокирующие ID, а не все открытые строки
    assert view["discrepancy"] is True

    decision = gate._plan_review_decision(ctx_with_card(p))
    assert decision["action"] == "create_plan_fix", decision
    assert "F1" in decision["reason"] and "F2" not in decision["reason"]


def test_style_finding_spends_no_fix_round(stand):
    """Реестр из `стиль` и `формат-стража` со `открыто` карточки правок не заказывает."""
    root, p, db = stand
    write_report(root, "REQUEST_CHANGES")
    write_registry(p, ("F1", "стиль", "открыто"), ("F2", "формат-стража", "открыто"))

    got = gate.run_gate(p, ctx=ctx_with_card(p), create_card=spy_cards([]), notify_fn=quiet)
    assert got["decision"]["action"] != "create_plan_fix", got["decision"]
    assert [c for c in cards(db) if ":planfix:" in (c["key"] or "")] == []
    # Словари классов не расширены: «подробность» — пометка внутри строки, а не класс.
    assert gate.FINDING_KINDS == ("замысел", "двусмысленность", "формат-стража",
                                 "гейт-исполнение", "стиль")


# ───────────────────────────── 5-10. замер вопроса ─────────────────────────────

def write_question_prompt(root: Path, question: str = QUESTION_LINE) -> Path:
    """Постановка S1 с абзацем-вопросом: источник строки вопроса для `register_questions`."""
    path = Path(root) / "docs" / "prompts" / "2026-11-16_1-S1_prompt.md"
    path.write_text(f"# Постановка S1\n\n## 8. Вопросы\n\n{question}\n", encoding="utf-8")
    return path


def set_questions_enforce(root: Path, value: bool) -> Path:
    """`questions.enforce` в живом `pipeline.json`: ключ читает движок, не `config.py`."""
    path = Path(root) / "pipeline.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["questions"] = {"enforce": value}
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def stub_run(monkeypatch, *, program: str = "grep", returncode: int = 0, stdout: str = "2\n",
             error: BaseException | None = None) -> list[list[str]]:
    """Подменить `gate._run`: вызовы замера считаются, остальные команды идут как раньше.

    Замер опознаётся по программе вопроса (`grep` в фикстуре): движок сам зовёт
    `git rev-parse HEAD`, и «любой разрешённый первый токен» посчитал бы его замером.
    """
    real = gate._run
    calls: list[list[str]] = []

    def fake(cmd, cwd=None, timeout=120, env=None):
        argv = [str(token) for token in cmd]
        if argv and argv[0] == program:
            calls.append(argv)
            if error is not None:
                raise error
            return subprocess.CompletedProcess(argv, returncode, stdout, "")
        return real(cmd, cwd=cwd, timeout=timeout, env=env)

    monkeypatch.setattr(gate, "_run", fake)
    return calls


def history_measures(cfg) -> list[dict]:
    """Строки истории тиков с действием `measure`: их и читает владелец."""
    path = gate.history_path(cfg)
    if not path.is_file():
        return []
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
               if line.strip()]
    return [record for record in records if record.get("action") == "measure"]


def question_rows(root: Path) -> list[dict]:
    return gate.load_questions(Path(root) / "docs" / "reviews" / "plan-1-questions.md")


@pytest.fixture()
def q_stand(project: Path, hermes_stub: Path, monkeypatch):
    """Стенд вопросов: проект плана 1, конфиг и пути плана (образец `test_question_hold`)."""
    monkeypatch.setenv("PIPELINE_REPO", str(project))
    cfg = config_mod.load(project)
    return project, gate.resolve_paths(cfg, "1"), cfg


def tick(p, messages: list[str] | None = None) -> dict:
    """Один тик без борды: карточки не нужны, уведомления — списком."""
    return gate.run_gate(p, execute=True, create_card=spy_cards([]),
                         notify_fn=(messages.append if messages is not None else quiet))


def test_measurable_question_closed_by_measurement(q_stand, monkeypatch):
    """`enforce: true`: измеримый вопрос не заводится, ответ — строкой «замерил: …», отметка в состоянии."""
    root, p, cfg = q_stand
    write_question_prompt(root)
    calls = stub_run(monkeypatch)

    got = tick(p)

    assert calls and calls[0][0] == "grep", calls
    assert question_rows(root) == []                     # строки вопроса нет вовсе
    measures = history_measures(cfg)
    assert len(measures) == 1 and "замерил:" in measures[0]["detail"], measures
    assert measures[0]["step"] == "S1"
    assert got["decision"].get("question") is None       # шаг не стоит на вопросе
    marks = gate.load_state(p).get(gate.QUESTION_MEASURE_STATE_KEY) or {}
    assert len(marks) == 1, marks                        # отметка пережила тик


def test_measurable_question_kept_when_enforce_false(q_stand, monkeypatch):
    """`enforce: false`: правило понижено до замера — вопрос владельцу остаётся, замер в истории."""
    root, p, cfg = q_stand
    set_questions_enforce(root, False)
    cfg = config_mod.load(root)             # конфиг читается при старте тика, а не один раз
    p = gate.resolve_paths(cfg, "1")
    write_question_prompt(root)
    stub_run(monkeypatch)

    got = tick(p)

    rows = question_rows(root)
    assert len(rows) == 1 and rows[0]["status"] == "открыт", rows
    assert got["decision"].get("question") == rows[0]["id"]
    assert got["decision"]["action"] == "idle", got["decision"]
    assert "вопрос" in got["decision"]["reason"]
    assert [m for m in history_measures(cfg) if "замерил:" in m["detail"]]


def test_slow_measure_keeps_question(q_stand, monkeypatch):
    """Таймаут замера вопрос не отменяет: строка заведена, в истории — «замер не удался»."""
    root, p, cfg = q_stand
    write_question_prompt(root)
    stub_run(monkeypatch, error=subprocess.TimeoutExpired("grep", 60))

    tick(p)

    assert len(question_rows(root)) == 1
    measures = history_measures(cfg)
    assert len(measures) == 1 and "замер не удался" in measures[0]["detail"], measures
    assert "таймаут 60 с" in measures[0]["detail"]
    assert "замерил:" not in measures[0]["detail"]


def test_empty_measure_keeps_question(q_stand, monkeypatch):
    """Код 0 и пустой вывод — не ответ: вопрос остаётся, причина названа."""
    root, p, cfg = q_stand
    write_question_prompt(root)
    stub_run(monkeypatch, stdout="")

    tick(p)

    assert len(question_rows(root)) == 1
    measures = history_measures(cfg)
    assert len(measures) == 1 and "пустой вывод" in measures[0]["detail"], measures


def test_repo_plan_registries_have_no_blocking_style():
    """Проверка движком: блокирующих строк `стиль`/`формат-стража` в реестрах плана нет.

    Тот же вопрос глазами дал бы вакуумный зелёный, поэтому тест доказывает, что файлы
    прочитаны: множество путей непусто, а строк-находок столько же, сколько даёт подсчёт
    строк таблицы по тексту файлов (независимо от `load_findings`).

    В дереве без реестров (чистый клон: `docs/reviews/` лежит в `.gitignore`) проверка
    неприменима и пропускается с причиной — провал там краснил `main` на ровном месте.
    """
    root = Path(__file__).resolve().parents[2]
    files = sorted(root.glob("docs/reviews/plan-*-findings.md"))
    if not files:
        pytest.skip("реестры плана — артефакт прогона конвейера (docs/reviews в .gitignore), "
                    "в чистом клоне их нет")

    raw_rows = 0
    rows: list[dict] = []
    for path in files:
        raw_rows += len(re.findall(r"(?m)^\|\s*F\d+", path.read_text(encoding="utf-8")))
        rows.extend(gate.load_findings(path))
    assert len(rows) == raw_rows and raw_rows > 0, (len(rows), raw_rows)

    view = gate.effective_plan_verdict({"plan_review_verdict": {"verdict": "APPROVE"},
                                        "plan_findings": rows, "plan_findings_exists": True})
    blocking = [row["id"] for row in rows
                if row.get("status") == "открыто"
                and row.get("kind") in ("стиль", "формат-стража")
                and row["id"] in (view["open"] or [])]
    assert blocking == [], blocking


def test_second_tick_does_not_measure_again(q_stand, monkeypatch):
    """Два тика на одном отчёте: строка вопроса одна, замер исполнен один раз, обе — `idle`."""
    root, p, cfg = q_stand
    set_questions_enforce(root, False)
    cfg = config_mod.load(root)
    p = gate.resolve_paths(cfg, "1")
    write_question_prompt(root)
    calls = stub_run(monkeypatch)

    first = tick(p)
    second = tick(p)

    assert len(calls) == 1, calls
    assert len(question_rows(root)) == 1
    assert len(history_measures(cfg)) == 1
    for got in (first, second):
        assert got["decision"]["action"] == "idle", got["decision"]
        assert got["decision"].get("question") == "Q-1", got["decision"]

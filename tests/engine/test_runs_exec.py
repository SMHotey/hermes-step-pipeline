# -*- coding: utf-8 -*-
"""Прогоны шага исполняет страж (план 9, S2).

Пятнадцать тестов постановки: три вида ожидания (`exit`, `содержит`, `regex`), таймаут,
кэш по паре sha-команда, бюджет шага (хвост догоняет следующий тик, а признак исчерпания —
пропуск, не сумма), зелёный прогон как карточка ревью, красный — карточка правок с
находкой F03 вместо ревью (включая исчерпание лимита кругов), строка прогона в журнале и
блок прогона в панели.

Команды прогонов — только первый токен `python` и встроенные модули stdlib: строка
пункта «Прогон» обязана исполняться одинаково в bash и в cmd.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
from pathlib import Path

import pytest

import config as config_mod
import gate
import next_step
from engine_kit import complete, write_approval
from stub_hermes import SCHEMA
from step_pipeline import snapshot as snap

#: Команда, которая кладёт ровно один символ в файл за каждый свой запуск: по нему
#: считается, сколько раз прогон исполнен (кэш, «второй тик»).
CMD_MARK = "python -c \"open('mark.txt','a').write('x')\""
#: Имя файла-маркера (в корне проекта — там cwd команды).
MARKER = "mark.txt"

CMD_OK = "python -c \"print(42)\""
CMD_MISSING = "python -m module_s2_missing"
CMD_ZEN = "python -m this"
CMD_SLEEP = "python -c \"import time; time.sleep(10)\""
CMD_SLOW = "python -c \"import time; time.sleep(1.5)\""
CMD_RED_SLOW = "python -c \"import sys, time; time.sleep(1.5); sys.exit(1)\""

EMPTY_RUNS = {"sha": "", "total": 0, "green": 0, "seconds": 0, "items": []}


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """Конфиг стенда — из проекта, а не из чужих `PIPELINE_*` в сессии.

    `PIPELINE_KANBAN_DB` не трогаем: его ставит `hermes_stub`, и по нему же читает
    борду панель (`snapshot`). `PIPELINE_REPO` задаёт `stand` (его читает сборщик шагов
    в подпроцессе — см. `next_step._repo_root`), а снимается здесь же на выходе:
    каталог стенда после теста удалён, и оставленная переменная сломала бы соседние
    модули.
    """
    for name in ("PIPELINE_CONFIG", "PIPELINE_PLANS", "PIPELINE_STATE_DIR",
                 "PIPELINE_BOARD", "PIPELINE_REPO"):
        monkeypatch.delenv(name, raising=False)
    yield
    os.environ.pop("PIPELINE_REPO", None)


def git(root: Path, *args: str) -> str:
    """git в стенде: коммит обязателен — доказательство именуется по HEAD."""
    res = subprocess.run(["git", *args], cwd=str(root), capture_output=True,
                         text=True, encoding="utf-8")
    assert res.returncode == 0, res.stderr
    return res.stdout.strip()


def plan_text(runs: list[str]) -> str:
    """План 1 из одного шага: шапка, раздел шагов и журнал — иначе шага движок не видит."""
    lines = ["# 1. Демо-план конвейера", "",
             "Цель: прогоны шага исполняет страж.", "",
             "## Шаги", "", "### S1. Шаг с прогонами (1 дн)", "",
             "- Файл: `app/step.py`",
             "- Тесты/гейты: `python -m pytest tests/test_step.py -q` → 1 passed",
             *runs,
             "- Готово = прогоны зелёные", "",
             "## Журнал выполнения", "",
             "| Дата | Шаг | Коммиты | Прогоны и доказательства | Статус |",
             "|---|---|---|---|---|",
             "| — | S1 | — | — | открыт |", ""]
    return "\n".join(lines) + "\n"


def stand(root: Path, db: Path, runs: list[str], *, budget: int | None = None,
          max_fix_rounds: int | None = None, seed: bool = True) -> gate.Paths:
    """План с пунктами «Прогон», утверждение, постановка, git-коммит и карточка шага.

    Запись утверждения пишется ПОСЛЕ переписывания плана: иначе sha записи не сошёлся бы
    с файлом, и страж ушёл бы в требование утверждения вместо прогона.
    """
    plan = root / "docs" / "plans" / "1-demo.md"
    plan.write_text(plan_text(runs), encoding="utf-8")
    write_approval(root, plan)
    prompt = root / "docs" / "prompts" / "2026-11-15_1-S1_prompt.md"
    prompt.parent.mkdir(parents=True, exist_ok=True)
    prompt.write_text("# Постановка S1\n", encoding="utf-8")
    if budget is not None or max_fix_rounds is not None:
        cfg_path = root / "pipeline.json"
        raw = json.loads(cfg_path.read_text(encoding="utf-8"))
        if budget is not None:
            raw["step_runs"] = {"timeout": 60, "budget": budget}
        if max_fix_rounds is not None:
            raw["max_fix_rounds"] = max_fix_rounds
        cfg_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2),
                            encoding="utf-8")
    git(root, "init", "-b", "main")
    git(root, "config", "user.email", "stand@example.com")
    git(root, "config", "user.name", "Stand")
    git(root, "add", "-A")
    git(root, "commit", "-m", "начало стенда")
    if seed:
        seed_step(db)
    # Корень для сборщика шагов: он идёт подпроцессом, и `__file__` у него — каталог
    # репозитория, а не `.pipeline/` стенда (`next_step._repo_root`).
    os.environ["PIPELINE_REPO"] = str(root)
    return gate.resolve_paths(config_mod.load(root), "1")


def seed_step(db: Path, status: str = "done") -> None:
    """Карточка шага S1 на поддельной борде (колонки — `stub_hermes.SCHEMA`).

    Тик, решающий заказать ревью, обязан увидеть закрытый шаг: так первый тик стенда
    уже заказывает ревью (а не карточку шага), и команда прогона считается с него.
    """
    con = sqlite3.connect(db)
    try:
        con.executescript(SCHEMA)
        con.execute(
            "INSERT OR REPLACE INTO tasks (id, title, status, assignee, idempotency_key,"
            " completed_at, result, last_failure_error, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            ("t_step_S1", "карточка шага", status, "demo", "planloop:1:S1:step",
             1, "", "", 1))
        con.commit()
    finally:
        con.close()


def next_step_dict(root: Path) -> dict:
    """Шаг ровно в том виде, в каком его отдаёт сборщик стражу (`--json`)."""
    return next_step.build_package(config_mod.load(root), "1")["next"]


def tick(p: gate.Paths, created: list[tuple[str, str]] | None = None,
         says: list[str] | None = None) -> dict:
    """Один тик `run_gate`: карточки идут на поддельную борду, уведомления молчат."""

    def create(pp, title, body, assignee, key, skills, parent=None, max_runtime=None):
        if created is not None:
            created.append((key, body))
        return gate._create_card(pp, title, body, assignee, key, skills,
                                 parent=parent, max_runtime=max_runtime)

    notify = says.append if says is not None else (lambda _text: None)
    return gate.run_gate(p, execute=True, create_card=create, notify_fn=notify)


def proof_of(p: gate.Paths) -> dict | None:
    """Доказательство текущего HEAD — того, что читает решение."""
    return gate.load_runs_proof(p, "S1", gate.head_commit(p))


def red_rows(proof: dict) -> list[dict]:
    """Строки доказательства, которые не зелёные: красные, таймаут и пропущенные."""
    return [row for row in proof.get("runs") or [] if row.get("outcome") != "green"]


# ─────────────────────────── три вида ожидания ───────────────────────────


def test_exit_zero_is_green(project: Path, hermes_stub: Path):
    """`→ exit 0`: код 0 — зелёный прогон, доказательство с кодом, секундами и хвостом."""
    p = stand(project, hermes_stub, [f"- Прогон: `{CMD_OK}` → exit 0"])

    proof = gate.run_step_runs(p, next_step_dict(project))

    assert proof is not None
    assert (proof["total"], proof["green"]) == (1, 1)
    row = proof["runs"][0]
    assert row["outcome"] == "green" and row["code"] == 0
    assert "42" in row["tail"] and len(row["tail"]) <= gate.RUN_TAIL_CHARS
    assert proof["sha"] == gate.head_commit(p) and proof["step"] == "S1"
    assert proof["seconds"] >= 0 and row["seconds"] >= 0


def test_exit_one_is_red(project: Path, hermes_stub: Path):
    """`→ exit 0` при коде 1 — красный прогон: код возврата лежит в строке."""
    p = stand(project, hermes_stub, [f"- Прогон: `{CMD_MISSING}` → exit 0"])

    proof = gate.run_step_runs(p, next_step_dict(project))

    row = proof["runs"][0]
    assert (row["outcome"], row["code"]) == ("red", 1)
    assert (proof["total"], proof["green"]) == (1, 0)


def test_contains_expectation(project: Path, hermes_stub: Path):
    """Подстрока есть — зелёный, нет — красный: два пункта в одном шаге."""
    p = stand(project, hermes_stub, [
        f'- Прогон: `{CMD_ZEN}` → содержит "Python"',
        f'- Прогон: `{CMD_ZEN}` → содержит "нет такого текста"',
    ])

    proof = gate.run_step_runs(p, next_step_dict(project))

    assert (proof["total"], proof["green"]) == (2, 1)
    assert [row["outcome"] for row in proof["runs"]] == ["green", "red"]
    assert len(red_rows(proof)) == 1


def test_regex_expectation(project: Path, hermes_stub: Path):
    """`regex`: выражение сошлось — зелёный, не сошлось — красный."""
    p = stand(project, hermes_stub, [
        f'- Прогон: `{CMD_ZEN}` → regex "The Zen of Python"',
        f'- Прогон: `{CMD_ZEN}` → regex "no_such_pattern_42"',
    ])

    proof = gate.run_step_runs(p, next_step_dict(project))

    assert (proof["total"], proof["green"]) == (2, 1)
    assert [row["outcome"] for row in proof["runs"]] == ["green", "red"]


def test_timeout_is_red(project: Path, hermes_stub: Path):
    """Таймаут убивает процесс (`outcome timeout`), тик доходит до решения и не падает."""
    p = stand(project, hermes_stub,
              [f"- Прогон: `{CMD_SLEEP}` → exit 0; таймаут 1 сек"])

    proof = gate.run_step_runs(p, next_step_dict(project))
    row = proof["runs"][0]
    assert (row["outcome"], row["code"]) == ("timeout", None)
    assert "таймаут" in row["tail"] or "таймаут" in str(row.get("reason") or "")

    res = tick(p)
    assert res["decision"]["action"] == "create_fix"   # таймаут — не зелёный


def test_python_token_uses_engine_python(project: Path, hermes_stub: Path):
    """`python` в пункте — интерпретатор движка: сверка с символом, не с текстом."""
    p = stand(project, hermes_stub, [f"- Прогон: `{CMD_OK}` → exit 0"])

    argv = gate.runs_argv(p, CMD_OK)

    assert argv[0] == gate._python(p) and Path(argv[0]).is_file()
    assert argv[1:] == ["-c", "print(42)"]
    assert gate.run_step_runs(p, next_step_dict(project))["green"] == 1


# ─────────────────────── кэш, бюджет, повторные тики ───────────────────────


def test_cache_reuses_proof_by_sha(project: Path, hermes_stub: Path):
    """Кэш по паре sha-команда: файл есть — ни одной команды, хвост не прибавился."""
    p = stand(project, hermes_stub, [f"- Прогон: `{CMD_MARK}` → exit 0"])
    step = next_step_dict(project)

    first = gate.run_step_runs(p, step)
    assert (project / MARKER).read_text(encoding="utf-8") == "x"
    second = gate.run_step_runs(p, step)

    assert (project / MARKER).read_text(encoding="utf-8") == "x"
    assert second == first
    assert second["sha"] == first["sha"] == gate.head_commit(p)


def test_second_tick_does_not_run_twice(project: Path, hermes_stub: Path):
    """Карточка ревью заказана, прогон исполнен один раз; второй тик ничего не делает."""
    p = stand(project, hermes_stub, [f"- Прогон: `{CMD_MARK}` → exit 0"])
    created: list[tuple[str, str]] = []

    first = tick(p, created=created)
    assert first["decision"]["action"] == "create_review"
    assert len([key for key, _ in created if key.endswith(":review:1")]) == 1
    assert (project / MARKER).read_text(encoding="utf-8") == "x"

    tick(p, created=created)
    assert not created[1:], f"второй тик создал карточки: {[k for k, _ in created[1:]]}"
    assert (project / MARKER).read_text(encoding="utf-8") == "x"


def test_budget_stops_remaining_runs(project: Path, hermes_stub: Path):
    """Бюджет шага: хвост не уходит, стоп пишет `run_gate`, а не функция исполнения."""
    p = stand(project, hermes_stub,
              [f"- Прогон: `{CMD_SLOW}` → exit 0", f"- Прогон: `{CMD_MARK}` → exit 0"],
              budget=1)
    stop_file = project / ".pipeline" / "state" / "plan_1_stop.json"

    proof = gate.run_step_runs(p, next_step_dict(project))

    assert proof["runs"][1]["outcome"] == "skipped"
    assert proof["budget"]["exhausted"] is True
    assert not (project / MARKER).exists()          # хвост не исполнен
    assert not stop_file.exists()                   # исполнение стоп не ставит

    res = tick(p)
    assert res["decision"]["action"] == "stop"
    stop = json.loads(stop_file.read_text(encoding="utf-8"))
    assert stop["kind"] == "incident"
    assert "бюджет" in stop["reason"] and "1 из 2" in stop["reason"]


def test_all_green_over_budget_orders_review(project: Path, hermes_stub: Path):
    """Сумма прогонов перевалила за лимит, но ничего не пропущено — ревью, а не стоп.

    Находка 9-S2 F1: признак исчерпания — факт пропуска. Два зелёных прогона, чья сумма
    дошла до лимита, доказательство собрали полностью, и стоп на них — ложный инцидент.
    """
    p = stand(project, hermes_stub,
              [f"- Прогон: `{CMD_SLOW}` → exit 0", f"- Прогон: `{CMD_SLOW}` → exit 0"],
              budget=3)
    created: list[tuple[str, str]] = []

    res = tick(p, created=created)

    proof = proof_of(p)
    assert [row["outcome"] for row in proof["runs"]] == ["green", "green"]
    assert proof["budget"]["spent"] >= 3                   # сумма дошла до лимита
    assert proof["budget"]["exhausted"] is False           # но пропуска не было
    assert res["decision"]["action"] == "create_review"
    assert not (project / ".pipeline" / "state" / "plan_1_stop.json").exists()


def test_stop_counts_executed_not_green(project: Path, hermes_stub: Path):
    """«Исполнено N из M» — исполненные прогоны, а не зелёные (находка 9-S2 F1).

    Красный прогон съел бюджет, хвост пропущен: исполнено один из двух, зелёных ноль.
    Причина стопа обязана назвать первое число, иначе человек читает «0 из 2» и ищет
    два несуществующих пропуска.
    """
    p = stand(project, hermes_stub,
              [f"- Прогон: `{CMD_RED_SLOW}` → exit 0", f"- Прогон: `{CMD_MARK}` → exit 0"],
              budget=1)

    proof = gate.run_step_runs(p, next_step_dict(project))

    assert [row["outcome"] for row in proof["runs"]] == ["red", "skipped"]
    assert proof["budget"]["exhausted"] is True
    assert not (project / MARKER).exists()

    tick(p)
    stop = json.loads((project / ".pipeline" / "state" / "plan_1_stop.json")
                      .read_text(encoding="utf-8"))
    assert "1 из 2 исполнено" in stop["reason"], stop["reason"]
    assert "0 из 2" not in stop["reason"]


# ─────────────────── зелёный → ревью, красный → правки ───────────────────


def test_green_run_orders_review(project: Path, hermes_stub: Path):
    """Зелёное доказательство — карточка ревью, карточки правок не создаётся."""
    p = stand(project, hermes_stub, [f"- Прогон: `{CMD_OK}` → exit 0"])
    created: list[tuple[str, str]] = []

    res = tick(p, created=created)

    assert res["decision"]["action"] == "create_review"
    assert any(key.endswith(":review:1") for key, _ in created)
    assert not any(":fix:" in key for key, _ in created)
    assert proof_of(p)["green"] == 1


def test_red_run_creates_fix_card_without_review(project: Path, hermes_stub: Path):
    """Красный прогон — карточка правок с F03, ревью не тратится; лимит — стоп."""
    p = stand(project, hermes_stub, [f"- Прогон: `{CMD_MISSING}` → exit 0"],
              max_fix_rounds=1)
    created: list[tuple[str, str]] = []
    says: list[str] = []

    res = tick(p, created=created, says=says)

    assert res["decision"]["action"] == "create_fix"
    fixes = [(key, body) for key, body in created if key.endswith(":fix:1")]
    assert len(fixes) == 1
    assert not any(key.endswith(":review:1") for key, _ in created)
    key, body = fixes[0]
    assert key == "planloop:1:S1:fix:1"
    assert "F03" in body and CMD_MISSING in body and "код 1" in body
    assert "Прогоны: 0/1" in body and "Осталось кругов: 0 из 1" in body
    assert proof_of(p)["fix_card"].startswith("t_")
    assert any("Прогоны: 0/1" in text for text in says)

    # Второй круг: карточка закрыта, HEAD сдвинулся, лимит исчерпан — стоп.
    complete(hermes_stub, ":fix:1")
    git(project, "commit", "--allow-empty", "-m", "правка")
    res = tick(p, created=created, says=says)

    assert res["decision"]["action"] == "stop"
    stop = json.loads((project / ".pipeline" / "state" / "plan_1_stop.json")
                      .read_text(encoding="utf-8"))
    assert stop["kind"] == "incident"
    assert "исчерпан лимит" in stop["reason"] and "normal" in stop["reason"]
    assert "1" in stop["reason"]
    assert not any(key.endswith(":fix:2") for key, _ in created)


# ─────────────────────── журнал и панель ───────────────────────


def test_journal_has_runs_counter(project: Path, hermes_stub: Path):
    """Четвёртая колонка журнала несёт счётчик прогона той же строкой, что карточка."""
    p = stand(project, hermes_stub, [f"- Прогон: `{CMD_OK}` → exit 0"])
    gate.run_step_runs(p, next_step_dict(project))
    sha = gate.head_commit(p)
    verdict = {"commit": sha, "path": "docs/reviews/1-S1.md", "verdict": "APPROVE"}

    text = gate._evidence(p, verdict, 0, "S1")
    assert "Прогоны: 1/1, " in text
    assert gate.append_journal_row(p, "S1", "Шаг с прогонами", sha, text) is True

    rows = [[c.strip() for c in ln.strip().strip("|").split("|")]
            for ln in Path(p.plan_file).read_text(encoding="utf-8").splitlines()
            if ln.strip().startswith("|")]
    journal = [r for r in rows if len(r) >= 5 and r[1].startswith("S1")]
    assert len(journal) == 1, f"строк журнала по S1: {len(journal)}"
    assert "Прогоны: 1/1, " in journal[0][3], journal[0][3]


def test_snapshot_shows_runs_state(project: Path, hermes_stub: Path):
    """Панель видит блок прогона; красный прогон не откатывает процент шага."""
    p = stand(project, hermes_stub, [f"- Прогон: `{CMD_MISSING}` → exit 0"])
    before = snap.step_detail(project, "1", "S1")
    assert before["runs"] == EMPTY_RUNS
    percent_before = before["progress"]["percent"]

    tick(p)

    after = snap.step_detail(project, "1", "S1")
    runs = after["runs"]
    assert runs["sha"] == gate.head_commit(p) and runs["total"] == 1
    assert runs["green"] == 0 and runs["seconds"] >= 0
    assert runs["items"][0]["outcome"] == "red"
    assert after["progress"]["rounds"], "карточка правок должна идти строкой rounds"
    assert after["progress"]["percent"] == percent_before, \
        f"процент откатился: {percent_before} → {after['progress']['percent']}"

    # Файлов доказательства нет — пустая форма с `sha ""`, а не блок с «—».
    for path in (project / ".pipeline" / "state" / "runs").glob("*.json"):
        path.unlink()
    assert snap.step_detail(project, "1", "S1")["runs"] == EMPTY_RUNS

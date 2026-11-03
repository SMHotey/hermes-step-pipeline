# -*- coding: utf-8 -*-
"""Предзаказ постановки N+1 и гейт закрытия шага без неё (план 2, §S6).

Проверяется поведение стража на временном проекте с поддельной бордой:
постановка N+1 заказывается, пока шаг N в работе (повторных заказов нет), последний
шаг заказа не получает, устаревшая постановка блокирует старт до APPROVE ревьюера,
свежая — стартует без ревью, `prefetch_prompt: false` возвращает прежнее поведение,
а шаг не закрывается, пока постановки N+1 нет.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import config as config_mod
import gate
from engine_kit import cards, complete, plan_body, write_plan, write_prompt

APPROVE = "## ВЕРДИКТ: APPROVE\n\nГейты: 1 passed\n"


@pytest.fixture()
def stand(project: Path, hermes_stub: Path, monkeypatch):
    """Пути плана 1 на временном проекте; сборщик шагов смотрит в PIPELINE_REPO."""
    monkeypatch.setenv("PIPELINE_REPO", str(project))
    cfg = config_mod.load(project)
    return project, cfg, gate.resolve_paths(cfg, "1"), hermes_stub


def keys_of(db: Path) -> set[str]:
    return {c["key"] for c in cards(db)}


def quiet(_text: str) -> None:
    return None


def step_to_review(root: Path, p, db: Path) -> None:
    """Довести шаг S1 до состояния «ревью закрыто, отчёт APPROVE есть»."""
    assert gate.run_gate(p, execute=True, notify_fn=quiet)["decision"]["action"] == "create_step"
    complete(db, ":step")
    assert gate.run_gate(p, execute=True, notify_fn=quiet)["decision"]["action"] == "create_review"
    report = root / "docs" / "reviews" / "1-S1.md"
    report.write_text(APPROVE, encoding="utf-8")
    complete(db, ":review:1")


def git(root: Path, *args: str) -> str:
    res = subprocess.run(["git", *args], cwd=str(root), capture_output=True, text=True,
                         encoding="utf-8", errors="replace")
    assert res.returncode == 0, res.stderr
    return res.stdout.strip()


def test_prefetch_orders_next_prompt_while_step_runs(stand):
    root, cfg, p, db = stand
    write_plan(root, plan_body(4))
    write_prompt(root, "S1")

    first = gate.run_gate(p, execute=True, notify_fn=quiet)
    assert first["decision"]["action"] == "create_step", first["decision"]
    assert "planloop:1:S1:step" in keys_of(db)

    second = gate.run_gate(p, execute=True, notify_fn=quiet)
    assert second["decision"]["action"] == "create_prompt", second["decision"]
    assert second["decision"]["step"] == "S2"
    assert second["applied"] is True
    assert "planloop:1:S2:prompt" in keys_of(db)
    order = next(c for c in cards(db) if c["key"] == "planloop:1:S2:prompt")
    assert order["assignee"] == "prompter"
    assert order["max_runtime"] == "45m"
    # Заказ постановки не начинает следующий шаг: карточки шага S2 ещё нет.
    assert "planloop:1:S2:step" not in keys_of(db)


def test_prefetch_is_idempotent(stand):
    root, cfg, p, db = stand
    write_plan(root, plan_body(4))
    write_prompt(root, "S1")
    gate.run_gate(p, execute=True, notify_fn=quiet)
    gate.run_gate(p, execute=True, notify_fn=quiet)

    third = gate.run_gate(p, execute=True, notify_fn=quiet)
    assert third["decision"]["action"] == "idle", third["decision"]
    assert third["applied"] is False
    assert "S2" in third["decision"]["reason"]
    orders = [c for c in cards(db) if c["key"] == "planloop:1:S2:prompt"]
    assert len(orders) == 1          # второй заказ не ставится


def test_prefetch_skips_last_step(stand):
    root, cfg, p, db = stand
    write_plan(root, plan_body(4, {"S1": "ЗАКРЫТ", "S2": "ЗАКРЫТ"}))
    write_prompt(root, "S3")

    first = gate.run_gate(p, execute=True, notify_fn=quiet)
    assert first["decision"]["action"] == "create_step", first["decision"]
    second = gate.run_gate(p, execute=True, notify_fn=quiet)
    assert second["decision"]["action"] == "idle", second["decision"]
    assert "S3" in second["decision"]["reason"]
    # S4 — последний шаг плана: постановка для него не заказывается (D2).
    assert "planloop:1:S4:prompt" not in keys_of(db)


def test_stale_prompt_blocks_start_until_approve(stand):
    root, cfg, p, db = stand
    write_plan(root, plan_body(3))
    write_prompt(root, "S1", recon="1111111")     # постановка старше HEAD

    first = gate.run_gate(p, execute=True, notify_fn=quiet)
    assert first["decision"]["action"] == "create_prompt_review", first["decision"]
    assert first["applied"] is True
    assert "planloop:1:S1:promptreview" in keys_of(db)
    assert "planloop:1:S1:step" not in keys_of(db)

    second = gate.run_gate(p, execute=True, notify_fn=quiet)
    assert second["decision"]["action"] == "idle", second["decision"]
    assert "ревью постановки S1 идёт" in second["decision"]["reason"]
    assert "planloop:1:S1:step" not in keys_of(db)

    report = root / "docs" / "reviews" / "1-S1-prompt.md"
    report.write_text("recon_commit: 1111111\n\n## ВЕРДИКТ: APPROVE\n", encoding="utf-8")
    complete(db, ":promptreview")
    third = gate.run_gate(p, execute=True, notify_fn=quiet)
    assert third["decision"]["action"] == "create_step", third["decision"]
    assert "planloop:1:S1:step" in keys_of(db)


def test_fresh_prompt_starts_without_review(stand):
    root, cfg, p, db = stand
    write_plan(root, plan_body(3))
    git(root, "init", "-b", "main")
    git(root, "config", "user.email", "test@example.com")
    git(root, "config", "user.name", "Test")
    git(root, "add", "-A")
    git(root, "commit", "-m", "init")
    write_prompt(root, "S1", recon=git(root, "rev-parse", "HEAD"))

    res = gate.run_gate(p, execute=True, notify_fn=quiet)
    assert res["decision"]["action"] == "create_step", res["decision"]
    assert "planloop:1:S1:promptreview" not in keys_of(db)
    assert gate.prompt_fresh(p, gate.prompt_recon(root / "docs" / "prompts"
                                                  / "2026-11-04_1-S1_prompt.md")) is True


def test_switches_off_restore_previous_behaviour(stand):
    """`prefetch_prompt: false` и `prompt_review: never` — ровно поведение до шага."""
    root, cfg, p, db = stand
    write_plan(root, plan_body(3))
    write_prompt(root, "S1", recon="1111111")     # устаревшая постановка
    write_prompt(root, "S2")
    raw = json.loads((root / "pipeline.json").read_text(encoding="utf-8"))
    raw["prefetch_prompt"] = False
    raw["prompt_review"] = "never"
    (root / "pipeline.json").write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    off = gate.resolve_paths(config_mod.load(root), "1")

    assert gate.run_gate(off, execute=True, notify_fn=quiet)["decision"]["action"] == "create_step"
    # Ревью постановки не ставится даже на устаревшую постановку, предзаказа нет.
    assert "planloop:1:S1:promptreview" not in keys_of(db)
    waiting = gate.run_gate(off, execute=True, notify_fn=quiet)
    assert waiting["decision"]["action"] == "idle", waiting["decision"]
    assert "в работе" in waiting["decision"]["reason"]
    assert "planloop:1:S2:prompt" not in keys_of(db)

    complete(db, ":step")
    gate.run_gate(off, execute=True, notify_fn=quiet)
    (root / "docs" / "reviews" / "1-S1.md").write_text(APPROVE, encoding="utf-8")
    complete(db, ":review:1")
    # Постановка S2 есть, но даже без неё закрытие шло бы как раньше: гейт выключен.
    closed = gate.run_gate(off, execute=True, notify_fn=quiet)
    assert closed["decision"]["action"] == "close_step", closed["decision"]
    assert closed["applied"] is True
    assert gate.journal_closed(off, "S1") is True
    nxt = gate.run_gate(off, execute=True, notify_fn=quiet)
    assert nxt["decision"]["action"] == "create_step", nxt["decision"]


def test_close_blocked_without_next_prompt(stand):
    root, cfg, p, db = stand
    write_plan(root, plan_body(3))
    write_prompt(root, "S1")                      # постановки S2 нет
    messages: list[str] = []
    step_to_review(root, p, db)

    res = gate.run_gate(p, execute=True, notify_fn=messages.append)
    phrase = "шаг S1 принят, но закрыть не могу: нет постановки S2"
    decision = res["decision"]
    # Гейт закрытия живёт в решении (D10): шаг заказывает постановку N+1, а не «висит».
    assert decision["action"] == "create_prompt", decision
    assert decision.get("after_close") is True
    assert decision["step"] == "S2"
    assert res["applied"] is True
    assert phrase in res["detail"]
    assert gate.journal_closed(p, "S1") is False
    assert len(messages) == 1 and phrase in messages[0], messages
    assert "planloop:1:S2:prompt" in keys_of(db)

    again = gate.run_gate(p, execute=True, notify_fn=messages.append)
    assert again["decision"]["action"] == "idle", again["decision"]
    assert len(messages) == 1                      # второй раз не уведомляем
    orders = [c for c in cards(db) if c["key"] == "planloop:1:S2:prompt"]
    assert len(orders) == 1

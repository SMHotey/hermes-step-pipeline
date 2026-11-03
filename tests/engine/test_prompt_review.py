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


def write_report(root: Path, verdict: str, recon: str = "1111111") -> Path:
    """Отчёт ревью постановки: отметка версии + одна строка вердикта."""
    path = Path(root) / "docs" / "reviews" / "1-S1-prompt.md"
    path.write_text(f"recon_commit: {recon}\n\n## ВЕРДИКТ: {verdict}\n\nпроверено\n",
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
    """`recon_commit` равен HEAD — постановка свежая, ревью не нужно (режим auto)."""
    root, p, db = stand
    write_plan(root, plan_body(3))
    head = init_repo(root)
    prompt = write_prompt(root, "S1", recon=head)

    res = gate.run_gate(p, execute=True, notify_fn=quiet)
    assert res["decision"]["action"] == "create_step", res["decision"]
    assert "planloop:1:S1:promptreview" not in keys_of(db)
    assert gate.prompt_fresh(p, gate.prompt_recon(prompt)) is True


def test_stale_prompt_waits_for_prompt_review(stand):
    """Коммита `1111111` не существует: постановка устарела, шаг ждёт ревью постановки."""
    root, p, db = stand
    write_plan(root, plan_body(3))
    write_prompt(root, "S1", recon="1111111")

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
    """REQUEST_CHANGES и чужой recon версии — стоп; APPROVE по той же версии — старт."""
    root, p, db = stand
    write_plan(root, plan_body(3))
    write_prompt(root, "S1", recon="1111111")
    gate.run_gate(p, execute=True, notify_fn=quiet)     # карточка ревью постановки
    complete(db, ":promptreview")

    write_report(root, "REQUEST_CHANGES")
    stopped = gate.run_gate(p, execute=True, notify_fn=quiet)
    assert stopped["decision"]["action"] == "stop", stopped["decision"]
    assert "переписать" in stopped["detail"]
    assert "planloop:1:S1:step" not in keys_of(db)
    assert gate.load_stop(p)["kind"] == "incident"      # инцидентный, снимется сам

    # Снимаем стоп-флаг вручную: инцидент ждёт устранения причины, а причина — в отчёте.
    Path(p.stop_file).unlink()
    write_report(root, "APPROVE", recon="2222222")      # ревью не про эту версию (D6)
    mismatched = gate.run_gate(p, execute=True, notify_fn=quiet)
    assert mismatched["decision"]["action"] == "stop", mismatched["decision"]
    assert "не про эту версию" in mismatched["detail"]
    assert "planloop:1:S1:step" not in keys_of(db)

    Path(p.stop_file).unlink()
    write_report(root, "APPROVE", recon="1111111")
    started = gate.run_gate(p, execute=True, notify_fn=quiet)
    assert started["decision"]["action"] == "create_step", started["decision"]
    assert "planloop:1:S1:step" in keys_of(db)


def test_prompt_review_modes(stand):
    """`always` ревьюит и свежую постановку, `never` — никакую; умолчания и опечатка."""
    root, p, db = stand
    write_plan(root, plan_body(3))
    head = init_repo(root)
    write_prompt(root, "S1", recon=head)                # постановка свежая

    set_config(root, prompt_review="always")
    always = gate.run_gate(paths(root), execute=True, notify_fn=quiet)
    assert always["decision"]["action"] == "create_prompt_review", always["decision"]
    assert sum(1 for c in cards(db) if c["key"] == "planloop:1:S1:promptreview") == 1

    set_config(root, prompt_review="never")
    never = gate.run_gate(paths(root), execute=True, notify_fn=quiet)
    assert never["decision"]["action"] == "create_step", never["decision"]
    assert "planloop:1:S1:step" in keys_of(db)

    # Файл без новых ключей читается на умолчаниях (старые проекты не ломаются).
    set_config(root, prefetch_prompt=None, prompt_review=None)
    defaults = config_mod.load(root)
    assert defaults.prefetch_prompt is True
    assert defaults.prompt_review == "auto"

    set_config(root, prompt_review="sometimes")
    with pytest.raises(config_mod.ConfigError) as exc:
        config_mod.load(root)
    assert "prompt_review" in str(exc.value)

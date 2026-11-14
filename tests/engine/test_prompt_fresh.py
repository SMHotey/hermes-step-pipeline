# -*- coding: utf-8 -*-
"""Протухшая постановка пересобирается, а не останавливает план (план 12, §S1).

Признаки устаревания два: (а) `recon_commit` отчёта ревью не совпадает с отметкой
постановки, (б) отпечаток входа (файлы из «Файл:» шага плюс файл постановки) изменился
после того, как эта редакция ушла ревьюеру. Оба дают карточку пересборки
`:promptfix-stale:{N}` со своим лимитом `max_stale_rebuilds`, не входящим в
`max_fix_rounds`; после пересборки ревью ставится заново и номер круга сдвигается.
Стоп владельцу — только после исчерпания лимита.

Стенд — настоящий git-репозиторий проекта: свежесть постановки считается по коммитам,
отпечаток — по рабочему дереву (несохранённая правка входа тоже изменение).
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import config as config_mod
import gate
from engine_kit import cards, complete, write_approval

REPORT_REL = "docs/reviews/1-S1-prompt.md"
PROMPT_REL = "docs/prompts/2026-11-04_1-S1_prompt.md"

STEP = {"id": "S1", "epoch": "", "title": "Мастер первого запуска", "days": 1.0,
        "owner": "", "goal": "g", "files": ["app/s1.py"], "gates": ["python -m pytest -q"],
        "dod": "d", "read": [], "notes": ""}


def plan_body(count: int = 3, files: str = "`app/s1.py`") -> str:
    """План из `count` шагов с журналом; `files` — пункт «Файл:» первого шага."""
    parts = ["# 1. Демо-план конвейера", "", "Цель: проверить свежесть постановки.", "",
             "## Шаги", ""]
    for i in range(1, count + 1):
        parts += [f"### S{i}. Шаг номер {i} (1 дн)", "",
                  f"- Файл: {files if i == 1 else f'`app/s{i}.py`'}",
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


def write_prompt(root: Path, step: str = "S1", recon: str | None = None) -> Path:
    """Постановка шага; `recon` — отметка `recon_commit:` в шапке."""
    folder = Path(root) / "docs" / "prompts"
    folder.mkdir(parents=True, exist_ok=True)
    head = f"recon_commit: {recon}\n" if recon else ""
    path = folder / f"2026-11-04_1-{step}_prompt.md"
    path.write_text(f"# Постановка {step}\n\n{head}\n## Ревью\n\nроль `reviewer`.\n",
                    encoding="utf-8")
    return path


def write_report(root: Path, verdict: str, recon: str = "1111111") -> Path:
    """Отчёт ревью постановки: отметка версии и одна строка вердикта."""
    folder = Path(root) / "docs" / "reviews"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "1-S1-prompt.md"
    path.write_text(f"recon_commit: {recon}\n\n## ВЕРДИКТ: {verdict}\n\nпроверено\n",
                    encoding="utf-8")
    return path


def git(root: Path, *args: str) -> str:
    res = subprocess.run(["git", *args], cwd=str(root), capture_output=True, text=True,
                         encoding="utf-8", errors="replace")
    assert res.returncode == 0, res.stderr
    return res.stdout.strip()


def commit_all(root: Path, message: str) -> str:
    git(root, "add", "-A")
    git(root, "commit", "-m", message)
    return git(root, "rev-parse", "HEAD")


def init_repo(root: Path) -> str:
    """Настоящий git-репозиторий проекта; возвращает sha первого коммита."""
    git(root, "init", "-b", "main")
    git(root, "config", "user.email", "test@example.com")
    git(root, "config", "user.name", "Test")
    return commit_all(root, "init")


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
    """Пути плана 1 после чтения `pipeline.json` проекта."""
    return gate.resolve_paths(config_mod.load(root), "1")


def quiet(_text: str) -> None:
    """Заглушка уведомлений: тексты проверяются там, где они обязательны."""


def keys_of(db: Path) -> set[str]:
    return {c["key"] for c in cards(db)}


def tick(p, notify=quiet) -> dict:
    """Один тик стража на этих путях."""
    return gate.run_gate(p, execute=True, notify_fn=notify)


@pytest.fixture()
def stand(project: Path, hermes_stub: Path, monkeypatch):
    """Пути плана 1 на временном проекте; сборщик шагов смотрит в PIPELINE_REPO."""
    monkeypatch.setenv("PIPELINE_REPO", str(project))
    return project, paths(project), hermes_stub


@pytest.fixture()
def stale(stand):
    """Доведён до расхождения отметок: ревью постановки закрыто отчётом про чужую версию."""
    root, p, db = stand
    write_plan(root, plan_body(3))
    write_prompt(root, "S1", recon="1111111")
    tick(p)                                  # заказ ревью постановки
    complete(db, ":promptreview")
    write_report(root, "APPROVE", recon="2222222")   # отметка не про эту редакцию
    return root, p, db


def test_recon_mismatch_rebuilds_instead_of_stop(stale):
    """Расхождение отметок даёт карточку пересборки, а не стоп-флаг."""
    root, p, db = stale
    res = tick(p)
    decision = res["decision"]
    assert decision["action"] == "create_prompt_fix", decision
    assert decision["stale"] is True
    assert decision["stale_reason"] == "stale-recon"
    assert decision["rebuild"] == 1
    assert decision["limit"] == 2
    assert "1 из 2" in decision["reason"], decision["reason"]
    row = next(c for c in cards(db) if c["key"] == "planloop:1:S1:promptfix-stale:1")
    assert row["assignee"] == "prompter"
    assert "Пересобрать протухшую постановку" in row["title"]
    assert "planloop:1:S1:step" not in keys_of(db)
    assert gate.load_stop(p) is None


def test_rebuild_in_progress_waits(stale):
    """Пока пересборка в работе — `idle` с «пересобирается», вторая карточка не нужна."""
    root, p, db = stale
    tick(p)
    second = tick(p)
    assert second["decision"]["action"] == "idle", second["decision"]
    assert "пересобирается" in second["decision"]["reason"]
    assert sum(1 for c in cards(db) if c["key"].endswith(":promptfix-stale:1")) == 1


def test_rebuild_shifts_review_round(stale):
    """Закрытая пересборка заказывает новое ревью: номер круга сдвинут ею, а не правкой."""
    root, p, db = stale
    tick(p)
    complete(db, ":promptfix-stale:1")
    again = tick(p)
    assert again["decision"]["action"] == "create_prompt_review", again["decision"]
    assert again["decision"]["round"] == 2
    assert "planloop:1:S1:promptreview:2" in keys_of(db)


def test_after_rebuild_step_starts(stale):
    """Пересобранную постановку ревьюер принял — шаг стартует без ручного снятия стопа."""
    root, p, db = stale
    tick(p)
    write_prompt(root, "S1", recon="3333333")     # промптер пересобрал постановку
    complete(db, ":promptfix-stale:1")
    tick(p)                                        # ревью круга 2
    write_report(root, "APPROVE", recon="3333333")
    complete(db, ":promptreview:2")
    started = tick(p)
    assert started["decision"]["action"] == "create_step", started["decision"]
    assert "planloop:1:S1:step" in keys_of(db)
    assert gate.load_stop(p) is None


def test_rebuild_limit_stops_owner(stale):
    """Третье расхождение при лимите 2 — стоп владельцу с цифрой и именем ключа."""
    root, p, db = stale
    tick(p)                                        # пересборка 1
    complete(db, ":promptfix-stale:1")
    tick(p)                                        # ревью круга 2
    write_report(root, "APPROVE", recon="2222222")
    complete(db, ":promptreview:2")
    tick(p)                                        # пересборка 2
    assert "planloop:1:S1:promptfix-stale:2" in keys_of(db)
    complete(db, ":promptfix-stale:2")
    tick(p)                                        # ревью круга 3
    write_report(root, "APPROVE", recon="2222222")
    complete(db, ":promptreview:3")

    stopped = tick(p)
    assert stopped["decision"]["action"] == "stop", stopped["decision"]
    stop = gate.load_stop(p)
    assert stop is not None and stop["kind"] == "owner", stop
    assert "max_stale_rebuilds" in stop["reason"] and "2" in stop["reason"]
    assert "planloop:1:S1:promptfix-stale:3" not in keys_of(db)
    assert "planloop:1:S1:step" not in keys_of(db)


def test_zero_rebuilds_stops_like_before(stale):
    """`max_stale_rebuilds: 0` — выключатель: первое же расхождение останавливает конвейер."""
    root, p, db = stale
    set_config(root, max_stale_rebuilds=0)
    stopped = tick(paths(root))
    assert stopped["decision"]["action"] == "stop", stopped["decision"]
    assert "max_stale_rebuilds" in stopped["decision"]["reason"]
    assert not any("promptfix-stale" in k for k in keys_of(db))
    assert "planloop:1:S1:step" not in keys_of(db)


def test_input_change_is_second_sign(stand):
    """Правка входа в рабочем дереве при совпавшей отметке — тоже пересборка."""
    root, p, db = stand
    write_plan(root, plan_body(3))
    write_prompt(root, "S1")                      # черновик без отметки
    head = init_repo(root)
    write_prompt(root, "S1", recon=head)          # редакция с отметкой
    (root / "app").mkdir()
    (root / "app" / "s1.py").write_text("x = 1\n", encoding="utf-8")
    commit_all(root, "код после снятия постановки")

    p = paths(root)
    tick(p)                                        # ревью заказано, опора записана
    (root / "app" / "s1.py").write_text("x = 2\n", encoding="utf-8")   # без коммита
    write_report(root, "APPROVE", recon=head)
    complete(db, ":promptreview")

    res = tick(p)
    decision = res["decision"]
    assert decision["action"] == "create_prompt_fix", decision
    assert decision["stale"] is True
    assert decision["stale_reason"] == "stale-input"
    assert "вход изменился" in decision["reason"]
    assert "app/s1.py" in decision["changed"]
    assert "planloop:1:S1:step" not in keys_of(db)


def test_unchanged_input_starts_step(stand):
    """Вход не менялся — второго признака нет, APPROVE по той же версии пускает шаг."""
    root, p, db = stand
    write_plan(root, plan_body(3))
    write_prompt(root, "S1")
    head = init_repo(root)
    write_prompt(root, "S1", recon=head)
    (root / "app").mkdir()
    (root / "app" / "s1.py").write_text("x = 1\n", encoding="utf-8")
    commit_all(root, "код после снятия постановки")

    p = paths(root)
    tick(p)
    write_report(root, "APPROVE", recon=head)
    complete(db, ":promptreview")

    res = tick(p)
    assert res["decision"]["action"] == "create_step", res["decision"]
    assert "planloop:1:S1:step" in keys_of(db)


def test_config_max_stale_rebuilds(stand):
    """Ключ читается как целое ≥ 0; мусор роняет загрузку с именем ключа."""
    root, p, db = stand
    assert config_mod.load(root).max_stale_rebuilds == 2      # ключа нет — умолчание
    set_config(root, max_stale_rebuilds="3")
    assert config_mod.load(root).max_stale_rebuilds == 3
    set_config(root, max_stale_rebuilds=0)
    assert config_mod.load(root).max_stale_rebuilds == 0
    for bad in (-1, True, "мусор"):
        set_config(root, max_stale_rebuilds=bad)
        with pytest.raises(config_mod.ConfigError) as exc:
            config_mod.load(root)
        assert "max_stale_rebuilds" in str(exc.value)


def test_status_shows_rebuild_counter(stand):
    """Счётчик пересборок виден в `--status` и при нуле, и после первой пересборки."""
    root, p, db = stand
    write_plan(root, plan_body(3))
    write_prompt(root, "S1", recon="1111111")
    tick(p)
    complete(db, ":promptreview")
    write_report(root, "APPROVE", recon="2222222")

    fresh = paths(root)
    text = gate.status_text(gate.build_ctx(fresh), {"action": "idle"})
    assert "Пересборок постановки по свежести: 0 из 2" in text, text

    tick(fresh)                                    # пересборка 1
    after = paths(root)
    text = gate.status_text(gate.build_ctx(after), {"action": "idle"})
    assert "Пересборок постановки по свежести: 1 из 2" in text, text


def test_stale_card_body_names_changes(stand):
    """Тело карточки называет прошлый recon, HEAD, отпечаток, изменённый вход и коммиты."""
    root, p, db = stand
    write_plan(root, plan_body(3))
    write_prompt(root, "S1")
    head = init_repo(root)
    (root / "app").mkdir()
    (root / "app" / "s1.py").write_text("x = 1\n", encoding="utf-8")
    later = commit_all(root, "код после снятия постановки")
    decision = {"stale": True, "stale_reason": "stale-input", "rebuild": 1, "limit": 2,
                "fp": "ab" * 32, "prev_recon": head[:7], "review_round": 2,
                "changed": {"app/s1.py": ("a" * 64, "b" * 64)},
                "reason": "постановка S1 протухла: вход изменился после ревью"}
    body = gate.prompt_stale_card_body(paths(root), STEP, decision, REPORT_REL)

    assert f"`{head[:7]}`" in body, "нет прошлого recon_commit"
    assert f"`{later}`" in body, "нет текущего HEAD"
    assert decision["fp"][:12] in body, "нет отпечатка"
    assert "`app/s1.py`: было aaaaaaaa, стало bbbbbbbb" in body, body
    assert "код после снятия постановки" in body, "нет списка коммитов между версиями"
    assert "check_task_prompt.py" in body
    assert "пересборка 1 из 2" in body


def test_baseline_parts_are_per_file(stand):
    """Опора хранит `sha256` по каждому входу, подмена двух файлов местами даёт два пути."""
    root, p, db = stand
    write_plan(root, plan_body(3, files="`app/s1.py`, `app/s2.py`"))
    (root / "app").mkdir()
    (root / "app" / "s1.py").write_text("one\n", encoding="utf-8")
    (root / "app" / "s2.py").write_text("two\n", encoding="utf-8")
    write_prompt(root, "S1", recon="1111111")

    p = paths(root)
    tick(p)                                        # заказ ревью пишет опору
    state = json.loads(Path(p.state_file).read_text(encoding="utf-8"))
    parts = state["prompt_input"]["S1"]["parts"]
    assert set(parts) == {"app/s1.py", "app/s2.py", PROMPT_REL}, parts
    assert all(len(value) == 64 for value in parts.values()), parts

    (root / "app" / "s1.py").write_text("one!\n", encoding="utf-8")
    changed = gate.prompt_input_changed(gate.build_ctx(paths(root)))
    assert list(changed) == ["app/s1.py"], changed
    was, now = changed["app/s1.py"]
    assert was == parts["app/s1.py"] and now != was

    # Подмена двух файлов местами: оба пути, у каждого своя пара — агрегата для этого мало.
    (root / "app" / "s1.py").write_text("two\n", encoding="utf-8")
    (root / "app" / "s2.py").write_text("one\n", encoding="utf-8")
    changed = gate.prompt_input_changed(gate.build_ctx(paths(root)))
    assert set(changed) == {"app/s1.py", "app/s2.py"}, changed
    assert changed["app/s1.py"][1] == parts["app/s2.py"]
    assert changed["app/s2.py"][1] == parts["app/s1.py"]

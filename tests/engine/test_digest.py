# -*- coding: utf-8 -*-
"""Пакет шага (план 9, S5): объявленные файлы целиком, лимит — предохранитель.

Что держат эти проверки:

1. бюджет считается из окна профиля (`checks.context_window_tokens`), а не из константы:
   1 млн → 607 500 символов, умолчание 128k → 77 760, мусор в ключе — умолчание;
2. окно 1 млн — объявленные файлы шага приходят целиком, без пометок об обрезке;
3. окно 128k на файле движка — режим фрагментов и находка F09 с числами;
4. пакет никогда не длиннее бюджета и никогда не обрезан молча;
5. пустой список объявленных файлов не роняет сборку;
6. F01 ловит посторонний файл коммита и пропускает тесты, документацию и журнал изменений;
7. пакет идемпотентен по шагу, а пакет ревьюера равен пакету исполнителя (одна мета);
8. сборка пакета на демо-проекте укладывается в две секунды;
9. новый модуль едет в проекты списком движка (`ENGINE_FILES`) — иначе ImportError в cron.

Фикстуры — по образцу `tests/engine/test_finding_codes.py` и `test_commit.py`: проект из
`engine_kit.make_project`, пути — `gate.resolve_paths`, каталоги — `tmp_path`.
"""
from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest

import config as config_mod
import digest
import gate
import next_step
from engine_kit import make_project

STEP = {"id": "S5", "epoch": "", "title": "Пакет шага", "days": 2.0, "owner": "",
        "goal": "объявленные файлы целиком", "files": [], "gates": ["pytest"],
        "dod": "пакет собран", "read": [], "notes": ""}

REPORT_REL = "docs/reviews/1-S5.md"


# ───────────────────────────── стенды ─────────────────────────────

def stand(tmp_path: Path, files: dict[str, str] | None = None,
          window: int | None = None) -> Path:
    """Проект-приёмник с копией движка; `files` — объявленные файлы шага на диске."""
    overrides: dict = {}
    if window is not None:
        overrides["checks"] = {"context_window_tokens": window}
    root = make_project(tmp_path / "digest", **overrides)
    for rel, text in (files or {}).items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


def paths(root: Path):
    return gate.resolve_paths(config_mod.load(root), "1")


def git(cwd: Path, *args: str) -> str:
    res = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True,
                         encoding="utf-8", errors="replace")
    assert res.returncode == 0, res.stderr
    return res.stdout.strip()


def make_repo(tmp_path: Path) -> Path:
    root = make_project(tmp_path / "repo", git={"commit": True, "push": False})
    git(root, "init", "-b", "main")
    git(root, "config", "user.email", "test@example.com")
    git(root, "config", "user.name", "Test")
    git(root, "add", "-A")
    git(root, "commit", "-m", "init")
    return root


def commit_files(root: Path, files: dict[str, str], message: str) -> str:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    git(root, "add", "-A")
    git(root, "commit", "-m", message)
    return git(root, "rev-parse", "HEAD")


# ───────────────────────────── окно и бюджет ─────────────────────────────

def test_budget_is_computed_from_window(tmp_path):
    cfg = config_mod.load(stand(tmp_path, window=1_000_000))
    assert digest.context_window(cfg) == 1_000_000
    assert digest.budget_chars(1_000_000) == 607_500
    assert digest.budget_chars(digest.CONTEXT_WINDOW_DEFAULT) == 77_760


def test_window_defaults_and_ignores_garbage(tmp_path):
    cfg = config_mod.load(stand(tmp_path))
    assert digest.context_window(cfg) == 128_000
    for bad in ("abc", True, -5, 0):
        cfg._raw["checks"] = {"context_window_tokens": bad}
        assert digest.context_window(cfg) == 128_000, bad
    cfg._raw["checks"] = {"context_window_tokens": "200000"}
    assert digest.context_window(cfg) == 200_000


# ───────────────────────────── объявленные файлы ─────────────────────────────

def test_files_whole_at_million_window(tmp_path):
    files = {"app/models/invoice.py": "class Invoice:\n    pass\n" * 50,
             "app/api/invoices.py": "def list_invoices():\n    return []\n" * 50}
    root = stand(tmp_path, files, window=1_000_000)
    cfg = config_mod.load(root)
    step = {**STEP, "files": list(files)}
    meta = digest.collect(cfg, step, "1")
    assert meta["full"] == list(files)
    assert meta["fragmented"] == [] and meta["omitted"] == []
    assert meta["finding"] == ""
    text = (root / meta["path"]).read_text(encoding="utf-8")
    for body in files.values():
        assert body.rstrip() in text, "объявленный файл обязан прийти целиком"


def test_engine_file_fragments_and_f09_at_128k(tmp_path):
    """На окне 128k движок целиком не влезает: фрагменты и F09 с числами (ответ Q-4 = A)."""
    root = stand(tmp_path)
    cfg = config_mod.load(root)
    rel = ".pipeline/gate.py"
    meta = digest.collect(cfg, {**STEP, "files": [rel]}, "1")
    assert meta["fragmented"] == [rel]
    assert meta["finding"].startswith("F09")
    assert "бюджет" in meta["finding"] and "фрагментами: 1" in meta["finding"]
    text = (root / meta["path"]).read_text(encoding="utf-8")
    assert "обрезано: фрагментами" in text
    assert "ANCHOR:" in text, "фрагменты — окрестности якорей, а не начало файла"


@pytest.mark.parametrize("window", [1_000_000, 128_000, 20_000])
def test_package_never_exceeds_budget(tmp_path, window):
    root = stand(tmp_path, window=window)
    cfg = config_mod.load(root)
    step = {**STEP, "files": [".pipeline/gate.py", ".pipeline/next_step.py",
                              ".pipeline/config.py"]}
    meta = digest.collect(cfg, step, "1")
    text = (root / meta["path"]).read_text(encoding="utf-8")
    assert meta["chars"] == len(text)
    assert len(text) <= meta["budget"], f"пакет длиннее бюджета при окне {window}"


def test_tiny_window_marks_truncation(tmp_path):
    """Места нет даже на одну окрестность — пакет всё равно помечен, а не тихо порезан."""
    root = stand(tmp_path, window=2_000)
    cfg = config_mod.load(root)
    meta = digest.collect(cfg, {**STEP, "files": [".pipeline/gate.py"]}, "1")
    text = (root / meta["path"]).read_text(encoding="utf-8")
    assert len(text) <= meta["budget"]
    assert "обрезано" in text
    assert meta["finding"].startswith("F09")


def test_empty_file_list_does_not_crash(tmp_path):
    root = stand(tmp_path)
    cfg = config_mod.load(root)
    meta = digest.collect(cfg, {**STEP, "files": []}, "1")
    assert meta["declared"] == [] and meta["full"] == []
    assert meta["finding"] == ""
    text = (root / meta["path"]).read_text(encoding="utf-8")
    assert digest.NOTHING_DECLARED in text


def test_declared_dir_is_expanded(tmp_path):
    root = stand(tmp_path, {"app/api/a.py": "a = 1\n", "app/api/b.py": "b = 2\n"},
                 window=1_000_000)
    cfg = config_mod.load(root)
    meta = digest.collect(cfg, {**STEP, "files": ["app/api"]}, "1")
    assert meta["files"] == ["app/api/a.py", "app/api/b.py"]
    assert meta["full"] == meta["files"]


# ───────────────────────────── граница правок (F01) ─────────────────────────────

def test_f01_catches_foreign_file(tmp_path):
    root = make_repo(tmp_path)
    sha = commit_files(root, {"src/other.py": "x = 1\n"}, "чужой файл")
    p = paths(root)
    step = {**STEP, "files": ["app/models/invoice.py"]}
    assert gate.step_file_offenders(p, step, sha) == ["src/other.py"]


def test_f01_allows_tests_docs_and_changelog(tmp_path):
    root = make_repo(tmp_path)
    sha = commit_files(root, {
        "app/models/invoice.py": "class Invoice:\n    pass\n",
        "tests/engine/test_invoice.py": "def test_it():\n    assert True\n",
        "docs/reviews/1-S5.md": "## ВЕРДИКТ: APPROVE\n",
        "CHANGELOG.md": "## 0.45.0\n",
        "step_pipeline/plugin.yaml": "version: 0.45.0\n",
    }, "файлы шага")
    p = paths(root)
    step = {**STEP, "files": ["app/models/invoice.py"]}
    assert gate.step_file_offenders(p, step, sha) == []


# ───────────────────────────── воспроизводимость ─────────────────────────────

def test_package_is_idempotent(tmp_path):
    root = stand(tmp_path, {"app/models/invoice.py": "x = 1\n" * 20}, window=1_000_000)
    cfg = config_mod.load(root)
    step = {**STEP, "files": ["app/models/invoice.py"]}
    first = digest.collect(cfg, step, "1")
    first_text = (root / first["path"]).read_text(encoding="utf-8")
    second = digest.collect(cfg, step, "1")
    second_text = (root / second["path"]).read_text(encoding="utf-8")
    assert first == second
    assert first_text == second_text


def test_reviewer_package_equals_implementer(tmp_path):
    """Ревьюер судит по тому же входу: карточка ревью читает мету, а не пересобирает пакет."""
    rel = "app/models/invoice.py"
    root = stand(tmp_path, {rel: "x = 1\n" * 20}, window=1_000_000)
    p = paths(root)
    step = {**STEP, "files": [rel]}
    step_body = gate.step_card_body(p, step)
    meta = digest.load_meta(p.cfg, "1", "S5")
    assert meta is not None, "карточка шага обязана записать мету пакета"
    # дерево выросло после заказа карточки: пересборка показала бы другие числа
    (root / rel).write_text("y = 2\n" * 5000, encoding="utf-8")
    review_body = gate.review_card_body(p, step, 1, REPORT_REL, None)
    block = digest.card_lines(meta)
    assert digest.PACKAGE_HEADER in step_body
    assert digest.PACKAGE_HEADER in review_body
    for line in block:
        assert line in step_body, f"в карточке шага нет строки пакета: {line}"
        assert line in review_body, f"в карточке ревью нет строки пакета: {line}"
    assert "тот же вход" in review_body
    assert digest.load_meta(p.cfg, "1", "S5") == meta, "мета не перезаписывается ревью"


def test_json_mode_is_read_only(tmp_path, monkeypatch):
    """Машинный режим (его читает панель и предпросмотр) в проект не пишет."""
    root = stand(tmp_path, {"app/models/invoice.py": "x = 1\n"}, window=1_000_000)
    state = tmp_path / "state"
    monkeypatch.setenv("PIPELINE_REPO", str(root))
    monkeypatch.setenv("PIPELINE_STATE_DIR", str(state))
    assert next_step.main(["--plan", "1", "--json"]) == 0
    assert not (state / "digest").exists(), "режим чтения не создаёт каталог пакета"


def test_plain_run_writes_package(tmp_path, monkeypatch):
    """Человеческий запуск кладёт пакет в каталог состояния — это вход исполнителя."""
    root = stand(tmp_path, {"app/models/invoice.py": "x = 1\n"}, window=1_000_000)
    state = tmp_path / "state"
    monkeypatch.setenv("PIPELINE_REPO", str(root))
    monkeypatch.setenv("PIPELINE_STATE_DIR", str(state))
    assert next_step.main(["--plan", "1", "--brief"]) == 0
    assert (state / "digest" / "plan-1-S1.md").is_file()


def test_fix_card_carries_package(tmp_path):
    """Круг правок идёт по тому же входу: карточка правок несёт блок пакета."""
    root = stand(tmp_path, {"app/models/invoice.py": "x = 1\n"}, window=1_000_000)
    p = paths(root)
    step = {**STEP, "files": ["app/models/invoice.py"]}
    gate.step_card_body(p, step)
    (root / REPORT_REL).write_text("## ВЕРДИКТ: REQUEST_CHANGES\n", encoding="utf-8")
    body = gate.fix_card_body(p, step, 1, REPORT_REL)
    assert digest.PACKAGE_HEADER in body
    assert "тот же вход" in body


def test_collection_is_fast(tmp_path):
    root = stand(tmp_path)
    cfg = config_mod.load(root)
    step = {**STEP, "files": [".pipeline/gate.py", ".pipeline/next_step.py"]}
    started = time.perf_counter()
    digest.collect(cfg, step, "1")
    assert time.perf_counter() - started < 2.0


def test_digest_ships_with_engine():
    """Новый модуль обязан ехать в проекты списком движка (иначе ImportError в cron)."""
    assert "digest.py" in config_mod.ENGINE_FILES
    assert digest.__file__.endswith("digest.py")

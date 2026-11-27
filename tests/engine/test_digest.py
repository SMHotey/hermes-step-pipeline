# -*- coding: utf-8 -*-
"""Пакет шага (план 9, S5): объявленные файлы целиком, лимит — предохранитель.

Что держат эти проверки:

1. бюджет считается из окна профиля (`checks.context_window_tokens`), а не из константы:
   1 млн → 607 500 символов, умолчание 128k → 77 760, мусор в ключе — умолчание и честный
   источник в шапке пакета;
2. окно 1 млн — объявленные файлы шага приходят целиком, без пометок об обрезке;
3. окно 128k на трёх объявленных файлах — режим «фрагменты», пометки «обрезано: строки a–b
   из N» и «якорей нет», находка F09 с числами «2 из 3»;
4. пакет никогда не длиннее бюджета: предохранитель, а не украшение;
5. пустой список объявленных файлов не роняет сборку;
6. F01 в пакете: до коммита — «коммита шага нет», с коммитом — посторонний файл назван,
   тесты, документация и журнал изменений не считаются посторонними;
7. пакет идемпотентен по шагу, а пакет ревьюера — тот же файл, что у исполнителя;
8. сборка укладывается в две секунды; `--package` пишет файл и печатает путь и числа.

Фикстуры — по образцу `tests/engine/test_prompt_findings.py`: проект из
`engine_kit.make_project`, пути — `gate.resolve_paths`, каталоги — `tmp_path`.
"""
from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest

import config as config_mod
import gate
import next_step
from engine_kit import make_project

STEP = {"id": "S5", "epoch": "", "title": "Пакет шага", "days": 2.0, "owner": "",
        "goal": "объявленные файлы целиком", "files": [], "gates": ["pytest"],
        "dod": "пакет собран", "read": [], "notes": ""}

REPORT_REL = "docs/reviews/1-S5.md"

#: Три объявленных файла стенда: 400 строк с двумя якорями, 400 строк с одним якорем на
#: строке 200 и 300 строк без якорей — ровно то, на чём разница «целиком» и «фрагментами» видна.
BIG_A = "big_a.py"
BIG_B = "big_b.py"
PLAIN_C = "plain_c.py"
DECLARED = [BIG_A, BIG_B, PLAIN_C]


def _line(tag: str) -> str:
    """Строка ровно в 99 символов: файл в 400 строк — 40 000 символов, как в постановке."""
    return tag.ljust(99, "·")


def _text(lines: list[str]) -> str:
    return "\n".join(lines) + "\n"


def demo_files() -> dict[str, str]:
    """Тела объявленных файлов стенда: два якоря, один якорь, ни одного."""
    big_a = [_line(f"a{i:03d}") for i in range(1, 401)]
    big_a[49] = "# ANCHOR:начало " + _line("a050")
    big_a[299] = "# ANCHOR:конец " + _line("a300")
    big_b = [_line(f"b{i:03d}") for i in range(1, 401)]
    big_b[199] = "# ANCHOR:середина " + _line("b200")
    plain_c = [_line(f"c{i:03d}") for i in range(1, 301)]
    return {BIG_A: _text(big_a), BIG_B: _text(big_b), PLAIN_C: _text(plain_c)}


def stand(tmp_path: Path, window: int | None = None, files: dict[str, str] | None = None,
          repo: bool = False) -> Path:
    """Проект-приёмник с копией движка и объявленными файлами шага на диске."""
    overrides: dict = {"git": {"commit": repo, "push": False}}
    if window is not None:
        overrides["checks"] = {"context_window_tokens": window}
    root = make_project(tmp_path / "proj", **overrides)
    for rel, text in (files if files is not None else demo_files()).items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    if repo:
        git(root, "init", "-b", "main")
        git(root, "config", "user.email", "test@example.com")
        git(root, "config", "user.name", "Test")
        git(root, "add", "-A")
        git(root, "commit", "-m", "init")
    return root


def paths(root: Path):
    return gate.resolve_paths(config_mod.load(root), "1")


def git(cwd: Path, *args: str) -> str:
    res = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True,
                         encoding="utf-8", errors="replace")
    assert res.returncode == 0, res.stderr
    return res.stdout.strip()


def commit(root: Path, files: dict[str, str], message: str) -> str:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    git(root, "add", "-A")
    git(root, "commit", "-m", message)
    return git(root, "rev-parse", "HEAD")


def write_runs_proof(root: Path, step_id: str, sha: str) -> None:
    """Доказательство прогонов шага на текущем sha — без него F01 молчит «коммита нет»."""
    p = paths(root)
    path = gate.runs_proof_path(p, step_id, sha)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"sha": "%s", "runs": []}' % sha, encoding="utf-8")


def build(root: Path, files: list[str] | None = None) -> dict:
    cfg = config_mod.load(root)
    return next_step.build_digest(cfg, {**STEP, "files": DECLARED if files is None else files}, "1")


# ───────────────────────────── окно и бюджет ─────────────────────────────

def test_budget_is_computed_from_window(tmp_path):
    cfg = config_mod.load(stand(tmp_path, window=1_000_000))
    window, source = next_step.window_tokens(cfg)
    assert window == 1_000_000 and "context_window_tokens" in source
    assert next_step.digest_budget(window) == 607_500
    assert next_step.digest_budget(next_step.DIGEST_WINDOW_DEFAULT) == 77_760


def test_window_reading_is_tolerant(tmp_path):
    """Опечатка владельца не роняет конвейер, но и не проходит молча: источник виден в шапке."""
    cfg = config_mod.load(stand(tmp_path))
    assert next_step.window_tokens(cfg) == (128_000, "по умолчанию: в конфиге нет "
                                                        "`checks.context_window_tokens`")
    for bad in ("abc", True, -5, 0, 1.5):
        cfg._raw["checks"] = {"context_window_tokens": bad}
        window, source = next_step.window_tokens(cfg)
        assert window == next_step.DIGEST_WINDOW_DEFAULT, bad
        assert source.startswith("по умолчанию"), bad
    cfg._raw["checks"] = {"context_window_tokens": "200000"}
    assert next_step.window_tokens(cfg)[0] == 200_000


# ───────────────────────────── объявленные файлы ─────────────────────────────

def test_window_1m_keeps_declared_files_whole(tmp_path):
    root = stand(tmp_path, window=1_000_000)
    pkg = build(root)
    text = Path(pkg["path"]).read_text(encoding="utf-8")
    assert pkg["whole"] == DECLARED and pkg["fragments"] == [] and pkg["missing"] == []
    assert pkg["f09"] == "", "на окне 1 млн все объявленные файлы влезают целиком"
    assert "обрезано" not in text, "тихая или явная обрезка на 1 млн не нужна"
    for body in demo_files().values():
        assert body.rstrip() in text, "объявленный файл обязан прийти целиком"


def test_window_128k_fragments_and_f09(tmp_path):
    """На окне 128k влезает только первый файл: фрагменты и F09 с числами «2 из 3»."""
    root = stand(tmp_path, window=128_000)
    pkg = build(root)
    assert pkg["whole"] == [BIG_A]
    assert pkg["fragments"] == [BIG_B, PLAIN_C]
    assert pkg["f09"].startswith("F09"), "перебор обязан дать находку F09"
    assert "2 из 3" in pkg["f09"], "в находке — числа «файлов фрагментами M из K»"
    assert f"бюджете {next_step.digest_budget(128_000)}" in pkg["f09"].replace("\u202f", " ") \
        or "бюджете" in pkg["f09"]


def test_cut_marks_present(tmp_path):
    """Обрезка помечена: строки 120–280 из 400 у якорного файла и «якорей нет» у пустого."""
    root = stand(tmp_path, window=128_000)
    text = Path(build(root)["path"]).read_text(encoding="utf-8")
    assert "обрезано: строки 120–280 из 400" in text, "окрестность якоря — ±80 строк"
    assert "якорей нет" in text, "файл без якорей приходит началом файла и говорит об этом"


@pytest.mark.parametrize("window", [1_000_000, 128_000, 20_000])
def test_package_size_within_budget(tmp_path, window):
    root = stand(tmp_path, window=window)
    pkg = build(root)
    text = Path(pkg["path"]).read_text(encoding="utf-8")
    assert pkg["chars"] == len(text)
    assert len(text) <= pkg["budget"], f"пакет длиннее бюджета при окне {window}"
    raw = Path(pkg["path"]).read_bytes().decode("utf-8")
    assert raw == text, "запись без перевода строк: объём в шапке обязан совпасть с файлом"
    assert f"Объём пакета: {len(text)} символов" in text


def test_empty_file_list_does_not_crash(tmp_path):
    root = stand(tmp_path, window=1_000_000, files={})
    pkg = build(root, files=[])
    text = Path(pkg["path"]).read_text(encoding="utf-8")
    assert pkg["whole"] == [] and pkg["fragments"] == [] and pkg["missing"] == []
    assert pkg["f09"] == ""
    assert "объявленных файлов нет" in text


# ───────────────────────────── граница правок (F01) ─────────────────────────────

def test_f01_catches_foreign_file(tmp_path):
    root = stand(tmp_path, window=1_000_000, repo=True)
    sha = commit(root, {BIG_A: demo_files()[BIG_A], "src/other.py": "x = 1\n"}, "шаг с чужим")
    write_runs_proof(root, "S5", sha)
    pkg = build(root, [BIG_A])
    assert pkg["f01"].startswith("F01 — файлов вне объявленных: src/other.py")


def test_f01_allows_tests_docs_and_changelog(tmp_path):
    root = stand(tmp_path, window=1_000_000, repo=True)
    sha = commit(root, {
        BIG_A: demo_files()[BIG_A],
        "tests/engine/test_big_a.py": "def test_it():\n    assert True\n",
        "docs/reviews/1-S5.md": "## ВЕРДИКТ: APPROVE\n",
        "CHANGELOG.md": "## 0.45.0\n",
    }, "файлы шага")
    write_runs_proof(root, "S5", sha)
    pkg = build(root, [BIG_A])
    assert pkg["f01"].startswith("F01 — файлов вне объявленных нет"), pkg["f01"]


def test_f01_says_no_commit_before_step_commit(tmp_path):
    """До коммита шага F01 не пугает: строка говорит «коммита шага нет»."""
    root = stand(tmp_path, window=1_000_000, repo=True)
    pkg = build(root, [BIG_A])
    assert pkg["f01"] == "F01 — коммита шага нет: проверка в карточке ревью"


# ───────────────────────────── воспроизводимость ─────────────────────────────

def test_package_is_idempotent(tmp_path):
    root = stand(tmp_path, window=128_000)
    first = build(root)
    first_text = Path(first["path"]).read_text(encoding="utf-8")
    second = build(root)
    assert first == second, "пакет — функция дерева: два прогона дают одни числа"
    assert first_text == Path(second["path"]).read_text(encoding="utf-8")
    assert len(list(Path(first["path"]).parent.glob("package-plan-1-S5*"))) == 1


def test_reviewer_package_equals_executor_package(tmp_path):
    """Ревьюер судит по тому же файлу: карточка ревью не пересобирает пакет под выросшее дерево."""
    root = stand(tmp_path, window=1_000_000, repo=True)
    p = paths(root)
    cfg = config_mod.load(root)
    step = {**STEP, "files": [BIG_A]}
    pkg = next_step.build_digest(cfg, step, "1")
    step_body = gate.step_card_body(p, step, None, package=pkg)
    before = Path(pkg["path"]).read_bytes()
    (root / BIG_A).write_text(_text([_line(f"z{i:03d}") for i in range(1, 2001)]),
                              encoding="utf-8")
    (root / REPORT_REL).write_text("## ВЕРДИКТ: REQUEST_CHANGES\n", encoding="utf-8")
    review_body = gate.review_card_body(p, step, 1, REPORT_REL, None)
    fix_body = gate.fix_card_body(p, step, 1, REPORT_REL)
    assert Path(pkg["path"]).read_bytes() == before, "пакет ревью не перезаписывается"
    for body in (step_body, review_body, fix_body):
        assert pkg["path"] in body, "карточка обязана назвать файл пакета"
        assert "файлов целиком" in body.lower()
    assert "ПАКЕТ ШАГА" in step_body and "ПАКЕТ ШАГА (тот же вход)" in review_body


def test_collection_time_under_two_seconds(tmp_path):
    root = stand(tmp_path, window=1_000_000)
    started = time.perf_counter()
    build(root)
    assert time.perf_counter() - started < 2.0


# ───────────────────────────── ручной запуск ─────────────────────────────

def test_cli_package_writes_and_prints(tmp_path, monkeypatch, capsys):
    """`--package` пишет файл и печатает одной строкой путь, числа, бюджет и объём."""
    root = stand(tmp_path, window=1_000_000)
    state = tmp_path / "state"
    monkeypatch.setenv("PIPELINE_REPO", str(root))
    monkeypatch.setenv("PIPELINE_STATE_DIR", str(state))
    assert next_step.main(["--plan", "1", "--step", "S1", "--package"]) == 0
    out = capsys.readouterr().out
    assert "пакет шага:" in out and "файлов целиком:" in out and "бюджет" in out
    written = list((state).glob("package-plan-1-S1.md"))
    assert written, "пакет шага обязан лежать в каталоге состояния"
    assert str(written[0]) in out


def test_package_builder_lives_in_next_step():
    """Сборщик — в `next_step.py`: новый модуль движка не поехал бы в проекты без правки списка."""
    source = Path(next_step.__file__).read_text(encoding="utf-8")
    assert "# ANCHOR:дайджест" in source
    for name in ("def window_tokens", "def digest_budget", "def build_digest"):
        assert name in source

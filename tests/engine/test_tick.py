# -*- coding: utf-8 -*-
"""Тик для cron: разбор .env профиля и поиск gate.py.

Причина тестов: cron шлюза отдаёт HERMES_HOME профиля, но не переменные из его .env,
поэтому без собственного разбора уведомления движка молчат (см. 1-S17).
"""
from __future__ import annotations

import importlib.util
import os
import shutil
import sys
from pathlib import Path

import pytest

from engine_kit import ENGINE

TICK = ENGINE / "tick.py"


def load_tick(path: Path):
    """Импортировать копию тика из указанного пути (как отдельный модуль)."""
    spec = importlib.util.spec_from_file_location(f"tick_{abs(hash(str(path)))}", str(path))
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def profile_script(tmp_path: Path) -> Path:
    """Копия тика в структуре `profiles/<профиль>/scripts/pipeline_tick.py`."""
    scripts = tmp_path / "profiles" / "demo" / "scripts"
    scripts.mkdir(parents=True)
    target = scripts / "pipeline_tick.py"
    shutil.copy2(TICK, target)
    return target


def test_env_from_profile_is_loaded(profile_script: Path, monkeypatch):
    (profile_script.parents[1] / ".env").write_text(
        "# комментарий\nSTEP_TEST_TOKEN=abc\n\nexport STEP_TEST_CHAT=\"42\"\n", encoding="utf-8")
    monkeypatch.delenv("STEP_TEST_TOKEN", raising=False)
    monkeypatch.delenv("STEP_TEST_CHAT", raising=False)

    load_tick(profile_script).load_profile_env()

    assert os.environ["STEP_TEST_TOKEN"] == "abc"
    assert os.environ["STEP_TEST_CHAT"] == "42"


def test_existing_values_are_not_overwritten(profile_script: Path, monkeypatch):
    (profile_script.parents[1] / ".env").write_text("STEP_TEST_TOKEN=from_file\n", encoding="utf-8")
    monkeypatch.setenv("STEP_TEST_TOKEN", "from_env")

    load_tick(profile_script).load_profile_env()

    assert os.environ["STEP_TEST_TOKEN"] == "from_env"


def test_pipeline_env_overrides_profile_file(profile_script: Path, tmp_path: Path, monkeypatch):
    (profile_script.parents[1] / ".env").write_text("STEP_TEST_TOKEN=profile\n", encoding="utf-8")
    custom = tmp_path / "custom.env"
    custom.write_text("STEP_TEST_TOKEN=custom\n", encoding="utf-8")
    monkeypatch.delenv("STEP_TEST_TOKEN", raising=False)
    monkeypatch.setenv("PIPELINE_ENV", str(custom))

    load_tick(profile_script).load_profile_env()

    assert os.environ["STEP_TEST_TOKEN"] == "custom"


def test_missing_env_is_not_an_error(profile_script: Path, monkeypatch):
    monkeypatch.delenv("STEP_TEST_TOKEN", raising=False)
    load_tick(profile_script).load_profile_env()  # файла нет — просто ничего не делаем
    assert "STEP_TEST_TOKEN" not in os.environ


def test_find_gate_prefers_pipeline_repo(tmp_path: Path, monkeypatch):
    repo = tmp_path / "repo"
    (repo / ".pipeline").mkdir(parents=True)
    gate_file = repo / ".pipeline" / "gate.py"
    gate_file.write_text("# заглушка", encoding="utf-8")
    monkeypatch.setenv("PIPELINE_REPO", str(repo))

    assert load_tick(TICK).find_gate() == gate_file


def test_find_gate_uses_cwd(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("PIPELINE_REPO", raising=False)
    (tmp_path / ".pipeline").mkdir()
    gate_file = tmp_path / ".pipeline" / "gate.py"
    gate_file.write_text("# заглушка", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    assert load_tick(TICK).find_gate() == gate_file


def test_main_without_gate_reports_error(tmp_path: Path, monkeypatch, capsys):
    """Копия тика в пустом каталоге: gate.py нет ни рядом, ни выше — внятная ошибка."""
    lonely = tmp_path / "bin" / "pipeline_tick.py"
    lonely.parent.mkdir()
    shutil.copy2(TICK, lonely)
    monkeypatch.delenv("PIPELINE_REPO", raising=False)
    monkeypatch.chdir(tmp_path)

    module = load_tick(lonely)
    module.load_profile_env = lambda: None  # профиля нет, .env не нужен
    code = module.main()

    assert code == 1
    assert "gate.py не найден" in capsys.readouterr().err


def test_find_gate_in_installed_project_layouts(tmp_path: Path, monkeypatch):
    """Кандидаты 3 и 4 `find_gate`: `gate.py` рядом с тиком и над каталогом тика.

    `__file__` подменяется, а не делается копия тика: копия вне `.pipeline/` не попадает
    под шаблоны `.coveragerc`, и тест не двигал бы покрытие `tick.py` (см. 1-S12 §2.3).
    """
    module = load_tick(TICK)
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.delenv("PIPELINE_REPO", raising=False)
    monkeypatch.chdir(empty)          # чтобы текущий каталог ничего не подсказал

    # 1. Тик лежит в подкаталоге репо: proj/scripts/pipeline_tick.py + proj/.pipeline/gate.py
    proj = tmp_path / "proj"
    (proj / ".pipeline").mkdir(parents=True)
    (proj / "scripts").mkdir()
    script = proj / "scripts" / "pipeline_tick.py"
    script.write_text("", encoding="utf-8")
    above = proj / ".pipeline" / "gate.py"
    above.write_text("# заглушка", encoding="utf-8")
    monkeypatch.setattr(module, "__file__", str(script))
    assert Path(module.find_gate()).resolve() == above.resolve()

    # 2. Тик лежит рядом со стражем: bin/pipeline_tick.py + bin/gate.py
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    beside = bin_dir / "gate.py"
    beside.write_text("# заглушка", encoding="utf-8")
    side_script = bin_dir / "pipeline_tick.py"
    side_script.write_text("", encoding="utf-8")
    monkeypatch.setattr(module, "__file__", str(side_script))
    assert Path(module.find_gate()).resolve() == beside.resolve()

    # 3. Приоритет: PIPELINE_REPO сильнее соседа файла (tick.py:66-68)
    repo_a = tmp_path / "repo_a"
    (repo_a / ".pipeline").mkdir(parents=True)
    gate_a = repo_a / ".pipeline" / "gate.py"
    gate_a.write_text("# проект A", encoding="utf-8")
    repo_b = tmp_path / "repo_b"
    (repo_b / "bin").mkdir(parents=True)
    (repo_b / "bin" / "gate.py").write_text("# проект B", encoding="utf-8")
    other_script = repo_b / "bin" / "pipeline_tick.py"
    other_script.write_text("", encoding="utf-8")
    monkeypatch.setenv("PIPELINE_REPO", str(repo_a))
    monkeypatch.setattr(module, "__file__", str(other_script))
    assert Path(module.find_gate()).resolve() == gate_a.resolve()


def test_main_runs_gate_with_run_flag(tmp_path: Path, monkeypatch, capsys):
    """Успешный путь `main()`: страж найден и запущен через runpy с `--run`."""
    proj = tmp_path / "proj"
    (proj / ".pipeline").mkdir(parents=True)
    argv_file = tmp_path / "argv.txt"
    stub = (
        "import sys\n"
        "from pathlib import Path\n"
        f"Path({str(argv_file)!r}).write_text('\\n'.join(sys.argv[1:]), encoding='utf-8')\n"
        "print('страж заглушки: тик дошёл')\n"
    )
    (proj / ".pipeline" / "gate.py").write_text(stub, encoding="utf-8")
    monkeypatch.setenv("PIPELINE_REPO", str(proj))
    # main() перезаписывает sys.argv целиком (tick.py:86): без подмены правка утечёт
    # в соседние тесты и сломает им разбор аргументов.
    monkeypatch.setattr(sys, "argv", list(sys.argv))

    code = load_tick(TICK).main()

    out = capsys.readouterr().out
    assert code == 0
    assert argv_file.read_text(encoding="utf-8") == "--run"
    assert "тик дошёл" in out


def test_engine_tick_parses_env_file(tmp_path: Path, monkeypatch):
    """Разбор `.env` на самом файле движка: export, пустые значения, отсутствие перетирания."""
    env_file = tmp_path / "custom.env"
    env_file.write_text(
        "# комментарий\n"
        "\n"
        "строка без равно\n"
        'export STEP_TEST_EXPORT="q"\n'
        "STEP_TEST_PLAIN=plain\n"
        "STEP_TEST_EMPTY=\n"
        "STEP_TEST_KEEP=from_file\n",
        encoding="utf-8")
    for name in ("STEP_TEST_EXPORT", "STEP_TEST_PLAIN", "STEP_TEST_EMPTY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("STEP_TEST_KEEP", "from_env")
    monkeypatch.setenv("PIPELINE_ENV", str(env_file))

    module = load_tick(TICK)
    module.load_profile_env()

    assert os.environ["STEP_TEST_EXPORT"] == "q"
    assert os.environ["STEP_TEST_PLAIN"] == "plain"
    assert "STEP_TEST_EMPTY" not in os.environ      # пустое значение не ставится
    assert os.environ["STEP_TEST_KEEP"] == "from_env"  # setdefault не перетирает

    # PIPELINE_ENV на каталог: не файл — молча пропускаем, ничего не ставим и не падаем.
    monkeypatch.setenv("PIPELINE_ENV", str(tmp_path))
    monkeypatch.delenv("STEP_TEST_PLAIN", raising=False)
    module.load_profile_env()
    assert "STEP_TEST_PLAIN" not in os.environ


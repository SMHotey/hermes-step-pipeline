# -*- coding: utf-8 -*-
"""Тик для cron: разбор .env профиля и поиск gate.py.

Причина тестов: cron шлюза отдаёт HERMES_HOME профиля, но не переменные из его .env,
поэтому без собственного разбора уведомления движка молчат (см. 1-S17). Вторая половина
файла — про установленный мастером скрипт: он обязан быть файлом движка, а не копией.
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from engine_kit import ENGINE

TICK = ENGINE / "tick.py"

#: Корень репозитория: плагин импортируется как пакет (как в test_engine_integrity.py:22-24).
ROOT = ENGINE.parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from step_pipeline import setup_flow  # noqa: E402


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


# ─── Установленный мастером скрипт: источник, окружение джобы, обновление (1-S17) ───

#: Переменные, которых у cron-джобы нет: маркеры профиля и переменные конвейера.
_PROBE_DROP = ("STEP_PIPELINE_TELEGRAM_BOT_TOKEN", "STEP_PIPELINE_TELEGRAM_CHAT_ID",
               "PIPELINE_REPO", "PIPELINE_ENV", "STEP_TEST_TOKEN", "STEP_TEST_CHAT")
JOB_NAME = "План: страж шага (конвейер)"


class _Result:
    """Ответ поддельного `hermes`: только код возврата и stdout."""

    def __init__(self, stdout: str = "") -> None:
        self.returncode = 0
        self.stdout = stdout
        self.stderr = ""


def _stub_hermes(monkeypatch, *, stdout: str = "") -> list[tuple]:
    """Подменить живой hermes: собрать вызовы, ничего не запускать."""
    calls: list[tuple] = []

    def run(*argv, timeout=180):
        calls.append(argv)
        return _Result(stdout)

    monkeypatch.setattr(setup_flow.proj, "run_hermes", run)
    return calls


def _fake_profiles(monkeypatch, tmp_path: Path) -> Path:
    """Каталог профилей в tmp: тест не должен писать в живой профиль."""
    home = tmp_path / "home"
    monkeypatch.setattr(setup_flow.proj, "profile_dir", lambda p: home / "profiles" / p)
    return home


def _cron_env(**extra: str) -> dict[str, str]:
    """Окружение как у cron-джобы: без переменных конвейера и маркеров профиля."""
    env = {key: value for key, value in os.environ.items() if key not in _PROBE_DROP}
    env.update(extra)
    return env


def test_installed_tick_is_engine_file(tmp_path: Path, monkeypatch):
    """`ensure_cron` пишет ровно файл движка — байтами и текстом (D1, D2)."""
    home = _fake_profiles(monkeypatch, tmp_path)
    _stub_hermes(monkeypatch)
    project = tmp_path / "shop"
    project.mkdir()

    setup_flow.ensure_cron("shop", project, setup_flow.Log(), skip=False)

    installed = home / "profiles" / "shop" / "scripts" / "pipeline_tick.py"
    assert installed.is_file()
    assert installed.read_bytes() == TICK.read_bytes(), "установленный скрипт ≠ файл движка"
    assert installed.read_text(encoding="utf-8") == setup_flow.TICK_SCRIPT


def test_installed_tick_loads_profile_env_in_subprocess(tmp_path: Path):
    """Установленный текст в субпроцессе: `--run` дошёл, `.env` профиля подхвачен."""
    profile = tmp_path / "profiles" / "demo"
    (profile / "scripts").mkdir(parents=True)
    installed = profile / "scripts" / "pipeline_tick.py"
    installed.write_bytes(setup_flow.TICK_SOURCE.read_bytes())
    (profile / ".env").write_text("STEP_TEST_TOKEN=из-профиля\nSTEP_TEST_CHAT=77\n",
                                  encoding="utf-8")
    trace = tmp_path / "trace.txt"
    project = tmp_path / "proj"
    (project / ".pipeline").mkdir(parents=True)
    (project / ".pipeline" / "gate.py").write_text(
        "import os, sys\n"
        "from pathlib import Path\n"
        f"trace = Path({str(trace)!r})\n"
        "trace.write_text(' '.join(sys.argv[1:]) + '|' + os.environ.get('STEP_TEST_TOKEN', '')\n"
        "                 + '|' + os.environ.get('STEP_TEST_CHAT', ''), encoding='utf-8')\n",
        encoding="utf-8")

    done = subprocess.run([sys.executable, str(installed)], cwd=str(project),
                          env=_cron_env(HERMES_HOME=str(tmp_path / "hermes")),
                          capture_output=True, text=True, encoding="utf-8", timeout=120)

    assert done.returncode == 0, done.stderr
    assert trace.read_text(encoding="utf-8") == "--run|из-профиля|77"


def test_installed_tick_reports_missing_gate(tmp_path: Path):
    """Нет `gate.py` — установленный скрипт называет причину и возвращает 1, а не 0 молча."""
    lonely = tmp_path / "probe" / "scripts" / "pipeline_tick.py"
    lonely.parent.mkdir(parents=True)
    lonely.write_bytes(setup_flow.TICK_SOURCE.read_bytes())
    empty = tmp_path / "empty"
    empty.mkdir()

    done = subprocess.run([sys.executable, str(lonely)], cwd=str(empty), env=_cron_env(),
                          capture_output=True, text=True, encoding="utf-8", timeout=120)

    assert done.returncode == 1, "старая копия возвращала 0: под cron это «ok» на мёртвом конвейере"
    assert "gate.py не найден" in done.stderr


def test_existing_job_refreshes_installed_script(tmp_path: Path, monkeypatch):
    """Джоба уже есть — скрипт всё равно обновляется, вторая джоба не создаётся (D3)."""
    home = _fake_profiles(monkeypatch, tmp_path)
    calls = _stub_hermes(monkeypatch, stdout=f"{JOB_NAME}   */10 * * * *   local")
    scripts = home / "profiles" / "shop" / "scripts"
    scripts.mkdir(parents=True)
    installed = scripts / "pipeline_tick.py"
    installed.write_text("# старая рукописная копия\n", encoding="utf-8")
    project = tmp_path / "shop"
    project.mkdir()
    log = setup_flow.Log()

    setup_flow.ensure_cron("shop", project, log, skip=False)

    assert installed.read_bytes() == TICK.read_bytes()
    assert not [call for call in calls if "create" in call], calls
    assert any("тик-скрипт обновлён" in line for line in log.lines), log.lines


def test_upgrade_refreshes_tick_script_when_cron_marker_exists(tmp_path: Path, monkeypatch):
    """`upgrade` обновляет скрипт только у проекта с меткой cron, иначе не создаёт (D4)."""
    home = _fake_profiles(monkeypatch, tmp_path)
    _stub_hermes(monkeypatch)
    project = tmp_path / "proj"
    (project / ".pipeline").mkdir(parents=True)
    (project / "pipeline.json").write_text(json.dumps({"profiles": {"driver": "demo"}}),
                                           encoding="utf-8")
    (project / ".pipeline" / "cron.json").write_text(json.dumps({"driver": "demo"}),
                                                     encoding="utf-8")
    log = setup_flow.Log()

    assert setup_flow.upgrade(project, log, trust=False) == 0
    installed = home / "profiles" / "demo" / "scripts" / "pipeline_tick.py"
    assert installed.read_bytes() == TICK.read_bytes()

    plain = tmp_path / "plain"
    (plain / ".pipeline").mkdir(parents=True)
    (plain / "pipeline.json").write_text(json.dumps({"profiles": {"driver": "demo"}}),
                                         encoding="utf-8")
    installed.unlink()

    assert setup_flow.upgrade(plain, log, trust=False) == 0
    assert not installed.exists(), "скрипт создан там, где cron не заводили"
    assert any("не обновлялся" in line for line in log.lines), log.lines


# -*- coding: utf-8 -*-
"""Отладочный вывод подавленных исключений (S5 0.23.0).

Движок глотает часть исключений намеренно: необязательный шаг (cwd сессии, сводка
для чата, перепроверка обновления в панели) не должен ронять тик. Но молчать совсем
нельзя — «почему не сработало» иначе не выяснить. Отсюда флаг `STEP_PIPELINE_DEBUG`:
по умолчанию тишина (stdout движка читают cron-лог и панель), с флагом — строка в stderr.

Плагин не импортирует движок (решение docs/CONTEXT.md:49), поэтому предикат живёт в
двух местах — `config.debug_enabled` и `project.debug_note`. Расхождение между ними
означало бы, что одна половина конвейера рассказывает, а другая молчит; тест держит
их вместе.
"""
from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

import pytest

import helpers

TRUE_VALUES = ("1", "true", "TRUE", "yes", "да")
FALSE_VALUES = ("", "0", "false", "no", "нет", "  ")
WIDE_HANDLERS = (None, "Exception", "BaseException")


@pytest.fixture(scope="module")
def engine_config():
    """`engine/config.py` под своим именем: не подменяем sys.modules['config']."""
    import sys

    path = helpers.ENGINE / "config.py"
    spec = importlib.util.spec_from_file_location("engine_config_probe", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # dataclass в config.py разрешает аннотации через sys.modules[cls.__module__]:
    # без регистрации модуля exec падает с AttributeError.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def silent_handlers(path: Path) -> list[str]:
    """Обработчики, которые глотают исключение и ничего не сообщают.

    Разбор по AST, а не по тексту: `except OSError: pass` в `setup_stdio` — норма
    (поток подменён), а широкий `except Exception: pass` — молчание, из-за которого
    инцидент потом не разобрать. Поэтому смотрим только широкие обработчики и
    только те, чьё тело сводится к `pass`/`continue` без единого сообщения.
    """
    found = []
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler):
            continue
        if not isinstance(node.type, (ast.Name, type(None))):
            continue
        if isinstance(node.type, ast.Name) and node.type.id not in WIDE_HANDLERS:
            continue
        if node.body and all(isinstance(item, (ast.Pass, ast.Continue)) for item in node.body):
            found.append(f"{path.name}:{node.lineno}")
    return found


class TestEnginePredicate:
    @pytest.mark.parametrize("value", TRUE_VALUES)
    def test_flag_on(self, engine_config, monkeypatch, value):
        monkeypatch.setenv("STEP_PIPELINE_DEBUG", value)
        assert engine_config.debug_enabled() is True

    @pytest.mark.parametrize("value", FALSE_VALUES)
    def test_flag_off(self, engine_config, monkeypatch, value):
        monkeypatch.setenv("STEP_PIPELINE_DEBUG", value)
        assert engine_config.debug_enabled() is False

    def test_flag_unset(self, engine_config, monkeypatch):
        monkeypatch.delenv("STEP_PIPELINE_DEBUG", raising=False)
        assert engine_config.debug_enabled() is False

    def test_note_is_silent_without_flag(self, engine_config, monkeypatch, capsys):
        monkeypatch.delenv("STEP_PIPELINE_DEBUG", raising=False)
        engine_config.debug_note("commands", ValueError("сломалось"))
        assert capsys.readouterr().err == ""

    def test_note_goes_to_stderr_with_flag(self, engine_config, monkeypatch, capsys):
        monkeypatch.setenv("STEP_PIPELINE_DEBUG", "1")
        engine_config.debug_note("commands", ValueError("сломалось"))
        err = capsys.readouterr().err
        assert "[commands] подавлено" in err
        assert "сломалось" in err


class TestPluginPredicate:
    def test_silent_without_flag(self, monkeypatch, capsys):
        monkeypatch.delenv("STEP_PIPELINE_DEBUG", raising=False)
        helpers.load_plugin()
        from step_pipeline import project as proj

        proj.debug_note("update", RuntimeError("нет сети"))
        assert capsys.readouterr().err == ""

    def test_goes_to_stderr_with_flag(self, monkeypatch, capsys):
        monkeypatch.setenv("STEP_PIPELINE_DEBUG", "true")
        helpers.load_plugin()
        from step_pipeline import project as proj

        proj.debug_note("update", RuntimeError("нет сети"))
        err = capsys.readouterr().err
        assert "[update] подавлено" in err and "нет сети" in err

    def test_both_halves_agree(self, engine_config, monkeypatch):
        helpers.load_plugin()
        from step_pipeline import project as proj

        assert proj.DEBUG_ENV == engine_config.DEBUG_ENV
        for value in TRUE_VALUES + FALSE_VALUES:
            monkeypatch.setenv("STEP_PIPELINE_DEBUG", value)
            expected = value not in FALSE_VALUES
            assert engine_config.debug_enabled() is expected, value


def test_engine_has_no_silent_wide_handlers():
    silent = [item for path in sorted(helpers.ENGINE.glob("*.py"))
              for item in silent_handlers(path)]
    assert silent == [], ("пойманные исключения должны попадать в debug_note: "
                          + ", ".join(silent))


def test_plugin_has_no_silent_wide_handlers():
    silent = [item for path in sorted(Path(helpers.PLUGIN).glob("*.py"))
              for item in silent_handlers(path)]
    assert silent == [], "глотать исключения молча нельзя: " + ", ".join(silent)

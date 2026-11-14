# -*- coding: utf-8 -*-
"""Дрейф копии движка (S3 0.23.0): doctor видит расхождение раньше стража.

Прецедент 02.11.2026: в проекте работала копия `gate.py` на 527 строк меньше, чем
в плагине, при одинаковом `VERSION` 0.2.0. Страж это ловит только по `ENGINE.sha256`
и только тиком — то есть конвейер встаёт, владелец видит отказ. `doctor` обязан
сказать об этом заранее: и когда копию правили руками (маркер разошёлся), и когда
версия равна, а байты — нет.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import helpers
from step_pipeline import doctor, setup_flow
from step_pipeline import project as proj
from step_pipeline import snapshot as snap


@pytest.fixture()
def engine_project(project: Path, monkeypatch, tmp_path: Path) -> Path:
    """Проект с установленным движком и погашенными внешними источниками doctor'а."""
    db = tmp_path / "kanban.db"
    db.write_text("", encoding="utf-8")
    monkeypatch.setenv("PIPELINE_KANBAN_DB", str(db))
    monkeypatch.setattr(proj, "profile_dir", lambda name: tmp_path / "profiles" / name)
    monkeypatch.setattr(snap, "snapshot", lambda *a, **k: {})
    monkeypatch.setattr(proj, "run_hermes",
                        lambda *a, **k: SimpleNamespace(returncode=0, stdout="", stderr=""))
    return project


def engine_problems(report: doctor.Report) -> list[str]:
    return [item for item in report.problems if item.startswith("движок:")]


def engine_notes(report: doctor.Report) -> list[str]:
    return [item for item in report.notes if item.startswith("движок:")]


def write_versions(project: Path, version: str | None = None) -> None:
    """Записать `.pipeline/VERSION` (по умолчанию версию плагина)."""
    (project / ".pipeline" / "VERSION").write_text(version or setup_flow.plugin_version() + "\n",
                                                   encoding="utf-8")


class TestEngineDrift:
    def test_clean_copy_is_a_note(self, engine_project: Path):
        write_versions(engine_project)
        report = doctor.collect(engine_project, with_lint=False)
        assert engine_problems(report) == []
        assert "движок: копия совпадает с плагином" in engine_notes(report)

    def test_same_version_different_bytes_is_a_problem(self, engine_project: Path):
        """Ровно тот случай, из-за которого писался маркер: версия равна, логика разная."""
        write_versions(engine_project)
        gate_file = engine_project / ".pipeline" / "gate.py"
        gate_file.write_text(gate_file.read_text(encoding="utf-8") + "\n# правка руками\n",
                             encoding="utf-8")

        report = doctor.collect(engine_project, with_lint=False)

        problems = engine_problems(report)
        assert len(problems) == 1
        assert "копия расходится с плагином" in problems[0]
        assert "gate.py" in problems[0]
        assert "setup --upgrade" in problems[0]

    def test_marker_mismatch_is_reported(self, engine_project: Path):
        """Маркер сверяется раньше байтов: после правки руками тик вообще не запустится."""
        write_versions(engine_project)
        (engine_project / ".pipeline" / setup_flow.ENGINE_MARKER).write_text(
            "sha256:" + "0" * 64, encoding="utf-8")

        report = doctor.collect(engine_project, with_lint=False)

        problems = engine_problems(report)
        assert len(problems) == 1
        assert "изменён после установки" in problems[0]

    def test_empty_marker_is_reported(self, engine_project: Path):
        write_versions(engine_project)
        (engine_project / ".pipeline" / setup_flow.ENGINE_MARKER).write_text("", encoding="utf-8")

        report = doctor.collect(engine_project, with_lint=False)

        assert any("маркер целостности пуст" in item for item in engine_problems(report))

    def test_older_version_reports_once(self, engine_project: Path):
        """Расхождение версий — уже проблема; второй строки про байты быть не должно."""
        write_versions(engine_project, "0.0.1")
        gate_file = engine_project / ".pipeline" / "gate.py"
        gate_file.write_text(gate_file.read_text(encoding="utf-8") + "\n# старая копия\n",
                             encoding="utf-8")

        report = doctor.collect(engine_project, with_lint=False)

        assert not engine_problems(report)
        assert any("старее плагина" in item for item in report.problems)

    def test_missing_marker_is_legacy_and_not_a_problem(self, engine_project: Path):
        """Легаси-установка без маркера: чистая копия — не поломка."""
        write_versions(engine_project)

        report = doctor.collect(engine_project, with_lint=False)

        assert engine_problems(report) == []

    def test_extra_python_file_is_a_warning_not_a_problem(self, engine_project: Path):
        """Лишний `.py` в каталоге движка — предупреждение, а не красный doctor (S7)."""
        write_versions(engine_project)
        setup_flow.write_engine_marker(engine_project)
        (engine_project / ".pipeline" / "check_concurrent_pytest.py").write_text(
            "print(1)\n", encoding="utf-8")

        report = doctor.collect(engine_project, with_lint=False)

        assert engine_problems(report) == []
        assert "движок: копия совпадает с плагином" in engine_notes(report)
        assert any("в каталоге движка лежит не-движковый файл: check_concurrent_pytest.py"
                   in item for item in report.warnings)

    def test_missing_engine_file_is_a_problem(self, engine_project: Path):
        """Пропавший файл списка — проблема по имени файла (тик на такой копии встанет)."""
        write_versions(engine_project)
        setup_flow.write_engine_marker(engine_project)
        (engine_project / ".pipeline" / "state_io.py").unlink()

        report = doctor.collect(engine_project, with_lint=False)

        problems = engine_problems(report)
        assert len(problems) == 1, problems
        assert "нет файла движка: state_io.py" in problems[0]


class TestEngineDriftFunctions:
    def test_drift_lists_exact_files(self, engine_project: Path):
        (engine_project / ".pipeline" / "tick.py").write_text("# чужой тик\n", encoding="utf-8")
        assert setup_flow.engine_drift(engine_project) == ["tick.py"]

    def test_drift_sees_file_present_on_one_side(self, engine_project: Path):
        (engine_project / ".pipeline" / "state_io.py").unlink()
        assert "state_io.py" in setup_flow.engine_drift(engine_project)

    def test_drift_of_missing_pipeline_dir_is_empty(self, tmp_path: Path):
        assert setup_flow.engine_drift(tmp_path) == []

    def test_crlf_copy_is_refreshed_by_upgrade(self, engine_project: Path):
        """Копия с виндовыми переводами строк — дрейф; upgrade её перезаписывает.

        Регресс 0.25.3: `copy_engine` сверял текст (`read_text` съедает CRLF) и считал такую
        копию идентичной, поэтому файл не перезаписывался, а `doctor` сравнивал байты и
        вечно требовал `setup --upgrade` (живой случай — registry.py, scout_report.py).
        """
        src = proj.ENGINE_SRC / "registry.py"
        target = engine_project / ".pipeline" / "registry.py"
        target.write_bytes(src.read_text(encoding="utf-8").replace("\n", "\r\n").encode("utf-8"))
        assert "registry.py" in setup_flow.engine_drift(engine_project)

        copied = setup_flow.copy_engine(engine_project, force=False)

        assert ".pipeline/registry.py" in copied
        assert setup_flow.engine_drift(engine_project) == []
        assert target.read_bytes() == src.read_bytes()

    def test_marker_problem_of_clean_copy_is_empty(self, engine_project: Path):
        assert setup_flow.engine_marker_problem(engine_project) == ""

    def test_extra_python_file_is_not_drift_and_not_in_marker(self, engine_project: Path):
        """Свой скрипт проекта в `.pipeline/` — не файл движка (S7 0.27.0).

        Прецедент раскатки 07.11.2026: три скрипта проверок в DoorSystem считались дрейфом,
        а после их удаления не сходился маркер — doctor был красным при исправном движке.
        """
        setup_flow.write_engine_marker(engine_project)
        for name in ("check_concurrent_pytest.py", "s3_import_check.py", "s3_proof.py"):
            (engine_project / ".pipeline" / name).write_text("print(1)\n", encoding="utf-8")

        assert setup_flow.engine_drift(engine_project) == []
        assert setup_flow.engine_marker_problem(engine_project) == ""
        assert setup_flow.engine_extra(engine_project) == [
            "check_concurrent_pytest.py", "s3_import_check.py", "s3_proof.py"]

    def test_missing_engine_file_is_named_in_marker_problem(self, engine_project: Path):
        """Пропавший файл списка — проблема с именем файла, а не «движок изменён»."""
        setup_flow.write_engine_marker(engine_project)
        (engine_project / ".pipeline" / "state_io.py").unlink()

        problem = setup_flow.engine_marker_problem(engine_project)

        assert problem == "в копии нет файла движка: state_io.py"
        assert "state_io.py" in setup_flow.engine_drift(engine_project)

    def test_marker_problem_matches_gate_verdict(self, engine_project: Path):
        """Семантика доктора и стража совпадает: маркер тот же, вывод — тот же."""
        import sys

        sys.path.insert(0, str(helpers.ENGINE))
        import gate  # noqa: PLC0415 - движок кладётся на путь здесь же

        digest = gate.engine_digest(engine_project / ".pipeline")
        (engine_project / ".pipeline" / gate.ENGINE_MARKER).write_text("sha256:" + digest,
                                                                      encoding="utf-8")
        assert setup_flow.engine_marker_problem(engine_project) == ""

        (engine_project / ".pipeline" / "gate.py").write_text("x = 1\n", encoding="utf-8")
        ok, why = gate.verify_engine_integrity(engine_project / ".pipeline")
        assert not ok
        assert "изменён после установки" in setup_flow.engine_marker_problem(engine_project)


def test_cli_doctor_exit_code_on_drift(engine_project: Path, capsys):
    """Код возврата `doctor` ненулевой: предупреждением это не считается."""
    from step_pipeline import cli

    write_versions(engine_project)
    (engine_project / ".pipeline" / "gate.py").write_text("x = 1\n", encoding="utf-8")

    code = cli.cmd_doctor(SimpleNamespace(project=str(engine_project), plan=""))

    out = capsys.readouterr().out
    assert code == 1
    assert "ИТОГ: ЕСТЬ ПРОБЛЕМЫ" in out
    assert "копия расходится с плагином" in out


def test_cli_doctor_green_with_extra_python_file(engine_project: Path, capsys, monkeypatch):
    """Лишний `.py` в `.pipeline/` не краснит `doctor`: код 0, `ИТОГ: OK`, строка `!`.

    Это вторая половина шага S7: раньше такой файл давал «копия расходится с плагином»
    и `upgrade` не проходил; теперь он предупреждение, а не проблема. Доверие и ключи
    профилей подменены согласием — иначе `doctor` красный по причине, к дрейфу не
    относящейся (фикстура поднимает пустой каталог профилей).
    """
    from step_pipeline import cli

    monkeypatch.setattr(setup_flow, "is_project_trusted", lambda role, path: True)
    monkeypatch.setattr(setup_flow, "has_saved_credential", lambda role: (True, "demo"))
    write_versions(engine_project)
    setup_flow.write_engine_marker(engine_project)
    (engine_project / ".pipeline" / "helper.py").write_text("print(1)\n", encoding="utf-8")

    code = cli.cmd_doctor(SimpleNamespace(project=str(engine_project), plan=""))

    out = capsys.readouterr().out
    assert code == 0, out
    assert "ИТОГ: OK" in out
    assert "копия расходится с плагином" not in out
    assert "! в каталоге движка лежит не-движковый файл: helper.py" in out


def test_drift_is_not_reported_for_preview(project: Path, monkeypatch, tmp_path: Path):
    """Предпросмотр без .pipeline не читает движок и ничего не выдумывает."""
    bare = tmp_path / "bare"
    (bare / "docs" / "plans").mkdir(parents=True)
    (bare / "docs" / "plans" / "1-demo.md").write_text(helpers.PLAN, encoding="utf-8")
    config = json.loads(json.dumps(helpers.CONFIG))
    (bare / "pipeline.json").write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
    db = tmp_path / "kanban.db"
    db.write_text("", encoding="utf-8")
    monkeypatch.setenv("PIPELINE_KANBAN_DB", str(db))

    report = doctor.collect(bare, with_lint=False, preview=True)

    assert engine_problems(report) == []

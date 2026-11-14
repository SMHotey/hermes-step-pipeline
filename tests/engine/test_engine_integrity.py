# -*- coding: utf-8 -*-
"""Целостность движка: маркер ENGINE.sha256 и отказ тика при дрейфе копии.

Прецедент 02.11.2026: в проекте работала копия `.pipeline/gate.py` (1364 строки),
а в плагине лежала другая (1599 строк) — обе с VERSION 0.2.0. Один план двигали
две разные логики. Маркер закрывает это: хеш содержимого, а не номер версии.

С 0.27.0 хеш считается по именам `ENGINE_FILES`, а не по каталогу: вспомогательный
`.py` проекта (прецедент раскатки 07.11.2026 — три скрипта проверок в DoorSystem)
не должен ни менять маркер, ни считаться дрейфом. Пропавший файл списка — ошибка.
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from engine_kit import ENGINE, ENGINE_FILES  # noqa: E402

sys.path.insert(0, str(ENGINE))
import config as engine_config  # noqa: E402
import gate  # noqa: E402  (движок проекта: config/registry/state_io лежат рядом)

ROOT = ENGINE.parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture()
def engine_dir(tmp_path: Path) -> Path:
    """Каталог движка с полным набором имён `ENGINE_FILES` — как в живом проекте."""
    base = tmp_path / "engine"
    base.mkdir()
    for pos, name in enumerate(ENGINE_FILES):
        (base / name).write_bytes(f"x = {pos}\n".encode("utf-8"))
    return base


def write_marker(base: Path) -> str:
    """Маркер по текущему содержимому каталога — «движок только что установили»."""
    digest = gate.engine_digest(base)
    (base / gate.ENGINE_MARKER).write_text("sha256:" + digest, encoding="utf-8")
    return digest


class TestEngineDigest:
    def test_digest_is_stable_and_sensitive(self, engine_dir):
        first = gate.engine_digest(engine_dir)
        assert first == gate.engine_digest(engine_dir), "хеш должен быть детерминирован"
        (engine_dir / "gate.py").write_text("x = 2", encoding="utf-8")
        assert gate.engine_digest(engine_dir) != first, "правка файла движка меняет хеш"

    def test_extra_python_file_does_not_change_digest(self, engine_dir):
        """Свой скрипт проекта в `.pipeline/` — не движок: маркер не должен «расходиться»."""
        first = write_marker(engine_dir)
        (engine_dir / "check_concurrent_pytest.py").write_text("print(1)", encoding="utf-8")
        (engine_dir / "s3_proof.py").write_text("print(2)", encoding="utf-8")
        assert gate.engine_digest(engine_dir) == first
        ok, why = gate.verify_engine_integrity(engine_dir)
        assert ok, why

    def test_digest_ignores_non_python(self, engine_dir):
        first = gate.engine_digest(engine_dir)
        (engine_dir / "ENGINE.sha256").write_text("sha256:deadbeef", encoding="utf-8")
        (engine_dir / "notes.txt").write_text("текст", encoding="utf-8")
        assert gate.engine_digest(engine_dir) == first, "маркер и txt не входят в хеш"


class TestVerifyEngineIntegrity:
    def test_matching_marker_passes(self, engine_dir):
        write_marker(engine_dir)
        ok, why = gate.verify_engine_integrity(engine_dir)
        assert ok, why

    def test_drift_is_reported(self, engine_dir):
        write_marker(engine_dir)
        (engine_dir / "registry.py").write_text("y = 999", encoding="utf-8")
        ok, why = gate.verify_engine_integrity(engine_dir)
        assert not ok
        assert "изменён" in why

    def test_missing_marker_is_legacy_install(self, engine_dir):
        ok, why = gate.verify_engine_integrity(engine_dir)
        assert ok
        assert "легаси" in why

    def test_empty_marker_blocks(self, engine_dir):
        (engine_dir / gate.ENGINE_MARKER).write_text("", encoding="utf-8")
        ok, _ = gate.verify_engine_integrity(engine_dir)
        assert not ok

    def test_missing_engine_file_is_named(self, engine_dir):
        """Пропавший файл движка — ошибка по имени файла, а не «движок изменён»."""
        write_marker(engine_dir)
        (engine_dir / "state_io.py").unlink()
        ok, why = gate.verify_engine_integrity(engine_dir)
        assert not ok
        assert why == "нет файла движка: state_io.py"

    def test_missing_engine_file_blocks_without_marker_too(self, tmp_path):
        """Копия без половины движка не должна считаться легаси-установкой при маркере."""
        base = tmp_path / "engine"
        base.mkdir()
        (base / "gate.py").write_text("x = 1\n", encoding="utf-8")
        (base / gate.ENGINE_MARKER).write_text("sha256:" + "0" * 64, encoding="utf-8")
        ok, why = gate.verify_engine_integrity(base)
        assert not ok
        assert "нет файла движка:" in why


class TestSetupFlowAgreesWithGate:
    def test_marker_written_by_setup_matches_gate_digest(self, tmp_path):
        from step_pipeline.setup_flow import write_engine_marker

        project = tmp_path / "proj"
        (project / ".pipeline").mkdir(parents=True)
        for src in Path(ENGINE).glob("*.py"):
            shutil.copy2(src, project / ".pipeline" / src.name)

        digest = write_engine_marker(project)

        assert (project / ".pipeline" / gate.ENGINE_MARKER).is_file()
        assert gate.engine_digest(project / ".pipeline") == digest
        assert gate.verify_engine_integrity(project / ".pipeline")[0] is True

    def test_two_digest_implementations_agree_on_repo_engine(self):
        """Дубль digest'а (`gate` и `setup_flow`) — не расходится ни на байт.

        Реализации оставлены раздельными намеренно: плагин не импортирует движок
        (решение docs/CONTEXT.md:49). Согласие стережёт этот тест, а не общий модуль.
        """
        from step_pipeline.setup_flow import engine_digest as setup_digest

        assert setup_digest(ENGINE) == gate.engine_digest(ENGINE)

    def test_two_digest_implementations_agree_on_empty_dir(self, tmp_path):
        from step_pipeline.setup_flow import engine_digest as setup_digest

        empty = tmp_path / "empty"
        empty.mkdir()
        assert setup_digest(empty) == gate.engine_digest(empty)

    def test_vendored_file_list_matches_engine_dir(self):
        """Список копируемых файлов не должен отставать от каталога движка.

        Иначе новый модуль движка не доедет до проекта: `gate.py` на него сошлётся,
        а в `.pipeline/` его не будет — ImportError в cron. С 0.27.0 список один на
        весь конвейер и живёт в движке (`engine/config.py`): мастер установки и страж
        берут его оттуда, а не держат по копии.
        """
        from step_pipeline.setup_flow import ENGINE_FILES as setup_files
        from engine_kit import ENGINE_FILES as kit_files

        present = {path.name for path in Path(ENGINE).glob("*.py")}
        assert set(setup_files) == present, "список файлов движка отстал от каталога"
        assert set(kit_files) == set(setup_files), "список в тестах разошёлся с плагином"
        assert tuple(engine_config.ENGINE_FILES) == tuple(setup_files)
        assert gate.config_mod is engine_config, "страж обязан брать список из движка"

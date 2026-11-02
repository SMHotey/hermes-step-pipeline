# -*- coding: utf-8 -*-
"""Целостность движка: маркер ENGINE.sha256 и отказ тика при дрейфе копии.

Прецедент 02.11.2026: в проекте работала копия `.pipeline/gate.py` (1364 строки),
а в плагине лежала другая (1599 строк) — обе с VERSION 0.2.0. Один план двигали
две разные логики. Маркер закрывает это: хеш содержимого, а не номер версии.
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from engine_kit import ENGINE  # noqa: E402

sys.path.insert(0, str(ENGINE))
import gate  # noqa: E402  (движок проекта: config/registry/state_io лежат рядом)

ROOT = ENGINE.parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture()
def engine_dir(tmp_path: Path) -> Path:
    base = tmp_path / "engine"
    base.mkdir()
    (base / "a.py").write_text("x = 1", encoding="utf-8")
    (base / "b.py").write_text("y = 2", encoding="utf-8")
    return base


class TestEngineDigest:
    def test_digest_is_stable_and_sensitive(self, engine_dir):
        first = gate.engine_digest(engine_dir)
        assert first == gate.engine_digest(engine_dir), "хеш должен быть детерминирован"
        (engine_dir / "a.py").write_text("x = 2", encoding="utf-8")
        assert gate.engine_digest(engine_dir) != first, "правка файла меняет хеш"

    def test_new_file_changes_digest(self, engine_dir):
        first = gate.engine_digest(engine_dir)
        (engine_dir / "c.py").write_text("z = 3", encoding="utf-8")
        assert gate.engine_digest(engine_dir) != first

    def test_digest_ignores_non_python(self, engine_dir):
        first = gate.engine_digest(engine_dir)
        (engine_dir / "ENGINE.sha256").write_text("sha256:deadbeef", encoding="utf-8")
        (engine_dir / "notes.txt").write_text("текст", encoding="utf-8")
        assert gate.engine_digest(engine_dir) == first, "маркер и txt не входят в хеш"


class TestVerifyEngineIntegrity:
    def test_matching_marker_passes(self, engine_dir):
        digest = gate.engine_digest(engine_dir)
        (engine_dir / gate.ENGINE_MARKER).write_text("sha256:" + digest, encoding="utf-8")
        ok, why = gate.verify_engine_integrity(engine_dir)
        assert ok, why

    def test_drift_is_reported(self, engine_dir):
        digest = gate.engine_digest(engine_dir)
        (engine_dir / gate.ENGINE_MARKER).write_text("sha256:" + digest, encoding="utf-8")
        (engine_dir / "b.py").write_text("y = 999", encoding="utf-8")
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

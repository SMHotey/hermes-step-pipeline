# -*- coding: utf-8 -*-
"""Уведомления называют план: иначе два плана с шагом S1 выглядят дублем."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def test_step_message_has_plan_id(project: Path, hermes_stub: Path) -> None:
    """Сообщение о взятии шага содержит номер плана, а не только id шага."""
    raw = json.loads((project / "pipeline.json").read_text(encoding="utf-8"))
    raw["require_prompt"] = False
    (project / "pipeline.json").write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")

    env = {**os.environ, "PIPELINE_REPO": str(project)}
    res = subprocess.run(
        [sys.executable, str(project / ".pipeline" / "gate.py"), "--run"],
        cwd=str(project), capture_output=True, text=True, encoding="utf-8",
        errors="replace", env=env,
    )
    out = (res.stdout or "") + (res.stderr or "")
    assert res.returncode == 0, out
    assert "план 1, шаг S1" in out, out
    assert "что делаем:" in out, out

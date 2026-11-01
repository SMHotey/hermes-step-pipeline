# -*- coding: utf-8 -*-
"""Тик конвейера для cron (`--no-agent`): один шаг стража `gate.py --run`.

Молчание в выводе = конвейер ждёт (шаг в работе, ревью идёт, стоп-флаг). Поэтому
джобу заводят с `--deliver local`: stdout попадёт в историю запусков, а не в чат.

Куда кладут этот файл: `profiles/<профиль>/scripts/pipeline_tick.py` — cron принимает
`--script` только оттуда. Корень проекта берётся из `PIPELINE_REPO`, иначе из `--workdir`
джобы (текущий каталог), иначе из расположения файла (когда скрипт лежит в репо).
"""
from __future__ import annotations

import os
import runpy
import sys
from pathlib import Path


def find_gate() -> Path | None:
    candidates = []
    repo = os.environ.get("PIPELINE_REPO")
    if repo:
        candidates.append(Path(repo) / ".pipeline" / "gate.py")
    candidates.append(Path.cwd() / ".pipeline" / "gate.py")
    candidates.append(Path(__file__).resolve().parent / "gate.py")
    candidates.append(Path(__file__).resolve().parents[1] / ".pipeline" / "gate.py")
    for cand in candidates:
        if cand.is_file():
            return cand
    return None


def main() -> int:
    gate = find_gate()
    if gate is None:
        print("pipeline_tick: gate.py не найден (задай PIPELINE_REPO или --workdir джобы)",
              file=sys.stderr)
        return 1
    sys.argv = [str(gate), "--run"]
    runpy.run_path(str(gate), run_name="__main__")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

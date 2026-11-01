# -*- coding: utf-8 -*-
"""Ручной толчок: поставить следующий шаг плана карточкой канбана (не в чате).

Автоматический конвейер (шаг → ревью → следующий шаг) делает `gate.py`; этот скрипт —
разовый запуск одного шага по команде владельца. Тело карточки то же
(`gate.step_card_body`), чтобы ручной и автоматический запуск не расходились.

Использование:
    python .pipeline/step_card.py --dry-run      # показать, что будет создано
    python .pipeline/step_card.py                # создать карточку
    python .pipeline/step_card.py --step S3a     # конкретный шаг
    python .pipeline/step_card.py --plan 3       # другой план
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config as config_mod  # noqa: E402 - модуль рядом
import gate  # noqa: E402 - тексты карточек и создание
import registry  # noqa: E402 - реестр планов


def main(argv: list[str] | None = None) -> int:
    config_mod.setup_stdio()
    ap = argparse.ArgumentParser(description="Следующий шаг плана → карточка канбана")
    ap.add_argument("--plan", help="номер плана (по умолчанию из pipeline.json)")
    ap.add_argument("--step", help="id шага (S3a); по умолчанию — следующий открытый")
    ap.add_argument("--dry-run", action="store_true", help="только показать карточку")
    args = ap.parse_args(argv)

    try:
        cfg = config_mod.load(gate.repo_root())
        p = gate.resolve_paths(cfg, args.plan)
    except config_mod.ConfigError as exc:
        print(f"ошибка: {exc}", file=sys.stderr)
        return 2
    except registry.PlanNotFound as exc:
        print(f"ошибка: {exc}", file=sys.stderr)
        return 2

    package = gate.collector(p, args.step)
    step = package.get("next") or {}
    if not step:
        raise SystemExit(f"план {p.plan_id}: все шаги закрыты — делать нечего")
    prompt = gate.prompt_rel(p, step["id"])
    body = gate.step_card_body(p, step, prompt)
    title = f"[план {p.plan_id} {step['id']}] {step['title']}"

    if args.dry_run:
        print(title)
        print("-" * 60)
        print(body)
        return 0

    cid = gate._create_card(p, title, body, gate._step_assignee(p, step),
                            f"planloop:{p.plan_id}:{step['id']}:step",
                            p.cfg.step_skills, max_runtime=p.cfg.step_runtime)
    if not cid:
        print("карточка не создана — проверь `hermes kanban --board "
              f"{p.cfg.board} ls`", file=sys.stderr)
        return 1
    print(f"карточка шага {step['id']}: {cid}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

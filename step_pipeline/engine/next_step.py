# -*- coding: utf-8 -*-
"""Сборщик контекста следующего шага плана: одна команда — весь вход исполнителю.

Источник истины — ФАЙЛ плана, без списков шагов в коде:
    раздел, в заголовке которого есть маркер шагов («шаг»);
    внутри — заголовки `### S3. Название (2 дн) `owner``;
    пункты секции: «Файл:», «Тесты/гейты:», строка «Готово =».

Использование:
    python .pipeline/next_step.py                # пакет контекста
    python .pipeline/next_step.py --brief        # короткая версия (для промта)
    python .pipeline/next_step.py --json         # машинный формат (его читает gate.py)
    python .pipeline/next_step.py --step S3      # принудительно конкретный шаг
    python .pipeline/next_step.py --list         # все шаги плана и статус
    python .pipeline/next_step.py --list-plans   # все планы: следующий шаг, закрыто

Скрипт только ЧИТАЕТ репозиторий и ничего не меняет.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config as config_mod  # noqa: E402 - модуль рядом
import registry  # noqa: E402 - реестр планов


@dataclass
class Step:
    """Один шаг плана с контрактом выполнения."""

    id: str
    epoch: str
    title: str
    days: float
    owner: str
    goal: str
    files: list[str]
    gates: list[str]
    dod: str
    read: list[str]
    notes: str = ""
    # Риск шага из пункта «Риск: high|low|normal» (нет пункта — normal) и признак
    # «Авто: да»: контракт шага, по которому страж решает про ручной допуск.
    risk: str = "normal"
    auto: bool = False


# Маркер пункта ВЕРХНЕГО уровня: строка с отступом — продолжение предыдущего пункта.  # ANCHOR:пункты
_BULLET_RE = re.compile(r"^(?:[-*+]|\d+\.)\s+(?P<text>.*)$")
_DAYS_RE = re.compile(r"\((?P<days>\d+(?:[.,]\d+)?)\s*(?:[–-]\s*\d+(?:[.,]\d+)?\s*)?(?:дн|день|дня|дней)")
_DAYS_PAREN_RE = re.compile(r"\s*\([^()]*(?:дн|день|дня|дней)[^()]*\)")
_OWNER_RE = re.compile(r"`([^`]+)`")
_READY_RE = re.compile(r"готово(?:\s*\([^)]*\))?\s*[=:]\s*(?P<rest>.+)", re.IGNORECASE)
_OWNER_LINE_RE = re.compile(r"^(?:исполнитель|owner)\s*[=:]\s*`?([a-z][a-z0-9_-]{1,30})`?",
                            re.IGNORECASE)
# Пункты, которые НЕ годятся в «цель шага».
_GOAL_SKIP_LABELS = ("файл", "тест", "гейт", "готово", "замер", "доказательств",
                     "запрет", "важно", "исполнитель", "owner", "чита",
                     "риск", "авто")


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _risk_of(text: str) -> str:
    """Риск шага из значения пункта «Риск: …».

    Нераспознанная пометка (пусто, опечатка, русское слово) считается `high`:
    ошибка в плане не должна делать опасный шаг безопасным (D2). «По умолчанию
    normal» относится только к отсутствию пункта — его разбирает `_step_from_section`.
    """
    token = (_norm(text).split() or [""])[0].lower().strip(".,;:()«»\"'")
    return token if token in ("low", "normal", "high") else "high"


def _yes(text: str) -> bool:
    """Пункт «Авто: да» — признак зафиксированной договорённости, не отмена риска."""
    token = (_norm(text).split() or [""])[0].lower().strip(".,;:()«»\"'")
    return token in ("да", "yes", "true", "1")


def section_blocks(lines: list[str]) -> list[tuple[str, str, str]]:
    """Пункты раздела плана: (метка, текст, полный текст).

    Метка — префикс пункта до двоеточия, текст — остаток после него, полный —
    весь пункт целиком (метка + текст): по нему ищется критерий «Готово =»,
    даже если внутри критерия есть своё двоеточие.
    """
    chunks: list[tuple[str, list[str]]] = []
    for line in lines:
        if not line.strip():
            continue
        m = _BULLET_RE.match(line)
        if m:
            chunks.append((m.group("text").strip(), []))
        elif chunks:
            chunks[-1][1].append(line.strip())
        else:
            chunks.append((line.strip(), []))
    out: list[tuple[str, str, str]] = []
    for head, rest in chunks:
        label = ""
        if ":" in head:
            candidate = head.split(":", 1)[0]
            if 0 < len(candidate) <= 40 and "`" not in candidate:
                label = candidate.strip().lower()
        body = head.split(":", 1)[1].strip() if label else head
        tail = " ".join(rest)
        text = _norm(body + " " + tail) if rest else _norm(body)
        full = _norm(head + " " + tail) if rest else _norm(head)
        out.append((label, text, full))
    return out


def _backticks(text: str) -> list[str]:
    return [t.strip() for t in re.findall(r"`([^`]+)`", text) if t.strip()]


def _looks_like_path(token: str) -> bool:
    return " " not in token and ("/" in token or "." in token)


def _days_and_owner(title: str) -> tuple[float, str]:
    days = 0.0
    m = _DAYS_RE.search(title)
    if m:
        days = float(m.group("days").replace(",", "."))
    tail = title[m.end():] if m else title
    owner = _OWNER_RE.search(tail) or _OWNER_RE.search(title)
    return days, (owner.group(1) if owner else "")


def steps_section(cfg: config_mod.Config, lines: list[str]) -> tuple[int, int] | None:
    """Границы раздела со словом-маркером шагов (индекс заголовка, индекс конца)."""
    found = registry.headings(lines)
    marker = cfg.steps_marker
    for pos, (idx, level, title) in enumerate(found):
        if level >= 2 and marker in title.lower():
            return idx, registry.section_end(found, pos, len(lines))
    return None


def _step_from_section(cfg: config_mod.Config, sid: str, raw_title: str,
                       body: list[str], plan_file: Path) -> Step:
    """Контракт шага из его секции `### <ШАГ>. название` в файле плана."""
    days, owner = _days_and_owner(raw_title)
    title_src = _OWNER_RE.sub("", raw_title)                      # имена профилей в кавычках
    title_src = _DAYS_PAREN_RE.sub("", title_src)                 # «(1 день, …)», «(2 дн)»
    title_src = re.sub(r"\(\s*[,\s]*\)", "", title_src)           # пустые скобки после чистки
    title_src = re.sub(r"\s*[+,]\s*$", "", title_src).strip()     # хвостовой мусор
    title = _norm(title_src) or _norm(raw_title)
    blocks = section_blocks(body)
    goal = ""
    for label, text, _full in blocks:
        if len(text) < 15 or (label and label.startswith(_GOAL_SKIP_LABELS)):
            continue
        if text.lower().startswith("готово"):
            continue
        cut = re.search(r"\s\d+\.\s", text)
        goal = (text[:cut.start()].rstrip(" ;.,") + " …") if cut and cut.start() > 60 else text[:400]
        break
    goal = goal or title
    files: list[str] = []
    gates: list[str] = []
    dod = ""
    notes: list[str] = []
    risk = "normal"
    auto = False
    for label, text, full in blocks:
        # ANCHOR:файлы — стабильная ссылка для спеки и ревью
        if label.startswith("файл"):
            files += [t for t in _backticks(text) if _looks_like_path(t)]
        # ANCHOR:гейты — стабильная ссылка для спеки и ревью
        elif label.startswith(("тест", "гейт", "проверк")):
            gates.append(text)
        # ANCHOR:заметки — стабильная ссылка для спеки и ревью
        elif label.startswith(("важно", "примечани", "ограничени")):
            notes.append(text)
        elif label.startswith("риск"):
            risk = _risk_of(text)
        elif label.startswith("авто"):
            auto = _yes(text)
        if not owner and label.startswith(("исполнитель", "owner")):
            m = _OWNER_LINE_RE.match(text) or _OWNER_RE.search(text)
            if m:
                owner = m.group(1)
        if not dod:
            # Критерий «Готово» ищется по полному тексту пункта, а не по обрезке.
            ready = _READY_RE.search(full)  # ANCHOR:критерий
            if ready:
                dod = ready.group("rest").strip()
    return Step(id=sid, epoch="", title=title, days=days, owner=owner, goal=goal,
                files=files, gates=gates, dod=dod,
                read=[f"{cfg.rel(plan_file)} §{sid}"], notes=" ".join(notes),
                risk=risk, auto=auto)


def plan_steps(cfg: config_mod.Config, plan_file: Path) -> list[Step]:
    """Шаги плана из файла: раздел шагов → заголовки `### <ШАГ>. название`.

    Идентификатор шага разбирается выражением `step_id_regex` из `pipeline.json`
    (жёсткого списка нет — годится `T10`, `E15a`, `S25-8`); после идентификатора
    в заголовке обязательна точка: `### Ш1. Название` — шаг, `### Шаг первый` — нет.
    Пустой список = в плане нет раздела шагов с такими заголовками.
    """
    path = Path(plan_file)
    if not path.is_file():
        return []
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    span = steps_section(cfg, lines)
    if not span:
        return []
    start, end = span
    inside = [h for h in registry.headings(lines) if start < h[0] < end]
    steps: list[Step] = []
    for pos, (idx, _level, title) in enumerate(inside):
        m = cfg.step_regex.match(title)
        if not m:
            continue
        rest = title[m.end():].strip()
        if not rest.startswith("."):
            continue
        step_title = rest[1:].strip()
        if not step_title:
            continue
        body_end = registry.section_end(inside, pos, end)
        steps.append(_step_from_section(cfg, m.group("id").upper(), step_title,
                                        lines[idx + 1:body_end], path))
    return steps


def _run(cmd: list[str], cwd: Path) -> str:
    try:
        res = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=60)
    except (OSError, subprocess.SubprocessError):
        return ""
    return (res.stdout or "").strip()


def git_state(cfg: config_mod.Config) -> dict[str, object]:
    repo = str(cfg.repo)
    branch = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cfg.repo)
    sha = _run(["git", "log", "-1", "--oneline"], cfg.repo)
    dirty = [l for l in _run(["git", "status", "--porcelain"], cfg.repo).splitlines() if l.strip()]
    return {"branch": branch or "?", "head": sha or "?", "dirty_count": len(dirty),
            "dirty": dirty[:10], "repo": repo}


def build_package(cfg: config_mod.Config, plan_id: str | None = None,
                  force_step: str | None = None, brief: bool = False) -> dict[str, Any]:
    """Пакет контекста шага: контракт, состояние git, что осталось."""
    plan_id = registry.normalize(plan_id or registry.default_plan_id(cfg))
    spec = registry.get(cfg, plan_id)
    steps = plan_steps(cfg, Path(spec.file))
    closed_all = set(registry.closed_steps(spec, cfg))

    if force_step:
        target = next((s for s in steps if s.id.upper() == force_step.upper()), None)
        if target is None:
            raise SystemExit(f"неизвестный шаг: {force_step} (план {plan_id}). Доступные: " +
                             (", ".join(s.id for s in steps) or "шаги плана не распознаны"))
    else:
        target = next((s for s in steps if s.id.upper() not in closed_all), None)

    pkg: dict[str, Any] = {
        "date": __import__("datetime").datetime.now().strftime("%d.%m.%Y %H:%M"),
        "git": git_state(cfg),
        "project": cfg.project,
        "plan_id": plan_id,
        "plan_file": cfg.rel(Path(spec.file)),
        "closed": sorted(closed_all),
        "steps_total": len(steps),
        "steps": [{"id": s.id, "title": s.title, "days": s.days,
                   "owner": s.owner or cfg.driver,
                   "closed": s.id.upper() in closed_all} for s in steps],
        "next": asdict(target) if target else None,
        "remaining": [s.id for s in steps if s.id.upper() not in closed_all],
        "brief": brief,
    }
    if not steps:
        pkg["note"] = (f"в плане {plan_id} не распознан раздел шагов "
                       f"(заголовки `### <ШАГ>. название`)")
    elif target is None:
        pkg["note"] = f"все шаги плана {plan_id} закрыты — следующих нет"
    return pkg


def render(pkg: dict[str, Any], brief: bool) -> str:
    step = pkg["next"]
    plan_id = pkg.get("plan_id") or "?"
    git = pkg["git"]
    dirty = git["dirty_count"]
    lines: list[str] = [
        f"=== {pkg.get('project', 'проект')}: контекст следующего шага ===",
        f"Дата: {pkg['date']}",
        f"Git: {git['branch']} @ {git['head']} | "
        f"{'дерево чистое' if dirty == 0 else f'грязное дерево: {dirty} файл(ов)'}",
    ]
    for item in git["dirty"]:
        lines.append(f"   {item}")
    lines.append(f"План {plan_id} закрыто: {', '.join(pkg.get('closed') or []) or '—'}")
    lines.append("")
    parts = [f"{step['days']} дн."] if step.get("days") else []
    if step.get("owner"):
        parts.append(f"исполнитель {step['owner']}")
    head = f"СЛЕДУЮЩИЙ ШАГ: {step['id']}"
    lines.append(head + (f" ({', '.join(parts)})" if parts else ""))
    lines.append(f"Название: {step['title']}")
    lines.append(f"Цель: {step['goal']}")
    lines.append("")
    lines.append("Файлы (правим/создаём):")
    lines.extend(f"  - {f}" for f in step["files"])
    lines.append("Гейты (запустить и показать результат):")
    lines.extend(f"  $ {g}" for g in step["gates"])
    lines.append(f"Готово = {step['dod']}")
    if not brief:
        lines += ["", "Что прочитать перед началом:"]
        lines.extend(f"  * {r}" for r in step["read"])
        if step.get("notes"):
            lines.append(f"Важно: {step['notes']}")
        lines += ["", f"Осталось шагов: {', '.join(pkg['remaining'])}", ""]
        lines.append("Порядок работы (скилл pipeline-next-step):")
        lines.append("  1) прочитать постановку шага и файлы контракта;")
        lines.append("  2) выполнить шаг минимальными правками, тесты — контракт;")
        lines.append("  3) прогнать гейты шага, показать фактические цифры;")
        lines.append("  4) коммит и пуш только файлов шага;")
        lines.append("  5) закрыть карточку — журнал и приёмку делает страж после ревью.")
    return "\n".join(lines)


def _print_plans(cfg: config_mod.Config, rows: list[dict[str, Any]]) -> None:
    for row in rows:
        total = row["steps"]
        if row["next"]:
            head = f"следующий шаг — {row['next']} ({row['next_title']})"
        elif total:
            head = "следующий шаг — нет (все шаги закрыты)"
        else:
            head = "следующий шаг — нет (раздел шагов не распознан)"
        closed = f"закрыто {row['closed']}" + (f" из {total}" if total else "")
        print(f"План {row['id']}: {head}, {closed} "
              f"(файл `{row['file']}`, журнал «{row['journal']}»)")


def plans_overview(cfg: config_mod.Config) -> list[dict[str, Any]]:
    """Все планы проекта: номер, следующий открытый шаг, число закрытых и шагов."""
    rows: list[dict[str, Any]] = []
    closed_ids = {spec.id: set(registry.closed_steps(spec, cfg)) for spec in registry.specs(cfg)}
    steps_by_id = {spec.id: plan_steps(cfg, Path(spec.file)) for spec in registry.specs(cfg)}
    for spec in registry.specs(cfg):
        closed = closed_ids[spec.id]
        steps = steps_by_id[spec.id]
        target = next((s for s in steps if s.id.upper() not in closed), None)
        rows.append({"id": spec.id, "file": Path(spec.file).name,
                     "journal": spec.journal_heading,
                     "next": target.id if target else None,
                     "next_title": target.title if target else "",
                     "closed": (sum(1 for s in steps if s.id.upper() in closed)
                                if steps else len(closed)),
                     "steps": len(steps)})
    return rows


def main(argv: list[str] | None = None) -> int:
    config_mod.setup_stdio()
    parser = argparse.ArgumentParser(description="Контекст следующего шага плана")
    parser.add_argument("--plan", help="номер плана (по умолчанию из pipeline.json)")
    parser.add_argument("--step", help="принудительный шаг, например S3")
    parser.add_argument("--json", action="store_true", help="машинный вывод")
    parser.add_argument("--brief", action="store_true", help="короткая версия для промта")
    parser.add_argument("--list", action="store_true", help="все шаги плана и статус")
    parser.add_argument("--list-plans", action="store_true",
                        help="все планы: следующий шаг и число закрытых")
    args = parser.parse_args(argv)

    try:
        cfg = config_mod.load(os_dir())
    except config_mod.ConfigError as exc:
        print(f"ошибка: {exc}", file=sys.stderr)
        return 2

    if args.list_plans:
        _print_plans(cfg, plans_overview(cfg))
        return 0

    try:
        plan_id = registry.normalize(args.plan or registry.default_plan_id(cfg))
        spec = registry.get(cfg, plan_id)
    except registry.PlanNotFound as exc:
        print(f"ошибка: {exc}", file=sys.stderr)
        return 2

    if args.list:
        steps = plan_steps(cfg, Path(spec.file))
        closed = set(registry.closed_steps(spec, cfg))
        for s in steps:
            mark = "ЗАКРЫТ" if s.id.upper() in closed else "открыт"
            print(f"{s.id:<7} {s.days:>4} дн  {mark:<8} {s.title}")
        if not steps:
            print(f"план {plan_id}: раздел шагов не распознан")
        return 0

    try:
        pkg = build_package(cfg, plan_id, args.step, args.brief)
    except registry.PlanNotFound as exc:
        print(f"ошибка: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(pkg, ensure_ascii=False, indent=2))
        return 0
    if pkg.get("next") is None:
        print(f"план {plan_id}: {pkg.get('note') or 'следующего шага нет'}")
        return 0
    print(render(pkg, args.brief))
    return 0


def os_dir() -> str:
    """Корень проекта: env PIPELINE_REPO (предпросмотр), иначе каталог над движком."""
    env = os.environ.get("PIPELINE_REPO")
    if env:
        return str(Path(env).resolve())
    return str(Path(__file__).resolve().parents[1])


if __name__ == "__main__":
    sys.exit(main())

# -*- coding: utf-8 -*-
"""Метрики цикла плана: план → разведка → правка плана → ревью плана.

Модуль только читает файлы проекта и ничего не пишет на диск. Движок он не
импортирует (инвариант `docs/CONTEXT.md`: плагин общается с движком через
subprocess), поэтому правила разбора повторены здесь текстом; у каждого правила
назван источник в движке, чтобы при его правке расхождение нашлось сразу.

R1. План — файл вида `NN-имя.md` в каталоге планов, в тексте которого есть
    заголовок уровня 2-6 с маркером журнала `markers.journal` и хотя бы одна
    строка таблицы этого раздела (не меньше `JOURNAL_COLUMNS` ячеек, вторая
    ячейка распознаётся `step_id_regex`). Источник: `registry.journal_section`,
    `registry.step_of_row`, `registry.discover`.
R2. Разведка в плане — заголовок уровня 2-6 со словом «разведка», который не
    является заголовком шага и не лежит внутри раздела шагов. Источник:
    `plan_cycle.research_section`.
R3. Артефакт разведки — файл `N-prior-art.md` в каталоге разведки, где N — номер
    плана. Источник: `gate.scout_artifact`.
R4. Вердикты кандидатов — по разделу артефакта, в заголовке которого есть
    «кандидат»; вердикт — последняя ячейка строки таблицы. Источник:
    `scout_report.split_sections`, `scout_report._is_separator`. Отнесение
    «не берём» к «своим» — правило постановки шага S8, а не движка (движок
    помечает такую ячейку находкой R3).
R5. Правки по разведке — отметка `Правки по разведке: N`, только у планов с
    артефактом. Источник: `plan_cycle.EDIT_MARK_RE`, `plan_cycle.edits_count`.
R6. Ревью плана — отчёт `plan-N.md` в каталоге отчётов, вердикт собирается по
    ВСЕМ строкам `ВЕРДИКТ:` (две разные дают `invalid`), привязка к версии плана —
    метка `plan_sha256:` и sha256 сырых байтов файла плана. Источник:
    `approval.verdict_at`, `approval.plan_sha_of`, `approval.sha256_file`,
    `approval.has_constitution`, `approval.review_status`.
R7. Круги ревью плана — записи `create_plan_review` из истории тиков
    (`state/history.jsonl`, по строке на действие). Источник:
    `gate.remember_action`.
R8. Нечитаемый файл попадает строкой в `errors`, его числа считаются нулём;
    отсутствие каталогов — не ошибка (механизм ещё не работал).
R9. `by_plan` — по записи на план, по возрастанию номера.
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

#: Маркер раздела-якоря разведки в заголовке (без учёта регистра).
RESEARCH_MARKER = "разведка"
#: Строка шапки плана действующего формата (`approval.CONSTITUTION_MARK`).
CONSTITUTION_MARK = "Конституция:"
#: Отметка оформленной правки плана: `Правки по разведке: 2` (`plan_cycle.EDIT_MARK_RE`).
EDIT_MARK_RE = re.compile(r"прав\w*\s+по\s+разведке\s*:\s*(\d+)", re.IGNORECASE)
#: Строка вердикта — один шаблон на весь движок (`approval.VERDICT_RE`).
VERDICT_RE = re.compile(r"^#{0,4}\s*ВЕРДИКТ\s*:\s*(APPROVE|REQUEST_CHANGES)\s*$",
                        re.IGNORECASE | re.MULTILINE)
#: Метка привязки вердикта к версии плана (`approval.PLAN_SHA_RE`).
PLAN_SHA_RE = re.compile(r"plan_sha256\s*[:=]\s*`?([0-9a-f]{64})", re.IGNORECASE)
#: Номер плана — начало имени файла (`registry._PLAN_FILE_RE`); группа 1 — номер.
PLAN_FILE_RE = re.compile(r"^(\d{1,2})-")
#: Заголовок шага (`plan_cycle.STEP_HEADING_RE`).
STEP_HEADING_RE = re.compile(r"^#{3,}\s+[A-Za-z]{1,4}\d+[A-Za-z]?(?:-\d+)?\b")
#: Умолчание `step_id_regex` — дословно `config.DEFAULTS` (группа `id` именованная).
DEFAULT_STEP_ID_REGEX = r"(?P<id>[A-Za-z]{1,4}\d+[A-Za-z]?(?:-\d+)?)"
#: Колонок в строке журнала (`registry.JOURNAL_COLUMNS`).
JOURNAL_COLUMNS = 5
#: Пути по умолчанию (`config.DEFAULTS["paths"]`).
DEFAULT_PATHS = {"plans": "docs/plans", "reviews": "docs/reviews",
                 "research": "docs/research", "state": ".pipeline/state"}
#: Маркер журнала по умолчанию (`config.DEFAULTS["markers"]["journal"]`).
DEFAULT_JOURNAL_MARKER = "журнал"
#: Действие истории, считающее круг ревью плана.
PLAN_REVIEW_ACTION = "create_plan_review"

_HEADING_RE = re.compile(r"^(?P<hashes>#{1,6})\s+(?P<title>\S.*?)\s*$")
_MAX_HASHES = 6


# ─────────────────────────────── разбор файлов ───────────────────────────────

def _settings(cfg: dict | None) -> tuple[dict, str, str]:
    """Пути, маркер журнала и `step_id_regex` из `pipeline.json` с умолчаниями."""
    raw = cfg if isinstance(cfg, dict) else {}
    paths = {key: str(value) for key, value in DEFAULT_PATHS.items()}
    for key, value in (raw.get("paths") or {}).items():
        if key in paths and str(value).strip():
            paths[key] = str(value)
    markers = raw.get("markers") or {}
    marker = str(markers.get("journal") or DEFAULT_JOURNAL_MARKER)
    regex = str(raw.get("step_id_regex") or DEFAULT_STEP_ID_REGEX)
    return paths, marker, regex


def _read(path: Path, errors: list[str], kind: str) -> str | None:
    """Текст файла; нечитаемый — строка в `errors` и `None` (R8)."""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        errors.append(f"{kind} {path.name}: {exc}")
        return None


def _step_regex(regex: str, errors: list[str]) -> re.Pattern:
    """Скомпилировать `step_id_regex`; битый — строка в `errors` и умолчание (R1)."""
    pattern = regex if regex.startswith("^") else "^" + regex
    try:
        compiled = re.compile(pattern)
    except re.error as exc:
        errors.append(f"step_id_regex не компилируется: {pattern}: {exc}")
        return re.compile("^" + DEFAULT_STEP_ID_REGEX)
    if "id" not in compiled.groupindex:
        errors.append(f"в step_id_regex нет именованной группы id: {pattern}")
        return re.compile("^" + DEFAULT_STEP_ID_REGEX)
    return compiled


def _headings(lines: list[str]) -> list[tuple[int, int, str]]:
    """(индекс строки, уровень, заголовок) по всем заголовкам файла."""
    found: list[tuple[int, int, str]] = []
    for idx, line in enumerate(lines):
        match = _HEADING_RE.match(line)
        if match:
            found.append((idx, len(match.group("hashes")), match.group("title")))
    return found


def _section_end(found: list[tuple[int, int, str]], pos: int, limit: int) -> int:
    """Первый заголовок того же или более высокого уровня — граница раздела."""
    level = found[pos][1]
    for idx in range(pos + 1, len(found)):
        if found[idx][1] <= level:
            return found[idx][0]
    return limit


def _cells(line: str) -> list[str]:
    """Ячейки строки таблицы: снять крайние `|`, обрезать каждую."""
    body = line.strip()
    if body.startswith("|"):
        body = body[1:]
    if body.endswith("|"):
        body = body[:-1]
    return [part.strip() for part in body.split("|")]


def _step_of_row(line: str, step_re: re.Pattern) -> str | None:
    """Идентификатор шага из второй ячейки строки журнала (`registry.step_of_row`)."""
    cells = _cells(line)
    if len(cells) < JOURNAL_COLUMNS:
        return None
    match = step_re.match(cells[1])
    return match.group("id").upper() if match else None


def _journal_section(lines: list[str], marker: str,
                     step_re: re.Pattern) -> bool:
    """Есть ли раздел-журнал с распознанной строкой шага (`registry.journal_section`)."""
    found = _headings(lines)
    for pos, (idx, level, title) in enumerate(found):
        if level < 2 or marker.lower() not in title.lower():
            continue
        end = _section_end(found, pos, len(lines))
        rows = [i for i in range(idx + 1, end) if lines[i].strip().startswith("|")]
        if any(_step_of_row(lines[i], step_re) for i in rows):
            return True
    return False


def _step_line(level: int, title: str) -> str:
    """Восстановить строку заголовка — `STEP_HEADING_RE` матчит её с `#`."""
    return "#" * level + " " + title


def _research_section(text: str) -> str | None:
    """Тело раздела разведки по якорю, иначе `None` (`plan_cycle.research_section`)."""
    lines = text.splitlines()
    found = _headings(lines)           # (индекс строки, уровень, заголовок)
    last_h2 = ""
    for idx, (lineno, level, title) in enumerate(found):
        candidate = (
            2 <= level <= _MAX_HASHES
            and RESEARCH_MARKER in title.lower()
            and not STEP_HEADING_RE.match(_step_line(level, title))
            and (level == 2 or "шаг" not in last_h2.lower())
        )
        if candidate:
            end = len(lines)
            for next_lineno, next_level, _ in found[idx + 1:]:
                if next_level <= level:
                    end = next_lineno
                    break
            return "\n".join(lines[lineno + 1:end]).strip()
        if level == 2:
            last_h2 = title
    return None


def _edits_count(text: str) -> int | None:
    """Число из отметки `Правки по разведке: N`, иначе `None`."""
    match = EDIT_MARK_RE.search(text)
    return int(match.group(1)) if match else None


def _split_sections(text: str) -> dict[str, str]:
    """`## <заголовок>` → тело раздела (`scout_report.split_sections`)."""
    sections: dict[str, str] = {}
    current: str | None = None
    buffer: list[str] = []
    for line in text.splitlines():
        if line.startswith("## "):
            if current is not None:
                sections[current] = "\n".join(buffer).strip()
            current = line[3:].strip()
            buffer = []
        elif current is not None:
            buffer.append(line)
    if current is not None:
        sections[current] = "\n".join(buffer).strip()
    return sections


def _is_separator(row: list[str]) -> bool:
    """Строка-разделитель таблицы: непустые ячейки только из `-` и `:`."""
    return bool(row) and all(row) and all(all(ch in "-:" for ch in cell) for cell in row)


def _artifact_counts(text: str, rel: str, errors: list[str]) -> dict[str, int]:
    """Вердикты кандидатов артефакта (R4): всего, без готового решения, с решением."""
    counts = {"candidates": 0, "own": 0, "with_solution": 0, "unknown": 0}
    body = None
    for title, content in _split_sections(text).items():
        if "кандидат" in title.lower():
            body = content
            break
    if body is None:
        errors.append(f"{rel}: нет раздела «Кандидаты»")
        return counts
    rows = [line for line in body.splitlines() if line.strip().startswith("|")]
    for row in rows[1:]:                       # первая строка таблицы — шапка
        cells = _cells(row)
        if _is_separator(cells):
            continue
        verdict = (cells[-1] if cells else "").lower()
        counts["candidates"] += 1
        # «не берём» проверяется ДО «берём»: оно содержит «берём» внутри.
        if "своё" in verdict or "не берём" in verdict:
            counts["own"] += 1
        elif "берём" in verdict or "форкаем" in verdict:
            counts["with_solution"] += 1
        else:
            counts["unknown"] += 1
    return counts


def _has_constitution(text: str) -> bool:
    """Строка `Конституция:` в шапке плана до первого `## ` (`approval.has_constitution`)."""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("## "):
            return False
        if stripped.startswith(CONSTITUTION_MARK):
            return True
    return False


def _verdict(report: str) -> str:
    """Вердикт отчёта: APPROVE / REQUEST_CHANGES / NONE / AMBIGUOUS (`approval.verdict_at`)."""
    found = [match.group(1).upper() for match in VERDICT_RE.finditer(report)]
    unique = sorted(set(found))
    if not found:
        return "NONE"
    return unique[0] if len(unique) == 1 else "AMBIGUOUS"


def _sha256(path: Path) -> str | None:
    """sha256 сырых байтов файла (`approval.sha256_file`); не читается — `None`."""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _review_state(plan_text: str, plan_file: Path, report: Path) -> str:
    """Состояние ревью плана (R6): одно из `skipped/missing/invalid/pending/unbound/stale/fresh`."""
    if not _has_constitution(plan_text):
        return "skipped"
    if not report.is_file():
        return "missing"
    try:
        text = report.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "invalid"
    verdict = _verdict(text)
    if verdict not in ("APPROVE", "REQUEST_CHANGES"):
        return "invalid"
    if verdict == "REQUEST_CHANGES":
        return "pending"
    match = PLAN_SHA_RE.search(text)
    if not match:
        return "unbound"
    file_sha = _sha256(plan_file)
    return "fresh" if file_sha and match.group(1).lower() == file_sha else "stale"


def _rounds(state_dir: Path, errors: list[str]) -> dict[str, int]:
    """Круги ревью плана по истории тиков (R7): номер плана → число записей."""
    rounds: dict[str, int] = {}
    path = state_dir / "history.jsonl"
    if not path.is_file():
        return rounds
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        errors.append(f"история {path.name}: {exc}")
        return rounds
    bad = 0
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            bad += 1
            continue
        if not isinstance(record, dict):
            bad += 1
            continue
        if record.get("action") != PLAN_REVIEW_ACTION:
            continue
        plan = str(record.get("plan"))
        rounds[plan] = rounds.get(plan, 0) + 1
    if bad:
        errors.append(f"история: пропущено строк {bad}")
    return rounds


def _rel(root: Path, path: Path) -> str:
    """Путь относительно корня проекта в форме `docs/...`."""
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return str(path)


# ─────────────────────────────── публичный API ───────────────────────────────

def cycle_metrics(root, cfg: dict | None = None) -> dict:
    """Все числа цикла по файлам проекта. Исключений наружу не бросает (R8).

    *root* — корень проекта (`Path` или строка), *cfg* — сырой словарь
    `pipeline.json` (`paths`, `markers`, `step_id_regex`); `None` — умолчания.
    """
    root = Path(root)
    paths, marker, regex = _settings(cfg)
    errors: list[str] = []
    step_re = _step_regex(regex, errors)
    plans_dir = root / paths["plans"]
    reviews_dir = root / paths["reviews"]
    research_dir = root / paths["research"]
    state_dir = root / paths["state"]

    plans: list[dict] = []
    if plans_dir.is_dir():
        for path in sorted(plans_dir.glob("*.md")):
            match = PLAN_FILE_RE.match(path.name)
            if not match:
                continue
            text = _read(path, errors, "план")
            if text is None:
                continue
            lines = text.splitlines()
            if not _journal_section(lines, marker, step_re):
                continue
            plans.append({"plan": str(int(match.group(1))), "file": path, "text": text})
    plans.sort(key=lambda item: int(item["plan"]))

    rounds = _rounds(state_dir, errors)
    artifacts: dict[Path, dict[str, int]] = {}
    by_plan: list[dict] = []
    plans_with_research = 0
    research_gap: list[str] = []
    edits_plans = 0
    edits_total = 0
    ready_total = 0
    unknown_total = 0
    for plan in plans:
        plan_id = plan["plan"]
        text = plan["text"]
        research = _research_section(text) is not None
        artifact = research_dir / f"{plan_id}-prior-art.md"
        artifact_text = None
        if artifact.is_file():
            artifact_text = _read(artifact, errors, "артефакт разведки")
        has_artifact = artifact_text is not None
        if research and has_artifact:
            plans_with_research += 1
        elif research:
            research_gap.append(plan_id)
        counts = {"candidates": 0, "own": 0, "with_solution": 0, "unknown": 0}
        if has_artifact:
            if artifact not in artifacts:
                artifacts[artifact] = _artifact_counts(
                    artifact_text or "", _rel(root, artifact), errors)
            counts = artifacts[artifact]
            ready_total += counts["with_solution"]
            unknown_total += counts["unknown"]
        edits = _edits_count(text) if has_artifact else None
        if edits is not None:
            edits_total += edits
            edits_plans += 1
        by_plan.append({
            "plan": plan_id,
            "research": research,
            "artifact": _rel(root, artifact) if has_artifact else "",
            "candidates": counts["candidates"],
            "own": counts["own"],
            "edits": edits,
            "review": _review_state(text, plan["file"], reviews_dir / f"plan-{plan_id}.md"),
            "rounds": rounds.get(plan_id, 0),
        })

    states = [item["review"] for item in by_plan]
    return {
        "plans_total": len(by_plan),
        "plans_with_research": plans_with_research,
        "research_gap": research_gap,
        "candidates_total": sum(item["candidates"] for item in by_plan),
        "candidates_with_solution": ready_total,
        "candidates_own": sum(item["own"] for item in by_plan),
        "candidates_unknown": unknown_total,
        "edits_total": edits_total,
        "edits_plans": edits_plans,
        "edits_per_plan": round(edits_total / edits_plans, 1) if edits_plans else None,
        "plan_reviews": states.count("fresh"),
        "plan_reviews_stale": states.count("stale"),
        "plan_reviews_unbound": states.count("unbound"),
        "plan_reviews_pending": states.count("pending"),
        "plan_reviews_skipped": states.count("skipped"),
        "plan_reviews_first": sum(1 for item in by_plan
                                  if item["review"] == "fresh" and item["rounds"] == 1),
        "by_plan": by_plan,
        "errors": errors,
    }


def _num(value) -> str:
    """Число метрики строкой без хвоста `.0` у целых."""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def cycle_tiles(metrics: dict) -> list[dict]:
    """Четыре плитки панели по числам цикла; значения — уже готовые строки."""
    total = int(metrics.get("plans_total") or 0)
    with_research = int(metrics.get("plans_with_research") or 0)
    gap = [str(item) for item in metrics.get("research_gap") or []]

    researched = [str(item.get("plan")) for item in metrics.get("by_plan") or []
                  if item.get("research") and item.get("artifact")]
    hint1 = ", ".join(researched) or "ни одного"
    if gap:
        hint1 += "; без артефакта: " + ", ".join(gap)

    candidates = int(metrics.get("candidates_total") or 0)
    own = int(metrics.get("candidates_own") or 0)
    ready = int(metrics.get("candidates_with_solution") or 0)
    unknown = int(metrics.get("candidates_unknown") or 0)

    per_plan = metrics.get("edits_per_plan")
    edits_total = int(metrics.get("edits_total") or 0)
    edits_plans = int(metrics.get("edits_plans") or 0)

    first = int(metrics.get("plan_reviews_first") or 0)
    stale = int(metrics.get("plan_reviews_stale") or 0)
    unbound = int(metrics.get("plan_reviews_unbound") or 0)
    pending = int(metrics.get("plan_reviews_pending") or 0)
    skipped = int(metrics.get("plan_reviews_skipped") or 0)
    fresh = int(metrics.get("plan_reviews") or 0)

    return [
        {"label": "планов с разведкой",
         "value": f"{with_research} из {total}",
         "hint": hint1},
        {"label": "вердиктов без готового решения",
         "value": f"{own} из {candidates}" if candidates else "—",
         "hint": f"готовых {ready}, своих {own}, нераспознанных {unknown}"},
        {"label": "правок по разведке на план",
         "value": "—" if per_plan is None else _num(per_plan),
         "hint": (f"{edits_total} правок в {edits_plans} планах" if edits_plans
                  else "отметок нет")},
        {"label": "ревью плана с первого раза",
         "value": f"{first} из {total}",
         "hint": (f"свежих вердиктов {fresh}, не привязанных к текущей версии "
                  f"{stale + unbound}, ждут {pending}, без ревью {skipped}")},
    ]


def render_cycle(metrics: dict) -> str:
    """Одна строка «Цикл: …» для `hermes pipeline status`."""
    total = int(metrics.get("plans_total") or 0)
    candidates = int(metrics.get("candidates_total") or 0)
    ready = int(metrics.get("candidates_with_solution") or 0)
    without = int(metrics.get("candidates_own") or 0) + int(
        metrics.get("candidates_unknown") or 0)
    per_plan = metrics.get("edits_per_plan")
    edits = "—" if per_plan is None else _num(per_plan)
    return (f"Цикл: планов {total}, с разведкой {int(metrics.get('plans_with_research') or 0)}, "
            f"кандидатов {candidates} (готовых {ready}, без готового {without}), "
            f"правок по разведке {edits}, "
            f"ревью плана {int(metrics.get('plan_reviews') or 0)} из {total} "
            f"(с первого раза {int(metrics.get('plan_reviews_first') or 0)})")


if __name__ == "__main__":  # ручная проба владельца: `python -m step_pipeline.stats [путь]`
    for stream in (sys.stdout, sys.stderr):
        reconf = getattr(stream, "reconfigure", None)
        if reconf is None:
            continue
        try:
            reconf(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(".")
    print(render_cycle(cycle_metrics(target)))
    raise SystemExit(0)

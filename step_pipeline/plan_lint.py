# -*- coding: utf-8 -*-
"""Линтер формата плана: неверный план виден до запуска, а не через сутки простоя.

Проверки (все — только чтение; ничего не пишется):
    1. имя файла — «номер-слаг.md» (только внутри каталога планов проекта);
    2. у файла есть раздел со словом-маркером шагов;
    3. раздел с маркером шагов ровно один (по уровню первого найденного);
    4. заголовки шагов вида «### S1. Название»;
    5. шаг не повторяется в разделе шагов;
    6. у шага есть пункты «Файл:», «Гейты:» (не пустые) и критерий «Готово =»;
    7. раздел-журнал со словом «журнал» есть;
    8. строки журнала — пять колонок и шаг во второй;
    9. набор шагов в разделе и в журнале совпадает;
    10. файл читается.

Правила разбора плана — ОСОЗНАННАЯ КОПИЯ правил движка (решение D5): плагин не
импортирует движок как модуль (`project.py`), поэтому регулярки скопированы из
`engine/registry.py:31`, `engine/next_step.py:58,62` и `engine/config.py:51`
символ в символ, а смысл проверок повторяет `next_step.py:92-122,179-197`,
`registry.py:101-126`. Расхождение с движком — дефект, а не настройка.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

try:
    from . import project as proj
except ImportError:  # pragma: no cover - отладка файлом
    import project as proj  # type: ignore

# Умолчания конфига (engine/config.py:48, :51).
_STEPS_MARKER = "шаг"
_JOURNAL_MARKER = "журнал"
_STEP_ID_REGEX = r"(?P<id>[A-Za-z]{1,4}\d+[A-Za-z]?(?:-\d+)?)"
# Строка журнала: `Дата | Шаг | Коммиты | Прогоны и доказательства | Статус`.
JOURNAL_COLUMNS = 5  # engine/registry.py:33
_HEADING_RE = re.compile(r"^(?P<hashes>#{1,6})\s+(?P<title>\S.*?)\s*$")  # registry.py:31
# Имя плана: номер, дефис, слаг, .md — иначе движок план не увидит (registry.py:30).
_NAME_RE = re.compile(r"^\d{1,2}-.+\.md$")
_PLAN_NUM_RE = re.compile(r"^(\d{1,2})-")
# Критерий готовности — engine/next_step.py:62.
_READY_RE = re.compile(r"готово(?:\s*\([^)]*\))?\s*[=:]\s*(?P<rest>.+)", re.IGNORECASE)
# Маркер пункта верхнего уровня — engine/next_step.py:58.
_BULLET_RE = re.compile(r"^(?:[-*+]|\d+\.)\s+(?P<text>.*)$")
# Пункт «Гейты: —» считается пустым.
_EMPTY_GATES = ("", "—", "-", "−")
_PLANS_DEFAULT = "docs/plans"
_STEP_HEADING_HINT = "заголовок шага не по формату «### S1. Название»"
# Раздел разведки плана действующего формата (§5a). Константа, а не ключ конфига (D6):
# линтер называет нарушение так же, как оно записано в формате, — ведение формата
# переопределило бы и §5a, и строки Л18/Л19.
_RESEARCH_MARKER = "разведка"
# Дискриминатор действующего формата: строка `Конституция:` в шапке плана (D1).
_CONSTITUTION_PREFIX = "конституция:"
# Признаки полей раздела — слова-маркеры из §5a (D2): линтер ищет слово, не оформление.
_RESEARCH_LINK = "prior-art"
_RESEARCH_CANDIDATES = "кандидат"
_RESEARCH_LICENSES = "лиценз"
_RESEARCH_VERDICT = ("решение", "вердикт")
# Тексты находок — так же в §11.1, колонка «Что проверяется».
_PROBLEM_NO_RESEARCH = ("нет раздела «Разведка» — план действующего формата обязан "
                        "содержать раздел со ссылкой на артефакт, кандидатами и решением")


@dataclass(frozen=True)
class Problem:
    """Одна находка: файл для показа, строка (1-based; 0 — весь файл), текст и шаг.

    *step* — шаг, к которому относится находка: по нему панель даёт переход к шагу.
    Пустая строка значит «ко всему файлу» (имя файла, регулярка, разделы).
    """

    file: str
    line: int
    text: str
    step: str = ""

    def __str__(self) -> str:
        return f"{self.file}:{self.line}: {self.text}" if self.line else f"{self.file}: {self.text}"


@dataclass(frozen=True)
class Settings:
    """Правила разбора плана: маркеры и регулярка шага из pipeline.json проекта."""

    steps_marker: str
    journal_marker: str
    step_regex: re.Pattern | None      # None — регулярка не компилируется


@dataclass(frozen=True)
class LintResult:
    """Что проверено и что найдено.

    `problems` — единственный блокирующий канал: на него смотрят `plan_accept.check_draft`,
    коды возврата `doctor` и `plan --check`. `warnings` — то, что человеку показать надо, а
    утверждение не блокирует (Л20): иначе legacy-планы 1-4 начали бы краснеть, а решение
    S1 — «старые планы не ломаются» — было бы нарушено.
    """

    files: list[Path]
    problems: list[Problem]
    warnings: list[Problem] = field(default_factory=list)


_INDEX_NAMES = {"index.md", "00-index.md"}


def _is_index_file(path: Path) -> bool:
    """Файл-реестр каталога планов (`00-INDEX.md`) — не план: шагов и журнала в нём нет.

    Линтер проверяет формат плана, а реестр им не является; иначе каждая проверка
    проекта добавляла бы заведомую находку «нет раздела со словом-маркером шагов».
    """
    name = path.name.lower()
    return name in _INDEX_NAMES or name.startswith("00-")


def _plans_dir(project: Path) -> Path:
    """Каталог планов проекта из `pipeline.json` (умолчание — `docs/plans`)."""
    cfg = proj.read_config(Path(project))
    rel = (cfg.get("paths") or {}).get("plans") or _PLANS_DEFAULT
    return Path(project) / str(rel)


def _raw_step_regex(project: Path) -> str:
    cfg = proj.read_config(Path(project))
    return str(cfg.get("step_id_regex") or _STEP_ID_REGEX)


def _compile_step_regex(raw: str) -> tuple[re.Pattern | None, str]:
    """Регулярка шага из конфига и причина, почему она не годится (как config.py:209)."""
    pattern = raw if raw.startswith("^") else "^" + raw
    try:
        compiled = re.compile(pattern)
    except re.error as exc:
        return None, f"step_id_regex не компилируется: {pattern}: {exc}"
    if "id" not in compiled.groupindex:
        return None, f"step_id_regex не компилируется: в нём нет именованной группы id: {pattern}"
    return compiled, ""


def settings(project: Path) -> Settings:
    """Маркеры и регулярка шага проекта; битая регулярка — `step_regex = None`."""
    project = Path(project)
    cfg = proj.read_config(project)
    markers = cfg.get("markers") or {}
    steps_marker = str(markers.get("steps") or _STEPS_MARKER).lower()
    journal_marker = str(markers.get("journal") or _JOURNAL_MARKER).lower()
    compiled, _reason = _compile_step_regex(_raw_step_regex(project))
    return Settings(steps_marker=steps_marker, journal_marker=journal_marker,
                    step_regex=compiled)


# ───────────────────────────── разбор текста плана ─────────────────────────────

def _headings(lines: list[str]) -> list[tuple[int, int, str]]:
    """(индекс строки, уровень, заголовок) — как registry.py:82-89."""
    found: list[tuple[int, int, str]] = []
    for i, line in enumerate(lines):
        m = _HEADING_RE.match(line)
        if m:
            found.append((i, len(m.group("hashes")), m.group("title")))
    return found


def _span_end(found: list[tuple[int, int, str]], pos: int, limit: int) -> int:
    """Первый заголовок того же или более высокого уровня — граница раздела."""
    level = found[pos][1]
    for idx in range(pos + 1, len(found)):
        if found[idx][1] <= level:
            return found[idx][0]
    return limit


def _content_end(found: list[tuple[int, int, str]], idx: int, level: int, limit: int) -> int:
    """Конец раздела, начатого заголовком по индексу строки *idx* уровня *level*."""
    for item_idx, item_level, _title in found:
        if item_idx > idx and item_level <= level:
            return item_idx
    return limit


def _section_blocks(lines: list[str]) -> list[tuple[str, str, str]]:
    """Пункты раздела: (метка, текст, полный текст) — копия next_step.py:92-122."""
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


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _shown(path: Path, project: Path) -> str:
    """Путь для показа: относительно проекта, если файл внутри него."""
    try:
        return path.resolve().relative_to(project.resolve()).as_posix()
    except (ValueError, OSError):
        return path.as_posix()


def _in_plans_dir(path: Path, plans_dir: Path) -> bool:
    try:
        return path.resolve().parent == plans_dir.resolve()
    except OSError:  # pragma: no cover - недостижимо на нормальном диске
        return path.parent == plans_dir


# ───────────────────────────── проверки ─────────────────────────────

def _bullet_problems(shown: str, sid: str, line: int,
                     body: list[str]) -> list[Problem]:
    """Пункты шага: файл, гейты (не пустые) и критерий готовности."""
    blocks = _section_blocks(body)
    files = [b for b in blocks if b[0].startswith("файл")]
    gates = [b for b in blocks if b[0].startswith(("тест", "гейт", "проверк"))]
    out: list[Problem] = []
    if not files:
        out.append(Problem(shown, line, f"шаг {sid}: нет пункта «Файл:»", sid))
    if not gates:
        out.append(Problem(shown, line, f"шаг {sid}: нет пункта «Гейты:»", sid))
    elif all(b[1].strip() in _EMPTY_GATES for b in gates):
        out.append(Problem(shown, line, f"шаг {sid}: гейты пустые", sid))
    ready = False
    for _label, _text, full in blocks:
        m = _READY_RE.search(full)
        if m and m.group("rest").strip():
            ready = True
            break
    if not ready:
        out.append(Problem(shown, line, f"шаг {sid}: нет критерия «Готово =»", sid))
    return out


def _journal_section(lines: list[str], found: list[tuple[int, int, str]],
                     marker: str) -> tuple[int, list[int]] | None:
    """Раздел-журнал: первый с маркером, в котором есть строки таблицы.

    Наивный выбор «первый заголовок с маркером» перехватил бы раздел шага
    «Проверка шага и журнала» — движок так не делает (registry.py:101-117).
    """
    candidates = [(idx, level) for idx, level, title in found
                  if level >= 2 and marker in title.lower()]
    chosen: tuple[int, list[int]] | None = None
    for idx, level in candidates:
        end = _content_end(found, idx, level, len(lines))
        rows = [i for i in range(idx + 1, end) if lines[i].strip().startswith("|")]
        if rows:
            chosen = (idx, rows)
            break
    if chosen is None and candidates:
        idx, level = candidates[0]
        end = _content_end(found, idx, level, len(lines))
        chosen = (idx, [i for i in range(idx + 1, end) if lines[i].strip().startswith("|")])
    return chosen


def _journal_problems(shown: str, lines: list[str], rows: list[int],
                      step_regex: re.Pattern) -> tuple[list[Problem], dict[str, int]]:
    """Строки журнала: колонки, шаг во второй колонке, повторы. Шаги — с номерами строк."""
    problems: list[Problem] = []
    steps: dict[str, int] = {}
    for i in rows:
        raw = lines[i].strip()
        cells = [c.strip() for c in raw.strip("|").split("|")]
        if cells and set("".join(cells)) <= set("-: "):
            continue
        if cells and cells[0].lower() in ("дата", "date"):
            continue
        # Шаг вычисляется до проверки колонок: находка «строка журнала» тоже знает шаг,
        # а панель даёт по нему переход. Строка без шага остаётся без него.
        sid = _row_step(cells, step_regex)
        if len(cells) != JOURNAL_COLUMNS:
            problems.append(Problem(shown, i + 1,
                                    f"строка журнала без {JOURNAL_COLUMNS} колонок "
                                    f"(нашлось {len(cells)})", sid or ""))
        if sid is None:
            problems.append(Problem(shown, i + 1, "строка журнала без шага во второй колонке"))
            continue
        if len(cells) < JOURNAL_COLUMNS:
            continue
        if sid in steps:
            problems.append(Problem(shown, i + 1, f"шаг {sid} повторяется в журнале", sid))
            continue
        steps[sid] = i + 1
    return problems, steps


def _row_step(cells: list[str], step_regex: re.Pattern) -> str | None:
    """Идентификатор шага из второй колонки (в верхнем регистре), как registry.py:120."""
    if len(cells) < 2:
        return None
    m = step_regex.match(cells[1])
    return m.group("id").upper() if m else None


def _constitution_line(lines: list[str],
                       found: list[tuple[int, int, str]]) -> int | None:
    """Строка `Конституция:` в шапке плана (1-based) или None — план legacy.

    Шапка — между `# `-заголовком и первым заголовком уровня 2+ (§5a). Заголовка
    уровня 1 нет — шапки в этом смысле тоже нет, дискриминатор не срабатывает:
    линтер терпит такой план, а не выдумывает ему формат.
    """
    start = next((idx for idx, level, _title in found if level == 1), None)
    if start is None:
        return None
    end = len(lines)
    for idx, level, _title in found:
        if idx > start and level >= 2:
            end = idx
            break
    for i in range(start + 1, end):
        if lines[i].strip().lower().startswith(_CONSTITUTION_PREFIX):
            return i + 1
    return None


def _steps_span(lines: list[str], found: list[tuple[int, int, str]],
                settings: Settings) -> tuple[int, int] | None:
    """Границы раздела шагов: (строка заголовка, конец раздела) или None (§3)."""
    for idx, level, title in found:
        if level >= 2 and settings.steps_marker in title.lower():
            return idx, _content_end(found, idx, level, len(lines))
    return None


def _research_section(lines: list[str], found: list[tuple[int, int, str]],
                      settings: Settings) -> tuple[int, int, int] | None:
    """Первый раздел `## Разведка`: (позиция среди заголовков, строка заголовка, уровень).

    Заголовок шага разделом не считается: `### S1. Разведка рынка` — шаг, а не раздел
    плана, и находки Л19 про его поля были бы ложью. Наивный выбор «первый заголовок
    с маркером» перехватил бы его — та же причина, по которой `_journal_section`
    смотрит ещё и на строки таблицы.
    """
    steps = _steps_span(lines, found, settings)
    for pos, (idx, level, title) in enumerate(found):
        if level < 2 or _RESEARCH_MARKER not in title.lower():
            continue
        if settings.step_regex is not None and settings.step_regex.match(title):
            continue  # заголовок начинается с идентификатора шага
        if steps is not None and steps[0] < idx < steps[1]:
            continue  # внутри раздела шагов — шаг, а не раздел плана
        return pos, idx, level
    return None


def _research_problems(shown: str, lines: list[str],
                       found: list[tuple[int, int, str]], settings: Settings) -> list[Problem]:
    """Л18 и Л19 (§5a) — только у плана действующего формата.

    Дискриминатор — строка `Конституция:` в шапке (D1): без неё план legacy, линтер
    молчит и про раздел, и про его поля, счётчики ведомых планов не растут (§5a).
    Поля — слова-маркеры §5a (D2): таблица, проза и списки равны, важны слова. Число
    находок — по числу отсутствующих полей: ревьюер читает, какого поля не хватает.
    """
    if _constitution_line(lines, found) is None:
        return []  # legacy-план: ни Л18, ни Л19 не срабатывают (§5a, D1)
    section = _research_section(lines, found, settings)
    if section is None:
        heading = next((idx for idx, level, _title in found if level == 1), 0)
        return [Problem(shown, heading + 1, _PROBLEM_NO_RESEARCH)]
    _pos, start, level = section
    end = _content_end(found, start, level, len(lines))
    text = _norm(" ".join(lines[start + 1:end])).lower()
    out: list[Problem] = []
    if _RESEARCH_LINK not in text:
        out.append(Problem(shown, start + 1,
                           "в разделе «Разведка» нет ссылки на артефакт"))
    if _RESEARCH_CANDIDATES not in text:
        out.append(Problem(shown, start + 1, "в разделе «Разведка» нет кандидатов"))
    if _RESEARCH_LICENSES not in text:
        out.append(Problem(shown, start + 1, "в разделе «Разведка» нет лицензий"))
    if not any(marker in text for marker in _RESEARCH_VERDICT):
        out.append(Problem(shown, start + 1, "в разделе «Разведка» нет решения/вердикта"))
    return out


def lint_file(path: Path, project: Path, settings: Settings) -> list[Problem]:
    """Все проверки одного файла плана; строки — 1-based, «весь файл» — 0."""
    path = Path(path)
    project = Path(project)
    shown = _shown(path, project)
    problems: list[Problem] = []
    plans_dir = _plans_dir(project)
    if _in_plans_dir(path, plans_dir) and not _NAME_RE.match(path.name):
        problems.append(Problem(shown, 0,
                                "имя файла не по формату «номер-слаг.md» — "
                                "такой план конвейер не увидит"))
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        problems.append(Problem(shown, 0, f"файл не читается: {exc}"))
        return problems

    step_regex = settings.step_regex
    if step_regex is None:  # pragma: no cover - отсекается в lint_project
        return problems
    found = _headings(lines)
    # Уровень плана (Л18, Л19): до разделов шагов и журнала — у плана действующего
    # формата разведка обязательна, даже если остальной формат сломан и там своя находка.
    problems.extend(_research_problems(shown, lines, found, settings))
    sections = [(idx, level) for idx, level, title in found
                if level >= 2 and settings.steps_marker in title.lower()]
    if not sections:
        problems.append(Problem(shown, 0,
                                f"нет раздела со словом-маркером шагов "
                                f"«{settings.steps_marker}»"))
        return problems

    start, base_level = sections[0]
    same_level = [item for item in sections if item[1] == base_level]
    if len(same_level) > 1:
        problems.append(Problem(shown, same_level[1][0] + 1,
                                f"разделов с маркером шагов «{settings.steps_marker}»: "
                                f"{len(same_level)} — должен быть один"))

    end = _content_end(found, start, base_level, len(lines))
    inside = [item for item in found if start < item[0] < end]

    steps: dict[str, int] = {}
    for pos, (idx, _level, title) in enumerate(inside):
        m = step_regex.match(title)
        rest = title[m.end():].strip() if m else ""
        step_title = rest[1:].strip() if rest.startswith(".") else ""
        if not m or not step_title:
            problems.append(Problem(shown, idx + 1, _STEP_HEADING_HINT))
            continue
        sid = m.group("id").upper()
        body_end = _span_end(inside, pos, end)
        problems.extend(_bullet_problems(shown, sid, idx + 1, lines[idx + 1:body_end]))
        if sid in steps:
            problems.append(Problem(shown, idx + 1, f"шаг {sid} повторяется в разделе шагов", sid))
            continue
        steps[sid] = idx + 1

    journal = _journal_section(lines, found, settings.journal_marker)
    if journal is None:
        problems.append(Problem(shown, 0,
                                f"нет раздела-журнала со словом «{settings.journal_marker}»"))
        return problems
    journal_problems, journal_steps = _journal_problems(shown, lines, journal[1], step_regex)
    problems.extend(journal_problems)
    if not journal_steps:
        problems.append(Problem(shown, 0, "в журнале нет ни одной строки шага"))

    for sid, line in steps.items():
        if sid not in journal_steps:
            problems.append(Problem(shown, line, f"шаг {sid} есть только в разделе шагов", sid))
    for sid, line in journal_steps.items():
        if sid not in steps:
            problems.append(Problem(shown, line, f"шаг {sid} есть только в журнале", sid))
    return problems


# ───────────────────────────── точка входа ─────────────────────────────

# Л20 — единственная находка, которая не попадает в `problems`, а идёт в `warnings`:
# план без строки `Конституция:` в шапке — legacy, а планы 1-4 заведены до конституции
# (решение D1 шага S1: старые планы не ломаются и утверждение не блокируют).
_CONSTITUTION_WARNING = ("нет строки «Конституция: docs/CONSTITUTION.md» в шапке "
                         "плана — план не сослался на конституцию проекта")


def lint_warnings(path: Path, project: Path, settings: Settings) -> list[Problem]:
    """Предупреждения файла плана: не блокируют, но видны человеку (Л20).

    Файл не читается — предупреждения нет: это уже ошибка Л2, второй находки об одном
    и том же не нужно. Строка находки — заголовок плана (`# …`, 1-based); заголовка
    уровня 1 нет — строка 1. `step` пустой: находка уровня файла, как у Л18.
    """
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    lines = text.splitlines()
    found = _headings(lines)
    if _constitution_line(lines, found) is not None:
        return []
    heading = next((idx for idx, level, _title in found if level == 1), None)
    return [Problem(_shown(Path(path), project), 1 if heading is None else heading + 1,
                    _CONSTITUTION_WARNING)]


def _resolve_plan(project: Path, plans_dir: Path, plan: str) -> Path | None:
    """Номер (`2` → `docs/plans/2-*.md`) или путь (абсолютный/относительно проекта)."""
    as_path = Path(plan).expanduser()
    if as_path.is_absolute() and as_path.is_file():
        return as_path
    local = project / as_path
    if local.is_file():
        return local
    if plan.isdigit() and plans_dir.is_dir():
        want = str(int(plan))
        for candidate in sorted(plans_dir.glob("*.md")):
            m = _PLAN_NUM_RE.match(candidate.name)
            if m and str(int(m.group(1))) == want:
                return candidate
    return None


def lint_project(project: Path, plan: str = "") -> LintResult:
    """Проверить один план (*plan*) или все планы каталога планов проекта."""
    project = Path(project)
    parsed = settings(project)
    if parsed.step_regex is None:
        _compiled, reason = _compile_step_regex(_raw_step_regex(project))
        return LintResult(files=[], problems=[Problem("pipeline.json", 0, reason)])
    plans_dir = _plans_dir(project)
    target = str(plan or "").strip()
    if target:
        path = _resolve_plan(project, plans_dir, target)
        if path is None:
            return LintResult(
                files=[], problems=[Problem(
                    target, 0,
                    f"план «{target}» не найден: ни в каталог планов, ни файлом")])
        paths = [path]
    else:
        if not plans_dir.is_dir():
            return LintResult(files=[], problems=[])
        paths = [p for p in sorted(plans_dir.glob("*.md")) if not _is_index_file(p)]
    files: list[Path] = []
    problems: list[Problem] = []
    warnings: list[Problem] = []
    for path in paths:
        files.append(path)
        problems.extend(sorted(lint_file(path, project, parsed), key=lambda p: p.line))
        warnings.extend(lint_warnings(path, project, parsed))
    return LintResult(files=files, problems=problems, warnings=warnings)

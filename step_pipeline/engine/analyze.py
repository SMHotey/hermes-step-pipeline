# -*- coding: utf-8 -*-
"""Анализ согласованности плана, артефакта разведки и дерева проекта.

Зачем: до первой строки кода должно быть видно, стоит ли делать план и не противоречат
ли друг другу его артефакты. Линтер плана проверяет форму пунктов, гейт артефакта
разведки — таблицу кандидатов, а расхождений между планом и деревом файлов не видит
никто: гейт-пожелание «прогнать тесты» (пункт есть, а запускать нечего), файл, который
шаг де-факто трогает вне своего контракта, раздел «Разведка» без исхода и ссылки на
документы, которых нет.

Проверки (коды латиницей — рядом с Л1-Л20 линтера и P1-P3 `plan_cycle`, чтобы код
называл инструмент):

    A1  шаг без гейтов: у шага нет ни одной команды в обратных кавычках ни в одном
        пункте гейтов — доказать «Готово» нечем
    A2  файл вне списка шага: путь в обратных кавычках вне пункта «Файл:» есть в
        дереве и не объявлен ни в одном пункте «Файл:» ни одного шага
    A3  разведка без вердикта: раздел «Разведка» не называет исход строкой
        «Решение: …», либо исход плана расходится с исходом артефакта
    A4  нет раздела «Разведка» у плана действующего формата
    A5  ссылка на несуществующий файл: тот же скан, что у A2, но файла в дереве нет и
        ни один шаг его не объявляет; A2 и A5 взаимоисключающие по построению

Запуск:

    python .pipeline/analyze.py docs/plans/N-x.md
    python .pipeline/analyze.py docs/plans/N-x.md --root .

Выход: 0 — расхождений нет, 1 — есть находки, 2 — плана или конфигурации нет (причина
печатается в stderr).

Разбор — переиспользованием движка, второго парсера плана нет: контракт шага (файлы,
гейты) — `next_step.plan_steps`, тела шагов и номера строк — `registry.headings` +
`registry.section_end`, границы раздела шагов — `next_step.steps_section`, раздел
разведки — `plan_cycle.research_section`, словарь исходов и артефакт — `scout_report`,
формат плана — `approval.has_constitution`. Импорты — плоские имена соседей, как в
`gate.py`: модуль копируется в `.pipeline/` проекта и запускается как скрипт, а
обратного импорта стража нет — это цикл.

Две функции разбора читает и страж: `plan_decision` — исход идеи из раздела «Разведка»
(`kill` и `needs-clarification` меняют решение, отсутствие исхода — нет) и
`open_questions` — открытые ответы в `docs/research/N-questions.md`.
"""
from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

# Плоский импорт: скрипт запускают файлом (`python .pipeline/analyze.py`), пакетом — реже.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import approval  # noqa: E402 - дискриминатор формата плана: строка «Конституция:»
import config as config_mod  # noqa: E402 - модуль рядом
import next_step  # noqa: E402 - контракт шага: файлы, гейты, раздел шагов
import plan_cycle  # noqa: E402 - раздел-якорь разведки (проверки P1-P3)
import registry  # noqa: E402 - заголовки, границы разделов, журнал
import scout_report  # noqa: E402 - словарь исходов и гейт артефакта (R1-R5)

#: Шапка таблицы вопросов — единственный формат, который разбирает `open_questions`.
#: Тест сверяет её с шапкой `docs/research/TEMPLATE-questions.md`, как `COLUMNS` у
#: `scout_report`: шаблон, разбор и счёт открытых ответов — один контракт.
QUESTIONS_COLUMNS = ("№", "Вопрос", "Варианты", "Рекомендация", "Ответ")
#: Каталог артефактов по умолчанию — тот же, что у `gate.scout_research_dir` (D2 шага S2).
RESEARCH_DIR_DEFAULT = "docs/research"
ARTIFACT_SUFFIX = "-prior-art.md"
QUESTIONS_SUFFIX = "-questions.md"
#: Маркер раздела таблицы вопросов — тот же приём, что `scout_report.CANDIDATES_MARK`:
#: ищется слово, а не оформление.
QUESTIONS_MARK = "вопрос"
#: Строка исхода: слово берётся целиком и сверяется со словарём `scout_report.DECISIONS`.
DECISION_RE = re.compile(r"Решение\s*:\s*([A-Za-z][A-Za-z-]*)")
#: Признаки ссылки: адрес — не файл, A2 и A5 его пропускают.
LINK_MARKERS = ("://", "http", "www", "mailto")
#: Хвост ссылки, которого нет в имени файла: `#ANCHOR`, `:196`, `:44-60`.
ANCHOR_RE = re.compile(r"[#:].*$")


@dataclass(frozen=True)
class Finding:
    """Одна находка: код A1-A5, строка файла плана (1-based), шаг и текст."""

    code: str        # "A1".."A5"
    line: int        # строка файла плана, 1-based
    step: str        # "S1"; "" для плановых находок A3/A4
    message: str

    def render(self, plan_rel: str) -> str:
        """Строка вывода: «A2 docs/plans/1-demo.md:31 S1: текст» (D2)."""
        where = f" {self.step}" if self.step else ""
        return f"{self.code} {plan_rel}:{self.line}{where}: {self.message}"


# ───────────────────────────── разбор плана ─────────────────────────────

def _clean_path(token: str) -> str:
    """Имя файла из ссылки: снять якорь и номер строки (`gate.py:715-727`, `x.md#A`)."""
    return ANCHOR_RE.sub("", token.strip()).rstrip(".,;:)").strip()


def _is_link(token: str) -> bool:
    """Токен — адрес, а не путь в дереве (A2, A5 его не проверяют)."""
    lowered = token.lower()
    return any(marker in lowered for marker in LINK_MARKERS)


def _path_tokens(line: str) -> list[str]:
    """Пути строки: токены в обратных кавычках, похожие на путь, без ссылок.

    `next_step._backticks` и `next_step._looks_like_path` — приватные предикаты движка,
    и берутся намеренно (D3 §4.1): критерий «путь» обязан совпадать с тем, по которому
    `next_step` собирает `Step.files` для карточки, иначе A2 разъедется с контрактом шага.
    """
    out: list[str] = []
    for token in next_step._backticks(line):
        if _is_link(token) or not next_step._looks_like_path(token):
            continue
        value = _clean_path(token)
        if value:
            out.append(value)
    return out


def _step_spans(cfg: config_mod.Config, lines: list[str]) -> list[tuple[str, int, int]]:
    """Шаги плана: (идентификатор, индекс строки заголовка от нуля, конец тела).

    Разбор тот же, что в `next_step.plan_steps`: раздел шагов, заголовки внутри него,
    `step_id_regex` и обязательная точка после идентификатора. Отдельным проходом —
    потому что находке нужен номер строки, а `next_step.Step` его не несёт.
    """
    span = next_step.steps_section(cfg, lines)
    if not span:
        return []
    start, end = span
    inside = [h for h in registry.headings(lines) if start < h[0] < end]
    out: list[tuple[str, int, int]] = []
    for pos, (idx, _level, title) in enumerate(inside):
        m = cfg.step_regex.match(title)
        if not m:
            continue
        rest = title[m.end():].strip()
        if not rest.startswith(".") or not rest[1:].strip():
            continue
        out.append((m.group("id").upper(), idx, registry.section_end(inside, pos, end)))
    return out


def _bullet_label(line: str) -> str | None:
    """Метка пункта строки; `None` — строка не пункт (продолжение предыдущего).

    Оба условия метки — из `next_step.section_blocks`: префикс до `:` не длиннее 40
    символов и без обратной кавычки в нём. Без второго условия пункт вида
    «- `Файл:` `app/x.py`» перестал бы опознаваться как «Файл:», и объявленный файл
    попал бы в A2.
    """
    m = next_step._BULLET_RE.match(line)
    if not m:
        return None
    head = m.group("text").strip()
    if ":" not in head:
        return ""
    candidate = head.split(":", 1)[0]
    if 0 < len(candidate) <= 40 and "`" not in candidate:
        return candidate.strip().lower()
    return ""


def _declared(steps: list[next_step.Step]) -> set[str]:
    """Все файлы, объявленные в пунктах «Файл:» всех шагов плана."""
    return {_clean_path(path) for step in steps for path in step.files if path}


def _exists(value: str, root: Path) -> bool:
    """Путь есть в дереве; относительный проверяется от корня проекта."""
    path = Path(value)
    if not path.is_absolute():
        path = root / path
    return path.exists()


def _research_heading(lines: list[str]) -> int:
    """Номер строки (1-based) заголовка раздела «Разведка» — условия `plan_cycle` (D5).

    Тело раздела `plan_cycle.research_section` отдаёт без номеров строк, поэтому
    заголовок ищется тем же набором условий: уровень 2-6, маркер, не заголовок шага и
    для уровня выше 2 — вне раздела шагов.
    """
    last_h2 = ""  # последний заголовок уровня 2 — граница раздела шагов
    for idx, level, title in registry.headings(lines):
        step_line = "#" * level + " " + title          # форма строки для STEP_HEADING_RE
        if (2 <= level <= 6
                and plan_cycle.RESEARCH_MARKER in title.lower()
                and not plan_cycle.STEP_HEADING_RE.match(step_line)
                and (level == 2 or "шаг" not in last_h2.lower())):
            return idx + 1
        if level == 2:
            last_h2 = title
    return 1


# ───────────────────────────── исход, вопросы, ретроспектива ─────────────────────────────

def plan_decision(text: str) -> tuple[str, str]:
    """Исход идеи из раздела «Разведка» плана: (исход, пояснение); («», «») — нет исхода.

    Разбирается только тело раздела (P1 `plan_cycle`): строка `Решение: <слово>`, слово
    сверяется с `scout_report.DECISIONS`. Несколько строк — берётся последняя. Слово вне
    словаря исхода не даёт: исход действует только будучи явным (D7).
    """
    body = plan_cycle.research_section(text)
    if not body:
        return "", ""
    decision, note = "", ""
    for line in body.splitlines():
        m = DECISION_RE.search(line)
        if not m:
            continue
        value = m.group(1).lower()
        if value not in scout_report.DECISIONS:
            continue
        decision = value
        note = line[m.end():].lstrip(scout_report.NOTE_LEADERS).strip()
    return decision, note


def open_questions(path: Path) -> tuple[int, int] | None:
    """Открытые и все вопросы артефакта уточнений: (открытых, всего); None — файла нет.

    Вопрос — строка таблицы раздела «Вопросы» не короче пяти ячеек; шапка и строка-
    разделитель не считаются (`scout_report.table_rows` отбрасывает обе), открытая —
    пустая последняя ячейка или ячейка из набора пустоты `scout_report.EMPTY_CELLS`.
    """
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    body = scout_report.find_section(scout_report.split_sections(text), QUESTIONS_MARK)
    if body is None:
        return 0, 0
    _header, rows = scout_report.table_rows(body)
    opened = total = 0
    for row in rows:
        if len(row) < len(QUESTIONS_COLUMNS):
            continue
        total += 1
        if row[-1] in scout_report.EMPTY_CELLS:
            opened += 1
    return opened, total


def _plan_title(lines: list[str]) -> str:
    """Название плана — `# `-заголовок без номера («Демо-план конвейера»)."""
    for _idx, level, title in registry.headings(lines):
        if level == 1:
            return re.sub(r"^\d+\.\s*", "", title).strip() or title
    return ""


def _closed_rows(lines: list[str]) -> list[dict[str, str]]:
    """Строки журнала со статусом «ЗАКРЫТ»: дата, шаг, коммит, статус."""
    found = registry.headings(lines)
    for pos, (idx, level, title) in enumerate(found):
        if level < 2 or "журнал" not in title.lower():
            continue
        end = registry.section_end(found, pos, len(lines))
        rows: list[dict[str, str]] = []
        for line in lines[idx + 1:end]:
            if not line.strip().startswith("|"):
                continue
            cells = scout_report.cells(line)
            if len(cells) < registry.JOURNAL_COLUMNS:
                continue
            if cells[-1].upper().startswith("ЗАКРЫТ"):
                rows.append({"date": cells[0], "step": cells[1],
                             "commit": cells[2], "status": cells[-1]})
        return rows
    return []


def retro_text(text: str, plan_rel: str, plan_id: str, today: str) -> str:
    """Черновик ретроспективы: факты из журнала плана и три раздела для владельца (D8).

    Чистая функция: страж пишет её результат один раз, повторный тик файл не трогает.
    """
    lines = text.splitlines()
    title = _plan_title(lines) or plan_rel
    rows = _closed_rows(lines)
    out = [f"# Ретроспектива плана {plan_id} — {title}", "",
           f"Дата: {today}. План: {plan_rel}. Шагов закрыто: {len(rows)}.", ""]
    if rows:
        out += ["| Шаг | Дата | Коммит | Статус |", "|---|---|---|---|"]
        out += [f"| {row['step']} | {row['date']} | {row['commit']} | {row['status']} |"
                for row in rows]
        out.append("")
    out += ["## Что вышло", "",
            "Раздел заполняет владелец или ревьюер: что получилось и что подтвердили гейты.", ""]
    out += ["## Что мешало", "",
            "Раздел заполняет владелец или ревьюер: где теряли время и почему.", ""]
    out += ["## Что меняем в конвенциях", "",
            "Раздел заполняет владелец или ревьюер: правила, которые правятся до "
            "следующего плана.", ""]
    return "\n".join(out)


# ───────────────────────────── проверки A1-A5 ─────────────────────────────

def _gates_findings(steps: list[next_step.Step],
                    spans: list[tuple[str, int, int]]) -> list[Finding]:
    """A1: гейт, который можно запустить, — команда в обратных кавычках (D12)."""
    heading = {sid: idx + 1 for sid, idx, _end in spans}
    out: list[Finding] = []
    for step in steps:
        if any(next_step._backticks(gate) for gate in step.gates):
            continue
        out.append(Finding("A1", heading.get(step.id, 1), step.id,
                           f"шаг {step.id} без гейтов — нечем доказать «Готово»"))
    return out


def _path_findings(lines: list[str], spans: list[tuple[str, int, int]],
                   declared: set[str], root: Path) -> list[Finding]:
    """A2 и A5: пути в теле шага вне пункта «Файл:» (D3, D4).

    Сначала проверка существования, потом объявленности — поэтому классы не пересекаются:
    объявленный файл на существование не проверяется (его могут создавать), а
    необъявленный существующий попадает в A2, необъявленный отсутствующий — в A5.
    """
    out: list[Finding] = []
    for sid, heading, body_end in spans:
        label = ""                              # метка текущего пункта
        for idx in range(heading + 1, body_end):
            line = lines[idx]
            mark = _bullet_label(line)
            if mark is not None:
                label = mark
            if label.startswith("файл"):
                continue
            for value in _path_tokens(line):
                if value in declared:
                    continue
                if _exists(value, root):
                    out.append(Finding(
                        "A2", idx + 1, sid,
                        f"файл `{value}` упомянут в шаге {sid}, но не объявлен ни в одном "
                        f"пункте «Файл:»"))
                else:
                    out.append(Finding(
                        "A5", idx + 1, sid,
                        f"ссылка на несуществующий файл: `{value}` в шаге {sid} — файла нет "
                        f"в дереве и ни один шаг его не объявляет"))
    return out


def _research_dir(cfg: config_mod.Config, root: Path) -> Path:
    """Каталог артефактов разведки: ключ `paths.research`, умолчание `docs/research`.

    Повторяет чтение `gate.scout_research_dir` (в `config.py` этого ключа нет): конфиг
    читается сырым словарём, относительный путь разрешается от корня проекта.
    """
    raw = getattr(cfg, "state", None) or {}
    paths = raw.get("paths") if isinstance(raw, dict) else None
    value = paths.get("research") if isinstance(paths, dict) else None
    text = value.strip() if isinstance(value, str) else ""
    path = Path(text or RESEARCH_DIR_DEFAULT)
    return path if path.is_absolute() else Path(root) / path


def plan_number(cfg: config_mod.Config, plan_file: Path) -> str:
    """Номер плана: из реестра по пути, иначе из имени файла (регулярка реестра)."""
    try:
        target = Path(plan_file).resolve()
    except OSError:
        target = Path(plan_file)
    for spec in registry.discover(cfg):
        try:
            if Path(spec.file).resolve() == target:
                return spec.id
        except OSError:
            continue
    m = registry._PLAN_FILE_RE.match(Path(plan_file).name)
    if not m:
        return ""
    try:
        return registry.normalize(m.group("id"))
    except registry.PlanNotFound:
        return ""


def _research_findings(cfg: config_mod.Config, text: str, lines: list[str],
                       plan_file: Path, root: Path, plan_id: str) -> list[Finding]:
    """A3 и A4: раздел «Разведка» и его исход (D5, D6)."""
    body = plan_cycle.research_section(text)
    if body is None:
        if not approval.has_constitution(plan_file):
            return []                       # legacy-план: раздела от него не требуют
        return [Finding("A4", 1, "",
                        "нет раздела «Разведка» — план действующего формата обязан "
                        "содержать раздел со ссылкой на артефакт")]
    line = _research_heading(lines)
    decision, _note = plan_decision(text)
    if not decision:
        return [Finding("A3", line, "",
                        "раздел «Разведка» не называет исход: нужна строка «Решение: "
                        "go | needs-clarification | kill»")]
    if not plan_id:
        return []                           # номер не определился — артефакт не сверяется
    artifact = _research_dir(cfg, root) / f"{plan_id}{ARTIFACT_SUFFIX}"
    if not artifact.is_file():
        return []                           # отсутствие артефакта решает страж, не анализатор
    try:
        report = scout_report.check_file(artifact)
    except OSError:
        return []
    if not report.decision or report.decision == decision:
        return []
    try:
        artifact_rel = artifact.resolve().relative_to(Path(root).resolve()).as_posix()
    except (OSError, ValueError):
        artifact_rel = artifact.name
    return [Finding("A3", line, "",
                    f"исход плана «{decision}» не совпадает с исходом артефакта "
                    f"{artifact_rel} «{report.decision}»")]


def analyze(plan_file: Path, root: Path) -> list[Finding]:
    """Находки A1-A5 по плану и дереву проекта: порядок — строка, затем код.

    `root` — корень проекта с `pipeline.json`: из него берётся конфигурация (маркеры,
    регулярка шагов, каталог разведки) и относительно него проверяются пути.
    """
    root = Path(root)
    return _findings(config_mod.load(root), Path(plan_file), root)


def _findings(cfg: config_mod.Config, plan_file: Path, root: Path) -> list[Finding]:
    """Проверки на уже загруженном конфиге: CLI читает его сам и второй раз не читает."""
    text = plan_file.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    steps = next_step.plan_steps(cfg, plan_file)
    spans = _step_spans(cfg, lines)
    declared = _declared(steps)
    findings: list[Finding] = []
    findings += _gates_findings(steps, spans)
    findings += _path_findings(lines, spans, declared, root)
    findings += _research_findings(cfg, text, lines, plan_file, root, plan_number(cfg, plan_file))
    return sorted(findings, key=lambda item: (item.line, item.code))


# ───────────────────────────── точка входа ─────────────────────────────

def _setup_stdio() -> None:
    """UTF-8 в stdout/stderr: на Windows-консолях cp1251 русский текст падает."""
    for stream in (sys.stdout, sys.stderr):
        reconf = getattr(stream, "reconfigure", None)
        if reconf is None:
            continue
        try:
            reconf(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass


def main(argv: list[str] | None = None) -> int:
    """CLI анализатора: путь плана и необязательный `--root` (D2)."""
    _setup_stdio()
    parser = argparse.ArgumentParser(
        description="Согласованность плана, артефакта разведки и дерева проекта.")
    parser.add_argument("plan", help="файл плана, например docs/plans/5-platforma.md")
    parser.add_argument("--root", default=".", help="корень проекта с pipeline.json")
    args = parser.parse_args(argv)

    root = Path(args.root).resolve()
    plan = Path(args.plan)
    if not plan.is_absolute():
        plan = root / plan
    if not plan.is_file():
        print(f"нет файла плана: {args.plan}", file=sys.stderr)
        return 2
    try:
        cfg = config_mod.load(root)
    except config_mod.ConfigError as exc:
        print(f"проект не настроен: {exc}", file=sys.stderr)
        return 2
    try:
        findings = _findings(cfg, plan, root)
    except OSError as exc:
        print(f"план не читается: {exc}", file=sys.stderr)
        return 2

    plan_rel = cfg.rel(plan)
    for finding in findings:
        print(finding.render(plan_rel))
    if findings:
        print(f"ИТОГ: FAIL — находок {len(findings)}")
        return 1
    print("ИТОГ: PASS — расхождений нет")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

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
    python .pipeline/next_step.py --package      # пакет шага: записать и напечатать путь и числа
    python .pipeline/next_step.py --step S3      # принудительно конкретный шаг
    python .pipeline/next_step.py --list         # все шаги плана и статус
    python .pipeline/next_step.py --list-plans   # все планы: следующий шаг, закрыто

Скрипт репозиторий не меняет: единственная запись — пакет шага (флаг `--package` и сборка карточки
шага) в каталог состояния `<state_dir>/package-plan-<N>-<ШАГ>.md` (план 9, S5).
"""
from __future__ import annotations

import argparse
import io
import json
import os
import re
import subprocess
import sys
from dataclasses import asdict, dataclass, field
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
    runs: list["StepRun"] = field(default_factory=list)


# ANCHOR:структура_прогона — стабильная ссылка для спеки и ревью
@dataclass(frozen=True)
class StepRun:
    """Прогон шага как данные: команда, вид ожидания, значение, таймаут, пометка slow.

    Поле `runs` собирается из пунктов «Прогон:» (`_parse_run`, формат — §5 формата
    плана). Исполняет прогоны страж, а не этот модуль: здесь только разбор.
    """

    command: str
    expect: str          # exit | contains | regex
    value: str           # пустая строка у exit, иначе код/подстрока/выражение
    timeout: int | None = None   # None — умолчание исполнения
    slow: bool = False


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
                     "риск", "авто", "прогон")


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _risk_of(text: str) -> str:
    """Риск шага из значения пункта «Риск: …»: метка, опечатка или описание.

    Класс риска — первое слово из словаря (`low`/`normal`/`high`), сколько бы слов ни шло
    дальше: «high — данные и деньги» остаётся `high`. Ровно одно слово вне словаря
    (`хай`, `высокий`, `HIGH!`) — опечатка, она остаётся `high`: ошибка в плане не должна
    делать опасный шаг безопасным (П25). Два слова и больше, первое вне словаря, — описание
    риска прозой, класс `normal`: метку автор не ставил, а читать описание как метку значило
    бы выдавать «доки описывают ключи…» за решение «шаг опасен» (линтер плана предупреждает
    об этом, Л22). Пустое значение — прежний `high`. Отсутствие пункта по-прежнему `normal`:
    его разбирает `_step_from_section`, не эта функция.
    """
    tokens = _norm(text).split()
    if not tokens:
        return "high"
    token = tokens[0].lower().strip(".,;:()«»\"'")
    if token in ("low", "normal", "high"):
        return token
    return "normal" if len(tokens) > 1 else "high"


def _yes(text: str) -> bool:
    """Пункт «Авто: да» — признак зафиксированной договорённости, не отмена риска."""
    token = (_norm(text).split() or [""])[0].lower().strip(".,;:()«»\"'")
    return token in ("да", "yes", "true", "1")


# ───────────────────── пункт «Прогон»: прогон шага как данные ─────────────────────
# Формат — одна строка (§5 формата плана):
#     `- Прогон: `<команда>` → <ожидание>[; таймаут N сек][; slow]`
# Команда — первый токен в обратных кавычках (только она исполняется), дальше стрелка,
# затем ожидание одного из трёх видов: `exit 0`, `содержит "текст"` (или «ёлочки»),
# `regex "выражение"` (двойные, «ёлочки» или обратные кавычки). Хвосты отделяются `;`
# или `,` и идут после ожидания. Исполняет прогон страж, а не этот модуль.
_RUN_TICK_RE = re.compile(r"`([^`]+)`")
_RUN_ARROWS = ("→", "->")
_RUN_EXIT_RE = re.compile(r"exit\s*:?\s*(?P<code>-?\d+)", re.IGNORECASE)
_RUN_CONTAINS_RE = re.compile(r"содержит\s*:?\s*", re.IGNORECASE)
_RUN_REGEX_RE = re.compile(r"regex\s*:?\s*", re.IGNORECASE)
_RUN_TIMEOUT_RE = re.compile(r"таймаут\s*:?\s*(?P<seconds>\d+)", re.IGNORECASE)
_RUN_SLOW_RE = re.compile(r"\bslow\b")
#: Пары кавычек ожидания: двойные, «ёлочки» и (только для regex) обратные.
_RUN_QUOTES = {'"': '"', "«": "»", "`": "`"}


def _quoted_value(text: str, *, tick: bool = False) -> tuple[str, str] | None:
    """Фрагмент в кавычках с начала строки: (значение внутри, остаток после кавычек)."""
    text = text.lstrip()
    if not text:
        return None
    closer = _RUN_QUOTES.get(text[0])
    if closer is None or (text[0] == "`" and not tick):
        return None
    end = text.find(closer, 1)
    if end <= 0:
        return None
    return text[1:end], text[end + 1:]


def _expectation(text: str) -> tuple[str, str, str] | None:
    """Вид ожидания, значение и остаток строки (хвосты); None — вид не распознан."""
    match = _RUN_EXIT_RE.match(text)
    if match:
        return "exit", match.group("code"), text[match.end():]
    match = _RUN_CONTAINS_RE.match(text)
    if match:
        quoted = _quoted_value(text[match.end():])
        return ("contains", quoted[0], quoted[1]) if quoted else None
    match = _RUN_REGEX_RE.match(text)
    if match:
        quoted = _quoted_value(text[match.end():], tick=True)
        return ("regex", quoted[0], quoted[1]) if quoted else None
    return None


def _parse_run(text: str) -> StepRun | None:
    """Пункт «Прогон» → данные прогона; None — пункт не разбирается.

    Функция никогда не бросает: битый пункт — это «не прогон», а не падение сборщика
    контекста. Разбор повторяет правила §5 формата плана дословно — их же копией судит
    линтер (`plan_lint._run_problem`), поэтому правка здесь требует правки там.
    """
    match = _RUN_TICK_RE.search(text)
    if match is None:
        return None
    command = match.group(1).strip()
    if not command:
        return None
    rest = text[match.end():]
    arrow = next((item for item in _RUN_ARROWS if item in rest), None)
    if arrow is None:
        return None
    parsed = _expectation(rest.split(arrow, 1)[1].lstrip())
    if parsed is None:
        return None
    expect, value, tail = parsed
    timeout: int | None = None
    found = _RUN_TIMEOUT_RE.search(tail)
    if found:
        try:
            timeout = int(found.group("seconds"))
        except ValueError:  # pragma: no cover - регулярка уже отобрала цифры
            timeout = None
    return StepRun(command=command, expect=expect, value=value, timeout=timeout,
                   slow=_RUN_SLOW_RE.search(tail) is not None)


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
    runs: list[StepRun] = []
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
        # ANCHOR:прогоны — прогон шага собирается в данные, а не в гейты: одна и та же
        # команда иначе печаталась бы в карточке дважды, а анализатор судил бы её как гейт.
        elif label.startswith("прогон"):
            run = _parse_run(text)
            if run is not None:
                runs.append(run)
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
                risk=risk, auto=auto, runs=runs)


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
    dirty = [line for line in _run(["git", "status", "--porcelain"], cfg.repo).splitlines()
             if line.strip()]
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


# ANCHOR:дайджест — пакет шага: объявленные файлы целиком, лимит — предохранитель (план 9, S5)
# Числа — из §S5 плана: под пакет идёт 45 % окна профиля, токен смешанного русского кода —
# ≈1,35 символа. Бюджет считается из окна, а не зашивается константой: 607 500 и 77 760
# символов — результаты на окнах 1 млн и 128k, а не константы. Смысл пакета не в экономии окна:
# объём шага ограничен, вход исполнителя и ревьюера один и тот же, а объявленные файлы — ещё и
# граница правок (F01). Тихая обрезка запрещена: что не влезло — помечено, перебор даёт F09.
DIGEST_WINDOW_SHARE = 0.45
DIGEST_CHARS_PER_TOKEN = 1.35
DIGEST_WINDOW_DEFAULT = 128_000
DIGEST_FRAGMENT_LINES = 80      # строк в окрестности якоря и в начале файла без якорей
DIGEST_COMMITS = 3              # последних коммитов по объявленным файлам
DIGEST_TEST_HINTS = 20          # строк «релевантные тесты по имени»
DIGEST_RESERVE = 600            # запас бюджета под шапку, «что не вошло» и находки
DIGEST_ANCHOR_RE = re.compile(r"ANCHOR:([\w\-]+)", re.UNICODE)
DIGEST_FENCE = "````"           # ограда длиннее стандартной: в файлах движка есть свои ```
DIGEST_OUTSIDE_NOTE = ("остальной репозиторий не читался: в пакет попадают только объявленные "
                       "файлы шага")
DIGEST_NO_COMMIT = "F01 — коммита шага нет: проверка в карточке ревью"


def window_tokens(cfg) -> tuple[int, str]:
    """Окно профиля в токенах и источник значения (Д1).

    Читается терпимо: целое, строка из цифр и целый `float` годятся; отсутствие ключа, ноль,
    отрицательное, дробное и мусор дают `DIGEST_WINDOW_DEFAULT`. Опечатка владельца не роняет
    конвейер, но и не проходит молча: шапка пакета говорит, откуда окно.
    """
    key = "checks.context_window_tokens"
    raw = getattr(cfg, "state", None) or {}
    block = raw.get("checks") if isinstance(raw, dict) else None
    value = block.get("context_window_tokens") if isinstance(block, dict) else None
    if value is None:
        return DIGEST_WINDOW_DEFAULT, f"по умолчанию: в конфиге нет `{key}`"
    number: int | None = None
    if isinstance(value, bool):
        number = None
    elif isinstance(value, int):
        number = value
    elif isinstance(value, float) and value.is_integer():
        number = int(value)
    elif isinstance(value, str) and value.strip().isdigit():
        number = int(value.strip())
    if number is None or number <= 0:
        return DIGEST_WINDOW_DEFAULT, f"по умолчанию: `{key}`={value!r} не годится"
    return number, f"из `{key}`"


def digest_budget(window: int) -> int:
    """Бюджет пакета в символах: `round(окно × 0,45 × 1,35)` (Д2, Д3 — символы, не байты)."""
    return int(round(int(window) * DIGEST_WINDOW_SHARE * DIGEST_CHARS_PER_TOKEN))


def digest_path(cfg, plan_id: str, step_id: str) -> Path:
    """Файл пакета шага: `<state_dir>/package-plan-<N>-<ШАГ>.md`.

    Имя `digest.json` не занимается: оно принадлежит сводке уведомлений проекта.
    """
    return Path(cfg.state_dir) / f"package-plan-{plan_id}-{str(step_id).upper()}.md"


def _digest_declared(step: dict) -> list[str]:
    """Объявленные файлы шага в порядке плана: нормализованные пути без дублей."""
    out: list[str] = []
    for item in step.get("files") or []:
        path = str(item).strip().strip("`").replace("\\", "/")
        while path.startswith("./"):
            path = path[2:]
        if path and path not in out:
            out.append(path)
    return out


def _digest_num(value) -> str:
    """Число с пробелами между разрядами: пакет читает человек."""
    return f"{int(value or 0):,}".replace(",", " ")


def _digest_read(path: Path) -> tuple[str, bool]:
    """Текст файла; нет файла или не читается — `("", False)`, а не исключение."""
    try:
        return path.read_text(encoding="utf-8", errors="replace"), True
    except OSError:
        return "", False


def _digest_ranges(text: str) -> list[tuple[int, int]]:
    """Окрестности якорей `ANCHOR:` — ±`DIGEST_FRAGMENT_LINES` строк, пересечения слиты.

    Якорей нет — первые `DIGEST_FRAGMENT_LINES` строк: пустой фрагмент был бы хуже честного
    начала файла. Индексы с нуля, границы полуоткрытые.
    """
    lines = text.splitlines()
    if not lines:
        return []
    starts = sorted({index for index, line in enumerate(lines) if DIGEST_ANCHOR_RE.search(line)})
    if not starts:
        return [(0, min(len(lines), DIGEST_FRAGMENT_LINES))]
    ranges: list[tuple[int, int]] = []
    for index in starts:
        start = max(0, index - DIGEST_FRAGMENT_LINES)
        end = min(len(lines), index + DIGEST_FRAGMENT_LINES + 1)
        if ranges and start <= ranges[-1][1]:
            ranges[-1] = (ranges[-1][0], max(ranges[-1][1], end))
        else:
            ranges.append((start, end))
    return ranges


def _digest_whole(rel: str, text: str) -> str:
    """Файл целиком: заголовок с числом строк и символов, тело под оградой."""
    lines = text.splitlines()
    return (f"## Файл: {rel} — целиком ({_digest_num(len(lines))} строк, "
            f"{_digest_num(len(text))} символов)\n\n"
            f"{DIGEST_FENCE}\n{text.rstrip()}\n{DIGEST_FENCE}\n")


def _digest_fragment(rel: str, text: str) -> str:
    """Фрагменты файла с пометкой обрезки: видно, что прочитано не всё и докуда."""
    lines = text.splitlines()
    ranges = _digest_ranges(text)
    if not ranges:
        return f"## Файл: {rel} — обрезано: файл пуст\n"
    spans = ", ".join(f"{start + 1}–{end}" for start, end in ranges)
    tail = "" if DIGEST_ANCHOR_RE.search(text) else " (якорей нет)"
    parts = [f"## Файл: {rel} — обрезано: строки {spans} из {_digest_num(len(lines))}{tail}\n"]
    for start, end in ranges:
        chunk = "\n".join(lines[start:end])
        parts.append(f"{DIGEST_FENCE}\n# строки {start + 1}–{end} из {len(lines)}\n"
                     f"{chunk}\n{DIGEST_FENCE}\n")
    return "\n".join(parts)


def _digest_commits(cfg, declared: list[str]) -> list[str]:
    """Последние `DIGEST_COMMITS` коммитов по объявленным файлам: sha, дата, тема."""
    if not declared:
        return ["- объявленных файлов нет: коммитов не ищем"]
    cmd = ["git", "log", f"-{DIGEST_COMMITS}", "--pretty=format:%h %ad %s", "--date=short",
           "--", *declared]
    try:
        res = subprocess.run(cmd, cwd=str(cfg.repo), capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=20)
    except (OSError, subprocess.SubprocessError):
        return ["- git недоступен: коммиты по файлам не прочитаны"]
    if res.returncode != 0:
        return ["- git недоступен: коммиты по файлам не прочитаны"]
    rows = [line.strip() for line in (res.stdout or "").splitlines() if line.strip()]
    return [f"- {row}" for row in rows] or ["- коммитов по объявленным файлам нет"]


def _digest_tests(cfg, declared: list[str]) -> list[str]:
    """Тесты, чьё имя содержит основу объявленного файла (`gate` → `test_gate_e2e.py`).

    Содержимое тестов не читается — только имена: пакет даёт карту, а не второй прогон.
    """
    stems = {Path(rel).stem.lower() for rel in declared if Path(rel).suffix == ".py"}
    if not stems:
        return ["- тестов по имени нет: объявленные файлы не `*.py`"]
    base = Path(cfg.repo) / "tests"
    if not base.is_dir():
        return ["- каталога `tests/` нет"]
    found: list[str] = []
    for path in sorted(base.rglob("*.py")):
        name = path.name.lower()
        if any(stem in name for stem in stems):
            found.append(path.relative_to(Path(cfg.repo)).as_posix())
    if not found:
        return ["- тестов по имени нет"]
    shown = [f"- {rel}" for rel in found[:DIGEST_TEST_HINTS]]
    if len(found) > DIGEST_TEST_HINTS:
        shown.append(f"- … и ещё {len(found) - DIGEST_TEST_HINTS}")
    return shown


def _digest_cut(text: str, budget: int) -> str:
    """Обрезать пакет до бюджета с честной пометкой: предохранитель, а не украшение."""
    mark = "\n…обрезано: пакет длиннее бюджета — объявленные файлы смотреть по одному\n"
    if budget <= len(mark):
        return mark.strip("\n")
    return text[:budget - len(mark)] + mark


def digest_f01(cfg, step: dict, plan_id: str) -> str:
    """Строка F01 пакета: посторонние файлы коммита шага — проверка S4 (Д6).

    Импорт `gate` ленивый: `gate.py` импортирует `next_step` на уровне модуля, и обратный
    модульный импорт дал бы вторую копию модуля, когда `next_step.py` запускают скриптом.
    «Коммита шага ещё нет» — это отсутствие доказательства прогонов на текущем HEAD: тот же
    признак, по которому страж не кладёт `ctx["step_runs"]`.
    """
    try:
        import gate  # ленивый импорт: обратный к модульному импорту gate

        paths = gate.resolve_paths(cfg, plan_id)
        sha = gate.head_commit(paths)
        if not sha or gate.load_runs_proof(paths, str(step.get("id") or ""), sha) is None:
            return DIGEST_NO_COMMIT
        offenders = gate.step_file_offenders(paths, step, sha)
        count = len(gate.commit_files(paths, sha))
    except (OSError, ValueError, KeyError, AttributeError) as exc:
        return f"F01 — не проверено: {str(exc)[:120]}"
    if offenders:
        return "F01 — файлов вне объявленных: " + ", ".join(offenders)
    return f"F01 — файлов вне объявленных нет ({count} файлов)"


def _digest_build(cfg, step: dict, plan_id: str | None = None) -> dict[str, Any]:
    """Собрать пакет шага без записи: шапка, файлы, коммиты, тесты, «что не вошло», находки.

    Порядок разделов фиксирован (§4.2 постановки): по нему пакет читают и исполнитель, и
    ревьюер. Файлы идут в порядке объявленного списка: целиком, если вписываются в остаток
    бюджета, иначе — окрестностями якорей, иначе — в «что не вошло» с причиной.
    """
    plan_id = registry.normalize(plan_id or registry.default_plan_id(cfg))
    step_id = str(step.get("id") or "").upper()
    window, source = window_tokens(cfg)
    budget = digest_budget(window)
    declared = _digest_declared(step)
    git = git_state(cfg)
    head = [
        f"ПАКЕТ ШАГА — {cfg.project}: план {plan_id}, шаг {step_id} — {step.get('title', '')}",
        f"HEAD: {git.get('head') or '?'} · {git.get('branch') or '?'}",
        f"Окно профиля: {_digest_num(window)} токенов ({source}); бюджет пакета "
        f"{_digest_num(budget)} символов (45 % окна × 1,35 символа на токен)",
    ]
    tail = ["", f"## Последние {DIGEST_COMMITS} коммита по объявленным файлам",
            *_digest_commits(cfg, declared),
            "", "## Релевантные тесты по имени", *_digest_tests(cfg, declared)]
    room = max(budget - len("\n".join(head)) - len("\n".join(tail)) - DIGEST_RESERVE, 0)
    sections: list[str] = []
    whole: list[str] = []
    fragments: list[str] = []
    missing: list[str] = []
    omitted: list[str] = []
    volume = 0
    for rel in declared:
        text, exists = _digest_read(Path(cfg.repo) / rel)
        if not exists:
            missing.append(rel)
            sections.append(f"## Файл: {rel} — нет файла в репозитории\n")
            continue
        volume += len(text)
        section = _digest_whole(rel, text)
        if len(section) <= room:
            sections.append(section)
            room -= len(section)
            whole.append(rel)
            continue
        piece = _digest_fragment(rel, text)
        if len(piece) <= room:
            sections.append(piece)
            room -= len(piece)
            fragments.append(rel)
            continue
        omitted.append(rel)
    finding_f09 = ""
    if fragments or omitted:
        finding_f09 = (f"F09 — {registry.finding_code_title('F09')}: объём "
                       f"{_digest_num(volume)} при бюджете {_digest_num(budget)}; файлов "
                       f"фрагментами {len(fragments)} из {len(declared)}")
    finding_f01 = digest_f01(cfg, step, plan_id)
    outside = ["", "## Что не вошло", f"- {DIGEST_OUTSIDE_NOTE}"]
    if omitted:
        outside.append(f"- не вошло: бюджет исчерпан — {', '.join(omitted)}")
    if missing:
        outside.append(f"- нет файла в репозитории: {', '.join(missing)}")
    founds = ["", "## Находки", f"- {finding_f01}"]
    if finding_f09:
        founds.append(f"- {finding_f09}")
    mode = "фрагменты" if fragments or omitted else "целиком"
    # «Объём пакета» — итог целиком, поэтому строка вставляется после сборки текста: иначе
    # шапка показывала бы объём без хвоста (коммиты, «что не вошло», находки).
    counts = (f"Объём пакета: {{v}} символов; режим: {mode}; файлов целиком: {len(whole)}, "
              f"фрагментами: {len(fragments)}")
    placeholder = counts.format(v=0)
    head.append(placeholder)
    text = "\n".join([*head, "", *sections, *tail, *outside, *founds]) + "\n"
    line = counts.format(v=len(text))
    text = text.replace(placeholder, line, 1)
    exact = counts.format(v=len(text))
    if exact != line:
        text = text.replace(line, exact, 1)
    if len(text) > budget:
        text = _digest_cut(text, budget)
    return {
        "path": str(digest_path(cfg, plan_id, step_id)),
        "text": text,
        "chars": len(text),
        "budget": budget,
        "window": window,
        "window_source": source,
        "whole": whole,
        "fragments": fragments,
        "missing": missing,
        "f09": finding_f09,
        "f01": finding_f01,
    }


def _digest_write(path: Path, text: str) -> None:
    """Атомарная запись пакета: временный файл рядом, fsync, `os.replace`.

    `newline=""` обязателен: на Windows обычная запись превращает каждый перевод строки в
    CRLF, и файл оказывается длиннее посчитанного объёма — предохранитель и число в шапке
    начинают врать. Одинаковые байты на Windows и Linux — часть идемпотентности.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    try:
        with io.open(tmp, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def build_digest(cfg, step: dict, plan_id: str | None = None) -> dict[str, Any]:
    """Пакет шага: собрать, записать атомарно и вернуть путь, объём, бюджет и находки.

    Ни даты, ни времени, ни случайных значений — ни в имени, ни в теле: два прогона на одном
    дереве дают одинаковые байты. Запись атомарная (`os.replace`): ревьюер не увидит пакет,
    записанный наполовину.
    """
    pkg = _digest_build(cfg, step, plan_id)
    _digest_write(Path(pkg["path"]), pkg["text"])
    return pkg


def digest_summary(cfg, step: dict, plan_id: str | None = None) -> dict[str, Any]:
    """Тот же пакет, но без записи: файл уже есть, и пересобирать его поверх нельзя (Д5)."""
    return _digest_build(cfg, step, plan_id)


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


def _package_step(cfg, plan_id: str, spec, force_step: str | None = None) -> Step | None:
    """Шаг, для которого собирается пакет: названный `--step` или первый открытый."""
    steps = plan_steps(cfg, Path(spec.file))
    if force_step:
        target = next((s for s in steps if s.id.upper() == force_step.upper()), None)
        if target is None:
            raise SystemExit(f"неизвестный шаг: {force_step} (план {plan_id}). Доступные: " +
                             (", ".join(s.id for s in steps) or "шаги плана не распознаны"))
        return target
    closed_all = set(registry.closed_steps(spec, cfg))
    return next((s for s in steps if s.id.upper() not in closed_all), None)


def _print_package(cfg, plan_id: str, spec, force_step: str | None = None) -> int:
    """`--package`: записать пакет шага и напечатать одной строкой путь, числа и объём."""
    target = _package_step(cfg, plan_id, spec, force_step)
    if target is None:
        print(f"план {plan_id}: пакет не собран — открытых шагов нет")
        return 0
    pkg = build_digest(cfg, asdict(target), plan_id)
    print(f"пакет шага: {pkg['path']} — файлов целиком: {len(pkg['whole'])}, фрагментами: "
          f"{len(pkg['fragments'])}; бюджет {pkg['budget']} символов, "
          f"объём {pkg['chars']} символов")
    return 0


def main(argv: list[str] | None = None) -> int:
    config_mod.setup_stdio()
    parser = argparse.ArgumentParser(description="Контекст следующего шага плана")
    parser.add_argument("--plan", help="номер плана (по умолчанию из pipeline.json)")
    parser.add_argument("--step", help="принудительный шаг, например S3")
    parser.add_argument("--json", action="store_true", help="машинный вывод")
    parser.add_argument("--brief", action="store_true", help="короткая версия для промта")
    parser.add_argument("--package", action="store_true",
                        help="пакет шага: записать файл и напечатать путь и числа")
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

    if args.package:
        return _print_package(cfg, plan_id, spec, args.step)

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

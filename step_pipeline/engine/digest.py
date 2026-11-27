# -*- coding: utf-8 -*-
"""Пакет шага (план 9, S5): объявленные файлы целиком, лимит — предохранитель.

Смысл пакета не в экономии окна, а в трёх других вещах: объём шага ограничен (модель не
тонет во всём репозитории), вход воспроизводим (метрики S6 сравнимы между прогонами), а
объявленные файлы — ещё и граница правок (F01). Поэтому тихой обрезки нет: что не влезло
целиком, помечено «фрагментами» или «обрезано», а шаг получает находку F09 «шаг не
влезает в пакет — делить». Решение «делить шаг или править по фрагментам» принимает
владелец, не шаг.

Бюджет — не константа, а функция окна профиля: ключ `checks.context_window_tokens`
(мастер установки пишет его из выбранной модели; нет ключа — 128 000). Объём пакета не
больше 45 % окна в токенах при пересчёте ≈1,35 символа на токен для смешанного русского
кода: символы = окно × 0,45 × 1,35, то есть 607 500 на окне 1 млн и 77 760 на 128k.

Пакет одинаков для исполнителя и ревьюера: карточка шага собирает пакет и пишет его в
каталог состояния (`<state_dir>/digest/plan-<N>-<ШАГ>.md` плюс `.json` с числами), а
карточка ревью читает ту же мету и не пересобирает пакет под выросшее дерево — иначе спор
«ты не прочитал X» был бы неразрешим.

Модуль копируется в проекты вместе с движком (`config.ENGINE_FILES`) и обязан работать без
плагина: только stdlib и соседний `state_io`.
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import state_io  # модуль рядом: атомарная запись

#: Каталог пакетов шага внутри каталога состояния.
DIGEST_DIR = "digest"
#: Окно профиля по умолчанию — ключа `checks.context_window_tokens` нет.
CONTEXT_WINDOW_DEFAULT = 128_000
#: Доля окна под пакет шага.
BUDGET_SHARE = 0.45
#: Символов на токен для смешанного русского кода (замер из плана 9, S5).
CHARS_PER_TOKEN = 1.35
#: Окрестность якоря `ANCHOR:` в режиме фрагментов — строк.
FRAGMENT_LINES = 80
#: Сколько строк кода показывать до якоря внутри окрестности.
FRAGMENT_LEAD = 20
#: Последних коммитов по объявленным файлам — в пакете.
COMMITS_LOGGED = 3
#: Предел подсказок тестов по имени файла.
TEST_HINTS_LIMIT = 20
#: Файлов, на которые разворачивается объявленный каталог.
DIR_FILE_LIMIT = 100
#: Запас бюджета под блок «что не вошло»: список имён плюс пояснение.
OMITTED_RESERVE = 400
#: Ограда кода длиннее стандартной: внутри файлов движка встречаются свои ```` ``` ````.
FENCE = "````"

ANCHOR_RE = re.compile(r"ANCHOR:([\w\-]+)", re.UNICODE)

PACKAGE_HEADER = "ПАКЕТ ШАГА (план 9, S5)"
NOTHING_DECLARED = "объявленных файлов нет: пакет несёт только контракт шага"
OUTSIDE_NOTE = "остальной репозиторий не читался: в пакет попадают только объявленные файлы шага"


# ───────────────────────────── окно и бюджет ─────────────────────────────

def context_window(cfg) -> int:
    """Окно профиля в токенах из `checks.context_window_tokens`; мусор — умолчание.

    Ключа нет в проектах, установленных до шага: 128 000 — честная нижняя оценка, при
    которой объявленные файлы движка не влезают и шаг получает F09, а не тихую обрезку.
    """
    raw = getattr(cfg, "_raw", None) or {}
    block = raw.get("checks") if isinstance(raw, dict) else None
    value = block.get("context_window_tokens") if isinstance(block, dict) else None
    if isinstance(value, bool) or value is None:
        return CONTEXT_WINDOW_DEFAULT
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return CONTEXT_WINDOW_DEFAULT
    return number if number > 0 else CONTEXT_WINDOW_DEFAULT


def budget_chars(window: int) -> int:
    """Бюджет пакета в символах: 45 % окна × 1,35 символа на токен."""
    return int(round(max(int(window), 1) * BUDGET_SHARE * CHARS_PER_TOKEN))


# ───────────────────────────── объявленные файлы ─────────────────────────────

def declared_files(step: dict) -> list[str]:
    """Объявленные файлы шага из пункта «Файл:» плана — нормализованные пути, без дублей."""
    out: list[str] = []
    for item in step.get("files") or []:
        path = str(item).strip().strip("`").replace("\\", "/")
        while path.startswith("./"):
            path = path[2:]
        if path and path not in out:
            out.append(path)
    return out


def _expand(cfg, declared: list[str]) -> list[str]:
    """Объявленный каталог раскрывается в файлы (до `DIR_FILE_LIMIT`), файл остаётся собой."""
    root = Path(cfg.repo)
    out: list[str] = []
    for rel in declared:
        path = root / rel
        if not path.is_dir():
            out.append(rel)
            continue
        count = 0
        for child in sorted(path.rglob("*")):
            if not child.is_file() or count >= DIR_FILE_LIMIT:
                continue
            out.append(child.relative_to(root).as_posix())
            count += 1
    return out


# ───────────────────────────── сборка пакета ─────────────────────────────

def digest_dir(cfg) -> Path:
    return Path(cfg.state_dir) / DIGEST_DIR


def digest_path(cfg, plan_id: str, step_id: str) -> Path:
    """Файл пакета шага: `<state_dir>/digest/plan-<N>-<ШАГ>.md`."""
    return digest_dir(cfg) / f"plan-{plan_id}-{str(step_id).upper()}.md"


def meta_path(cfg, plan_id: str, step_id: str) -> Path:
    """Мета пакета (числа и состав) рядом с текстом: её читает карточка ревью."""
    return digest_path(cfg, plan_id, step_id).with_suffix(".json")


def _read(path: Path) -> tuple[str, bool]:
    """Текст файла; нет файла или не читается — `("", False)`, а не исключение."""
    try:
        return path.read_text(encoding="utf-8", errors="replace"), True
    except OSError:
        return "", False


def fragment_ranges(text: str) -> list[tuple[int, int]]:
    """Окрестности якорей `ANCHOR:` — по `FRAGMENT_LINES` строк, пересечения схлопнуты.

    Якорей нет (файл не движковый) — первые `FRAGMENT_LINES` строк: пустой пакет хуже
    честного начала файла. Индексы — с нуля, границы полуоткрытые.
    """
    lines = text.splitlines()
    if not lines:
        return []
    starts = sorted({max(0, index - FRAGMENT_LEAD)
                     for index, line in enumerate(lines) if ANCHOR_RE.search(line)})
    if not starts:
        return [(0, min(len(lines), FRAGMENT_LINES))]
    ranges: list[tuple[int, int]] = []
    for start in starts:
        end = min(len(lines), start + FRAGMENT_LINES)
        if ranges and start <= ranges[-1][1]:
            ranges[-1] = (ranges[-1][0], max(ranges[-1][1], end))
        else:
            ranges.append((start, end))
    return ranges


def _whole_section(rel: str, text: str) -> str:
    return (f"## Файл: {rel} — целиком ({len(text)} символов)\n\n"
            f"{FENCE}\n{text.rstrip()}\n{FENCE}\n")


def _fragment_section(rel: str, text: str) -> str:
    """Файл окрестностями якорей с пометкой обрезки: видно, что прочитано не всё."""
    lines = text.splitlines()
    ranges = fragment_ranges(text)
    if not ranges:
        return f"## Файл: {rel} — обрезано: файл пуст ({len(lines)} строк)\n"
    parts = [f"## Файл: {rel} — обрезано: фрагментами ({len(ranges)} окрестн. по "
             f"{FRAGMENT_LINES} строк из {len(lines)})\n"]
    for start, end in ranges:
        chunk = "\n".join(lines[start:end])
        parts.append(f"{FENCE}\n# строки {start + 1}-{end} из {len(lines)}\n"
                     f"{chunk}\n{FENCE}\n")
    return "\n".join(parts)


def _cut(text: str, room: int) -> str:
    """Обрезать блок по остатку бюджета, оставив честную пометку."""
    mark = "\n…обрезано: места в бюджете пакета не осталось\n"
    if room <= len(mark):
        return mark.strip() + "\n"
    return text[:room - len(mark)] + mark


def _commits_lines(cfg, declared: list[str]) -> list[str]:
    """Последние `COMMITS_LOGGED` коммитов по объявленным файлам (пусто — строка-пояснение)."""
    if not declared:
        return []
    try:
        res = subprocess.run(["git", "log", f"-{COMMITS_LOGGED}", "--oneline", "--", *declared],
                             cwd=str(cfg.repo), capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=20)
    except (OSError, subprocess.SubprocessError):
        return ["- git недоступен: последние коммиты по файлам не прочитаны"]
    if res.returncode != 0:
        return ["- git недоступен: последние коммиты по файлам не прочитаны"]
    rows = [line.strip() for line in (res.stdout or "").splitlines() if line.strip()]
    return [f"- {row}" for row in rows] or ["- коммитов по объявленным файлам нет"]


def _test_hints(cfg, declared: list[str]) -> list[str]:
    """Тесты, чьё имя содержит имя объявленного файла (`gate` → `test_gate_e2e.py`)."""
    stems = {Path(rel).stem.lower() for rel in declared if Path(rel).suffix == ".py"}
    if not stems:
        return []
    root = Path(cfg.repo)
    found: list[str] = []
    for folder in ("tests", "test"):
        base = root / folder
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.py")):
            name = path.name.lower()
            if not any(stem in name for stem in stems):
                continue
            rel = path.relative_to(root).as_posix()
            if rel not in found:
                found.append(rel)
            if len(found) >= TEST_HINTS_LIMIT:
                break
    return [f"- {rel}" for rel in sorted(found)]


def _num(value) -> str:
    """Число с пробелами между разрядами: карточки читает человек, а не парсер."""
    return f"{int(value):,}".replace(",", " ")


def _rel(cfg, path: Path) -> str:
    try:
        return Path(path).resolve().relative_to(Path(cfg.repo).resolve()).as_posix()
    except (ValueError, OSError):
        return Path(path).as_posix()


def collect(cfg, step: dict, plan_id: str, plan_file: Path | None = None,
            window: int | None = None, persist: bool = True) -> dict:
    """Собрать пакет шага, записать его в каталог состояния и вернуть мету с числами.

    Объявленные файлы пробуются целиком по порядку плана; что не влезло — фрагментами
    (окрестности якорей), что не влезло и так — в списке «не вошло». Пакет никогда не
    длиннее бюджета и никогда не обрезан молча: каждая обрезка помечена, а перебор даёт
    находку F09 с числами.

    `persist=False` — режим чтения (`next_step.py --json`, снимок панели): пакет считается,
    но в проект ничего не пишется. Предпросмотр и панель обязаны оставаться без побочных
    эффектов, поэтому мета тогда говорит `written: false`.
    """
    window = int(window or context_window(cfg))
    budget = budget_chars(window)
    step_id = str(step.get("id") or "")
    declared = declared_files(step)
    files = _expand(cfg, declared)
    plan_rel = _rel(cfg, Path(plan_file)) if plan_file else "—"

    head = [
        f"# Пакет шага {plan_id} {step_id} — {step.get('title', '')}".rstrip(),
        "",
        f"План: `{plan_rel}` §{step_id}",
        f"Окно профиля: {_num(window)} токенов → бюджет пакета {_num(budget)} символов "
        f"(45 % окна × 1,35 символа на токен)",
        "",
    ]
    if declared:
        head += ["## Объявленные файлы шага", *[f"- {rel}" for rel in declared]]
    else:
        head.append(NOTHING_DECLARED)
    tail = ["", "## Последние коммиты по объявленным файлам", *_commits_lines(cfg, declared),
            "", "## Релевантные тесты по имени", *(_test_hints(cfg, files) or ["- нет"])]

    room = max(budget - len("\n".join(head)) - len("\n".join(tail)) - OMITTED_RESERVE, 0)
    sections: list[str] = []
    full: list[str] = []
    fragmented: list[str] = []
    omitted: list[str] = []
    missing: list[str] = []
    declared_chars = 0
    for rel in files:
        text, exists = _read(Path(cfg.repo) / rel)
        if not exists:
            missing.append(rel)
            sections.append(f"## Файл: {rel} — нет файла в репозитории\n")
            continue
        declared_chars += len(text)
        section = _whole_section(rel, text)
        if len(section) <= room:
            sections.append(section)
            room -= len(section)
            full.append(rel)
            continue
        if room <= 0:
            omitted.append(rel)
            continue
        section = _fragment_section(rel, text)
        if len(section) > room:
            section = _cut(section, room)
        sections.append(section)
        room = max(room - len(section), 0)
        fragmented.append(rel)

    tail_outside = ["", "## Что не вошло", f"- {OUTSIDE_NOTE}"]
    if omitted:
        tail_outside.append(f"- не влезло в бюджет: {', '.join(omitted)}")
    if missing:
        tail_outside.append(f"- нет файла в репозитории: {', '.join(missing)}")

    finding = ""
    if fragmented or omitted:
        finding = (f"F09 — шаг не влезает в пакет — делить: объявленные файлы "
                   f"{_num(declared_chars)} символов при бюджете {_num(budget)} "
                   f"(окно {_num(window)} токенов); файлов целиком: {len(full)}, "
                   f"фрагментами: {len(fragmented)}, не вошло: {len(omitted)}")

    text = "\n".join([*head, "", *sections, *tail, *tail_outside]) + "\n"
    if len(text) > budget:
        text = _cut(text, budget)

    path = digest_path(cfg, plan_id, step_id)
    meta = {
        "plan": str(plan_id),
        "step": step_id,
        "window": window,
        "budget": budget,
        "chars": len(text),
        "path": _rel(cfg, path),
        "written": bool(persist),
        "declared": declared,
        "files": files,
        "full": full,
        "fragmented": fragmented,
        "omitted": omitted,
        "missing": missing,
        "declared_chars": declared_chars,
        "finding": finding,
    }
    if not persist:
        return meta
    state_io.atomic_write_text(path, text)
    try:
        state_io.save_json(meta_path(cfg, plan_id, step_id), meta)
    except OSError:
        # Мета не записана — карточка ревью пересоберёт пакет: это хуже, но не стоп.
        pass
    return meta


def load_meta(cfg, plan_id: str, step_id: str) -> dict | None:
    """Мета записанного пакета: нет файла или битый — `None` (пересборка на стороне вызова)."""
    try:
        data = json.loads(meta_path(cfg, plan_id, step_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def card_lines(meta: dict | None, note: str = "") -> list[str]:
    """Блок карточки «пакет шага»: окно, бюджет, что целиком и что фрагментами, F09.

    Числа берутся из меты, а не пересчитываются: карточка ревью обязана показывать тот же
    вход, что был у исполнителя.
    """
    if not meta:
        return []
    lines = [
        PACKAGE_HEADER,
        f"- Окно профиля: {_num(meta.get('window') or 0)} токенов → бюджет пакета "
        f"{_num(meta.get('budget') or 0)} символов (45 % окна × 1,35 символа на токен); "
        f"собрано {_num(meta.get('chars') or 0)} символов.",
        f"- Файлов целиком: {len(meta.get('full') or [])}, фрагментами: "
        f"{len(meta.get('fragmented') or [])}, не вошло: {len(meta.get('omitted') or [])}, "
        f"нет файла: {len(meta.get('missing') or [])}.",
        f"- Файл пакета: `{meta.get('path') or '—'}` — читать до кода, тихой обрезки нет.",
    ]
    if meta.get("fragmented"):
        lines.append(f"- Фрагментами: {', '.join(meta['fragmented'])}")
    if meta.get("omitted"):
        lines.append(f"- Не вошло в бюджет: {', '.join(meta['omitted'])}")
    if meta.get("finding"):
        lines.append(f"- {meta['finding']}")
    if note:
        lines.append(f"- {note}")
    return lines


def package_lines(cfg, step: dict, plan_id: str, plan_file: Path | None = None,
                  window: int | None = None, reuse: bool = False) -> list[str]:
    """Блок карточки шага (собирает пакет) или карточки ревью (`reuse=True` — читает мету)."""
    step_id = str(step.get("id") or "")
    if reuse:
        meta = load_meta(cfg, plan_id, step_id)
        if meta is not None:
            return card_lines(meta, "Пакет записан при заказе шага: ревьюер смотрит "
                                   "тот же вход, что был у исполнителя.")
    return card_lines(collect(cfg, step, plan_id, plan_file, window))

# -*- coding: utf-8 -*-
"""Страж шага: конвейер «шаг → ревью → следующий шаг» без остановок.

Разделение ролей, которое и есть смысл конструкции:
    исполнитель  делает шаг и коммитит его;
    промптер     заранее пишет постановку шага (без неё шаг не начинается);
    ревьюер      выносит машиночитаемый вердикт по плану и коду;
    страж (этот скрипт) исполняет вердикт: ставит карточки, пишет журнал, коммитит.

Почему страж — скрипт, а не «сотрудник-агент»: решение «всё хорошо» — это вердикт
(`<reviews_dir>/<план>-<шаг>.md`) плюс проверки. Агент на этом месте стоил бы
модельного прогона и мог бы согласиться с собственной прозой.

Состояния (до `max_actions` действий за тик, cron каждые 10 минут):

    шага нет                  → карточка шага        (профиль исполнителя)
    нет постановки шага       → карточка промптеру + инцидентный стоп
    шаг done                  → карточка ревью       (профиль ревьюера)
    вердикт APPROVE           → строка в журнал плана + коммит/пуш
    вердикт REQUEST_CHANGES   → карточка правок (1-й раз), стоп + уведомление (2-й раз)
    шаг упал/ревью упало      → стоп + уведомление
    план не утверждён         → карточки нет: причина в выводе, одно уведомление владельцу
    план изменён после утв.   → то же: sha256 файла не совпал с записью утверждения
    план уже идёт             → унаследованная запись (`inherited`) и работа продолжается

Утверждение плана сверяется перед любой карточкой: запись `plan_<НОМЕР>_approval.json`
в каталоге состояния плюс пересчёт sha256 файла плана. Исключение — план, который уже
идёт (есть файл состояния или закрытые шаги журнала): ему страж пишет унаследованную
запись и ведёт дальше, иначе шаг перекрыл бы дорогу сам себе.

Стоп-флаг блокирует ТОЛЬКО начало следующего шага: текущий шаг доводится до ревью и
журнала (это и значит «остановка ПОСЛЕ выполнения шага»).

Сколько действий делает тик, задаёт `max_actions` в `pipeline.json` проекта (умолчание 1 —
прежнее поведение: ровно одно действие за тик), флаг `--max-actions N` перекрывает это
значение на один запуск (cron берёт значение из файла). Состояние перечитывается перед
каждым следующим действием, поэтому цепочка «ревью APPROVE → журнал → следующий шаг →
заказ постановки» проезжается одним тиком. Инвариант предсказуемости: за тик шаг
получает не больше ОДНОГО действия — иначе быстрый воркер успел бы закрыть карточку
между действиями, и тик проскочил бы шаг целиком (исполнение и ревью сразу). Каждое
применённое действие дописывается в состояние плана (`last_actions`, последние 20
записей) и в историю тиков: файл `history.jsonl` в каталоге состояния, по строке JSONL на
действие, с ротацией по числу строк и размеру. Посчитанные по истории метрики отдаёт
`--status --json` (ключ `metrics`), снимок — под тем же ключом: видно, сколько было
действий за неделю, сколько шагов закрыто, сколько ушло на шаг и сколько было кругов
правок и инцидентов.

Простой виден сам: `idle` пишет `waiting_since`/`waiting_reason`, ожидание дольше
`notify.watchdog_hours` даёт одно напоминание на причину не чаще раза в час, а раз в
сутки после `notify.digest_at` уходит сводка (закрыто, ждёт, стоп — по строке на план).

Использование:
    python .pipeline/gate.py --status            # что видно и что было бы сделано
    python .pipeline/gate.py --plans             # таблица планов: шаг, закрыто, стоп
    python .pipeline/gate.py --plan 3 --status   # состояние конкретного плана
    python .pipeline/gate.py --run               # тик: до max_actions действий (режим cron)
    python .pipeline/gate.py --run --max-actions 3   # предел на один запуск
    python .pipeline/gate.py --run --plan 3      # тик только по одному плану
    python .pipeline/gate.py --stop "причина"    # остановиться после текущего шага
    python .pipeline/gate.py --resume            # снять стоп
    python .pipeline/gate.py --hold S3a          # шаг требует ручного допуска владельца
    python .pipeline/gate.py --unhold S3a
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config as config_mod  # noqa: E402 - модуль рядом
import registry  # noqa: E402 - реестр планов
import next_step  # noqa: E402 - сборщик контракта шага
import state_io  # noqa: E402 - атомарная запись состояния и блокировка тика

# ──────────────────── целостность движка ────────────────────
# Проект работает на КОПИИ движка в `.pipeline/`. Если движок правили в плагине,
# а копию не обновили, на одном плане оказываются две разные логики с одинаковым
# номером версии (прецедент 02.11.2026: gate.py 1364 строки в проекте против 1599
# в плагине, оба 0.2.0). Маркер ENGINE.sha256 пишется при установке/обновлении,
# тик сверяет его и отказывается работать при расхождении.
ENGINE_MARKER = "ENGINE.sha256"


def engine_digest(base: Path) -> str:
    """sha256 по содержимому всех .py движка: имя + байты, порядок стабилен."""
    digest = hashlib.sha256()
    for path in sorted(Path(base).glob("*.py")):
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def verify_engine_integrity(base: Path) -> tuple[bool, str]:
    """Сверить движок с маркером. Маркера нет — легаси-установка, не мешаем."""
    marker = Path(base) / ENGINE_MARKER
    if not marker.is_file():
        return True, "маркер целостности отсутствует (легаси-установка)"
    try:
        text = marker.read_text(encoding="utf-8").strip()
    except OSError as exc:
        return False, f"маркер не читается: {exc}"
    expected = text.split()[0].removeprefix("sha256:") if text else ""
    actual = engine_digest(base)
    if not expected:
        return False, "маркер пуст"
    if actual != expected:
        return False, (f"движок изменён после установки: {actual[:12]} != {expected[:12]}")
    return True, "движок соответствует маркеру"


TERMINAL = {"done", "blocked", "crashed", "timed_out", "gave_up"}
VERDICT_RE = re.compile(r"^#{0,4}\s*ВЕРДИКТ\s*:\s*(APPROVE|REQUEST_CHANGES)\s*$",
                        re.IGNORECASE | re.MULTILINE)
COMMIT_RE = re.compile(r"коммит\S*\s*`([0-9a-f]{7,40})`", re.IGNORECASE)

# Предел действий за тик: ключ `max_actions` в pipeline.json проекта, умолчание 1 (D1).
MAX_ACTIONS_DEFAULT = 1
# Сколько записей о действиях держим в состоянии плана: это краткая выжимка, полная
# история — в `history.jsonl`.
LAST_ACTIONS_KEPT = 20
# История тиков: файл в каталоге состояния, пределы ротации и окно метрик (план 2, S3).
HISTORY_NAME = "history.jsonl"
HISTORY_MAX_LINES = 2000
HISTORY_MAX_BYTES = 512 * 1024
METRICS_DAYS = 7
# Комментарий с находками в карточке правок: автор, предел длины и сколько находок берём.
COMMENT_AUTHOR = "страж"
COMMENT_MAX_LEN = 4000
FINDINGS_LIMIT = 5


# ───────────────────────────── окружение ─────────────────────────────

@dataclass
class Paths:
    """Все внешние точки, которые трогает страж (в тестах подменяются на временные)."""

    cfg: config_mod.Config
    repo: Path
    plan_file: Path
    review_dir: Path
    kanban_db: Path
    state_file: Path
    stop_file: Path
    plan_id: str
    journal_heading: str
    review_prefix: str
    spec: registry.PlanSpec

    @property
    def step_re(self) -> re.Pattern:
        return self.cfg.step_regex


class GateError(RuntimeError):
    """Понятный сбой движка: текст уходит в стоп-флаг, уведомление и `--json`."""

    def __init__(self, message: str, plan_id: str | None = None):
        super().__init__(message)
        self.plan_id = plan_id


def resolve_paths(cfg: config_mod.Config, plan_id: str | None = None) -> Paths:
    """Пути выбранного плана. Неизвестный номер — честная ошибка реестра."""
    want = registry.normalize(plan_id or registry.default_plan_id(cfg))
    spec = registry.get(cfg, want)
    return Paths(
        cfg=cfg,
        repo=cfg.repo,
        plan_file=Path(spec.file),
        review_dir=cfg.reviews_dir,
        kanban_db=cfg.kanban_db,
        state_file=Path(spec.state_file),
        stop_file=Path(spec.stop_file),
        plan_id=spec.id,
        journal_heading=spec.journal_heading,
        review_prefix=spec.review_prefix,
        spec=spec,
    )


def history_path(cfg: config_mod.Config) -> Path:
    """Файл истории тиков проекта: `<state_dir>/history.jsonl` (каталог может не быть)."""
    return Path(cfg.state_dir) / HISTORY_NAME


def _run(cmd: list[str], cwd: Path | None = None, timeout: int = 120,
         env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=str(cwd or Path.cwd()), capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout, env=env)


def _hermes() -> str:
    return shutil.which("hermes") or "hermes"


def _python(p: Paths) -> str:
    for cand in (Path(p.repo) / ".venv" / "Scripts" / "python.exe",
                 Path(p.repo) / ".venv" / "bin" / "python",
                 Path(p.repo) / "venv" / "Scripts" / "python.exe"):
        if cand.is_file():
            return str(cand)
    return sys.executable


# ───────────────────────────── состояние ─────────────────────────────

def load_state(p: Paths) -> dict:
    path = Path(p.state_file)
    if not path.exists():                 # отсутствие файла — норма
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise GateError(f"файл состояния не читается: {path}: {exc}", plan_id=p.plan_id) from exc
    return data if isinstance(data, dict) else {}


def save_state(p: Paths, state: dict) -> None:
    try:
        state_io.save_json(Path(p.state_file), state)
    except (OSError, ValueError, TypeError) as exc:
        raise GateError(f"состояние не записывается: {Path(p.state_file)}: {exc}",
                        plan_id=p.plan_id) from exc


def load_stop(p: Paths) -> dict | None:
    path = Path(p.stop_file)
    if not path.exists():                 # стоп-флага нет — норма
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise GateError(f"файл стоп-флага не читается: {path}: {exc}",
                        plan_id=p.plan_id) from exc
    return data if isinstance(data, dict) else {"reason": str(data)}


def write_stop(p: Paths, reason: str, source: str, kind: str | None = None) -> dict:
    """`kind=owner` — снимает только владелец; `kind=incident` — страж снимает сам."""
    payload = {"reason": reason, "source": source,
               "kind": kind or ("owner" if source == "owner" else "incident"),
               "at": time.strftime("%d.%m.%Y %H:%M:%S")}
    try:
        state_io.save_json(Path(p.stop_file), payload)
    except (OSError, ValueError, TypeError) as exc:
        raise GateError(f"стоп-флаг не записывается: {Path(p.stop_file)}: {exc}",
                        plan_id=p.plan_id) from exc
    return payload


def clear_stop(p: Paths) -> bool:
    path = Path(p.stop_file)
    existed = path.is_file()
    path.unlink(missing_ok=True)
    return existed


# ───────────────────────────── вердикт ─────────────────────────────

def review_path(p: Paths, step_id: str) -> Path:
    return Path(p.review_dir) / f"{p.review_prefix}-{step_id}.md"


def verdict_of(p: Paths, step_id: str) -> dict | None:
    """Разобрать отчёт ревьюера. None — отчёта нет; verdict=NONE/AMBIGUOUS — отчёт негоден."""
    path = review_path(p, step_id)
    if not path.is_file():
        return None
    text = path.read_text(encoding="utf-8", errors="replace")
    found = [m.group(1).upper() for m in VERDICT_RE.finditer(text)]
    unique = sorted(set(found))
    if not found:
        verdict = "NONE"
    elif len(unique) == 1:
        verdict = unique[0]
    else:
        verdict = "AMBIGUOUS"
    commit = COMMIT_RE.search(text)
    return {
        "path": str(path),
        "verdict": verdict,
        "occurrences": len(found),
        "sha": hashlib.sha256(text.encode("utf-8")).hexdigest()[:16],
        "commit": commit.group(1) if commit else None,
    }


# ───────────────────────────── журнал плана ─────────────────────────────

def _journal_rows(p: Paths) -> list[int]:
    """Индексы строк таблицы журнала: раздел ищет реестр планов, не страж."""
    lines = Path(p.plan_file).read_text(encoding="utf-8", errors="replace").splitlines()
    found = registry.journal_section(lines, p.cfg.journal_marker, p.step_re)
    return list(found[1]) if found else []


def journal_closed(p: Paths, step_id: str) -> bool:
    """Закрыт ли шаг в журнале своего плана (идентификатор — по выражению плана)."""
    path = Path(p.plan_file)
    if not path.is_file():
        return False
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    for i in _journal_rows(p):
        if registry.step_of_row(lines[i], p.step_re) != step_id.upper():
            continue
        if registry.status_of_row(lines[i]).startswith("ЗАКРЫТ"):
            return True
    return False


def append_journal_row(p: Paths, step_id: str, title: str, commit: str, evidence: str) -> bool:
    """Записать строку журнала. False — шаг уже закрыт (идемпотентно).

    Если строка шага уже есть (шаблон плана заводит их со статусом «открыт»), она
    ПЕРЕЗАПИСЫВАЕТСЯ, а не дублируется: два ряда на один шаг ломают чтение журнала.
    """
    path = Path(p.plan_file)
    if not path.is_file():
        raise SystemExit(f"нет файла плана: {path}")
    if journal_closed(p, step_id):
        return False
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    rows = _journal_rows(p)
    if not rows:
        raise SystemExit("в плане нет таблицы журнала — строка не добавлена")
    date = time.strftime("%d.%m.%Y")
    cells = [date, f"{step_id} ({title})", commit or "—", evidence or "ревью APPROVE", "ЗАКРЫТ"]
    row = "| " + " | ".join(c.replace("|", "/").replace("\n", " ") for c in cells) + " |"
    target = None
    for i in rows:
        if registry.step_of_row(lines[i], p.step_re) == step_id.upper():
            target = i
            break
    if target is not None:
        lines[target] = row
    else:
        lines.insert(rows[-1] + 1, row)
    state_io.atomic_write_text(path, "\n".join(lines) + ("\n" if text.endswith("\n") else ""))
    return True


def commit_and_push(p: Paths, files: list[str], message: str) -> tuple[bool, str]:
    """Закоммитить ТОЛЬКО перечисленные файлы; чужие правки в индексе остаются в индексе."""
    if not p.cfg.git_commit:
        return True, "(коммит выключен в pipeline.json)"
    status = _run(["git", "-c", "core.quotepath=off", "status", "--porcelain", "-z"], p.repo)
    entries = [e for e in status.stdout.split(chr(0)) if e.strip()]
    dirty = [e[3:].strip() for e in entries]
    allowed = {f.replace("\\", "/") for f in files}
    strangers = [d for d in dirty if d.replace("\\", "/") not in allowed]
    if strangers:
        print("[commit] в дереве чужие правки — не трогаю: " + ", ".join(strangers[:5]))
    add = _run(["git", "add", "--", *files], p.repo)
    if add.returncode != 0:
        return False, f"git add: {add.stderr.strip()[:200]}"
    diff = _run(["git", "diff", "--cached", "--name-only", "--", *files], p.repo)
    if diff.returncode != 0:
        return False, f"git diff: {diff.stderr.strip()[:200]}"
    changed = [ln.strip() for ln in diff.stdout.splitlines() if ln.strip()]
    if changed:
        commit = _run(["git", "commit", "-m", message, "--", *files], p.repo)
        if commit.returncode != 0:
            return False, f"git commit: {(commit.stderr or commit.stdout).strip()[:200]}"
    # sha читается один раз и уже ПОСЛЕ коммита: в ветке «нечего коммитить» HEAD не меняется,
    # поэтому одно чтение покрывает обе ветки, а detail всегда указывает на коммит шага.
    sha = _run(["git", "rev-parse", "--short", "HEAD"], p.repo).stdout.strip()
    detail = sha if changed else f"нечего коммитить: своих изменений нет (HEAD {sha})"
    if p.cfg.git_push:
        upstream = _run(["git", "rev-parse", "--abbrev-ref", "--symbolic-full-name",
                         "@{upstream}"], p.repo)
        if upstream.returncode != 0:
            detail += " · пуш пропущен: у ветки нет upstream"
        else:
            push = _run(["git", "push"], p.repo, timeout=180)
            if push.returncode != 0:
                return False, f"git push: {(push.stderr or push.stdout).strip()[:200]}"
            detail += " · пуш выполнен"
    return True, detail


# ───────────────────────────── доска ─────────────────────────────

def read_cards(p: Paths, key_prefix: str) -> list[dict]:
    db = Path(p.kanban_db)
    if not db.is_file():
        return []
    try:
        con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
        try:
            rows = con.execute(
                """SELECT id, title, status, assignee, idempotency_key, completed_at,
                          COALESCE(result,''), COALESCE(last_failure_error,'')
                   FROM tasks
                   WHERE idempotency_key LIKE ? AND status <> 'archived'
                   ORDER BY created_at ASC""",
                (key_prefix + "%",),
            ).fetchall()
        finally:
            con.close()
    except (sqlite3.Error, OSError) as exc:
        # Существующая база есть, а прочитать нельзя — это сбой, а не «карточек нет»:
        # иначе тик молча теряет ход конвейера.
        raise GateError(f"борда недоступна: {db}: {exc}", plan_id=p.plan_id) from exc
    return [{"id": r[0], "title": r[1], "status": r[2], "assignee": r[3], "key": r[4],
             "completed_at": int(r[5] or 0), "result": r[6], "error": r[7]} for r in rows]


def card_of(cards: list[dict], kind: str) -> list[dict]:
    """Карточки одного вида конвейера: `:<kind>` или `:<kind>:<номер>` в ключе."""
    pattern = re.compile(rf":{kind}(:\d+)?$")
    return [c for c in cards if pattern.search(c.get("key") or "")]


# ───────────────────────────── тексты карточек ─────────────────────────────

def _report_rel(p: Paths, step_id: str) -> str:
    return p.cfg.rel(Path(p.review_dir) / f"{p.review_prefix}-{step_id}.md")


def prompt_rel(p: Paths, step_id: str) -> str | None:
    """Постановка шага: `<prompts_dir>/<дата>_<план>-<шаг>_prompt.md` (дата любая)."""
    folder = Path(p.cfg.prompts_dir)
    if not folder.is_dir():
        return None
    suffix = f"_{p.plan_id}-{step_id}_prompt.md"
    matches = sorted(folder.glob(f"*{suffix}"))
    if not matches:
        return None
    return p.cfg.rel(matches[-1])


def step_card_body(p: Paths, step: dict, prompt: str | None = None) -> str:
    plan_id = p.plan_id
    plan_rel = p.cfg.rel(Path(p.plan_file))
    lines = [f"ШАГ ПЛАНА {plan_id} {step['id']} — {step['title']}", ""]
    if prompt:
        lines += [f"ПОСТАНОВКА (прочитать ПЕРВОЙ, до кода): `{prompt}`", ""]
    lines += [f"Цель: {step.get('goal', '')}", "", "Файлы (править/создавать):"]
    lines += [f"- {f}" for f in step.get("files") or []]
    lines += ["", "Гейты (запустить и показать фактический результат):"]
    lines += [f"- {g}" for g in step.get("gates") or []]
    lines += ["", f"Готово = {step.get('dod', '')}", "", "Что прочитать перед началом:"]
    if prompt:
        lines += [f"- {prompt} (постановка шага — границы, стоп-сигналы, формат отчёта)"]
    lines += [f"- {r}" for r in step.get("read") or []]
    if step.get("notes"):
        lines += ["", f"Важно: {step['notes']}"]
    lines += [
        "",
        "ЖЁСТКИЕ ПРАВИЛА",
        "- Работать только по этому шагу, не расширять объём и не начинать следующий.",
        "- Существующие тесты не удалять и не ослаблять; новые — happy path + границы + интеграция.",
        "- Вывод команд ≤ 200 строк: `pytest -q --tb=line`, `git status --porcelain | head -30`,",
        "  большой вывод — в файл и `grep` по нему (объём вывода = токены каждого хода).",
        "- Не перечитывать уже прочитанный файл: адресно `grep`/`read_file offset`.",
        "",
        "ЗАВЕРШЕНИЕ (обязательно, иначе конвейер встанет)",
        "1. Гейты зелёные — показать фактические цифры.",
        "2. Коммит и пуш ТОЛЬКО файлов шага: `git add <файлы шага>` (НЕ `git add -A`: в дереве",
        "   могут быть правки параллельной сессии), сообщение на русском, `git push`.",
        "3. Закрыть карточку через kanban_complete: что сделано, прогоны с цифрами, sha коммита.",
        "   Строку в журнал плана НЕ добавлять — её добавит страж после APPROVE ревьюера.",
        "",
        f"Контракт шага: `{plan_rel}` §{step['id']}.",
    ]
    return "\n".join(lines)


def _step_assignee(p: Paths, step: dict) -> str:
    """Профиль-исполнитель карточки: владелец шага, иначе драйвер из pipeline.json."""
    owner = str(step.get("owner") or "").strip()
    for token in re.split(r"[+,/]", owner):
        token = token.strip().strip("`")
        if re.fullmatch(r"[a-z][a-z0-9_-]{1,30}", token):
            return token
    return p.cfg.driver


def prompt_order_body(p: Paths, step: dict) -> str:
    """Тело карточки промптеру: заказ постановки, без которой шаг не начинается."""
    plan_rel = p.cfg.rel(Path(p.plan_file))
    step_id = step["id"]
    files = ", ".join(f"`{f}`" for f in (step.get("files") or [])) or "—"
    gates = "; ".join(f"`{g}`" for g in (step.get("gates") or [])) or "—"
    return "\n".join([
        f"ПОСТАНОВКА ШАГА {step_id} ПЛАНА {p.plan_id} — {step['title']}",
        "",
        f"Ты — роль `{p.cfg.prompter}`. Шаг не начинается, пока нет постановки.",
        "Задача — написать постановку и довести гейт формы до PASS.",
        "",
        "КОНТРАКТ ШАГА (из сборщика шагов, не менять):",
        f"- План и раздел: `{plan_rel}` §{step_id}",
        f"- Цель: {step.get('goal', '')}",
        f"- Файлы: {files}",
        f"- Гейты: {gates}",
        f"- Готово = {step.get('dod', '')}",
        "",
        "ЧТО СДЕЛАТЬ:",
        f"1. Прочитать §{step_id} плана и фактическое состояние кода (файлы выше, `git log -3 -- <файлы>`).",
        "2. Написать постановку по блокам шаблона (роль и цель; контекст с file:line; границы;",
        "   задачи; как проверить; стоп-сигналы; формат отчёта; вопросы; ревью).",
        "3. Раздел «Состояние на момент постановки»: что уже сделано, чего не хватает до «Готово =».",
        "4. Гейт: `python .pipeline/check_task_prompt.py <файл>` → `ИТОГ: PASS`.",
        "5. Коммит и пуш ТОЛЬКО файла постановки, затем kanban_complete: путь, результат гейта, sha.",
        "",
        "ЗАПРЕТЫ: не править код приложения, не запускать тесты и сборки, не менять план,",
        "не коммитить чужие файлы (`git add` перечислением, не `-A`).",
    ])


def review_card_body(p: Paths, step: dict, attempt: int, report_rel: str,
                     prompt: str | None = None) -> str:
    plan_id = p.plan_id
    plan_rel = p.cfg.rel(Path(p.plan_file))
    if prompt:
        prompt_block = [
            "СВЕРКА С ПОСТАНОВКОЙ (обязательный раздел отчёта)",
            f"- Постановка шага: `{prompt}` — прочитать её целиком.",
            "- Сверить итог по трём разрезам: «Что сделать» (каждый пункт — выполнен/нет, с file:line),",
            "  «Границы» (запрещённое — проверить, что НЕ сделано), «Формат отчёта».",
            "- Выполненное СВЕРХ постановки — находка (расширение объёма), а не плюс;",
            "  расхождение постановки с планом/кодом — отдельная находка с указанием, что верно.",
        ]
    else:
        prompt_block = [
            "СВЕРКА С ПОСТАНОВКОЙ (обязательный раздел отчёта)",
            "- Постановки шага в каталоге промтов НЕТ — зафиксировать это находкой",
            "  («шаг исполнен без постановки») и ревьюить только по §шага плана.",
        ]
    return "\n".join([
        f"РЕВЬЮ ШАГА {step['id']} плана {plan_id} — {step['title']} (круг {attempt})",
        "",
        f"Отчёт писать в: `{report_rel}` (перезаписать, если файл есть).",
        "",
        f"Источники — РОВНО два (роль `{p.cfg.reviewer}`): (1) раздел шага в плане",
        f"`{plan_rel}` §{step['id']}; (2) код — `git show` коммита шага, сами файлы, тесты.",
        "Отчёт исполнителя и его транскрипт — НЕ доказательство.",
        "",
        *prompt_block,
        "",
        "Гейты шага (прогнать САМОСТОЯТЕЛЬНО, свои цифры):",
        *[f"- {g}" for g in step.get("gates") or []],
        "",
        f"Готово = {step.get('dod', '')}",
        "",
        "ОБЯЗАТЕЛЬНЫЙ ФОРМАТ ОТЧЁТА",
        f"- Файл `{report_rel}` содержит РОВНО ОДНУ строку вердикта (заголовок допустим):",
        "  `## ВЕРДИКТ: APPROVE` либо `## ВЕРДИКТ: REQUEST_CHANGES`.",
        "  Две строки вердикта или цитата чужого вердикта = страж остановит конвейер (AMBIGUOUS).",
        "- Первая строка блока «Объект ревью» — коммит шага в обратных кавычках:",
        "  Объект ревью: коммит `<sha>`. Без него стражу нечего сверять.",
        "- Разбор каждого пункта «Готово =» с file:line, свои прогоны с цифрами, находки по",
        "  классам (блокирующая/значимая/стилистическая).",
        "",
        "ЗАПРЕТЫ: не править код, не коммитить, не пушить, не запускать полный набор тестов,",
        "не трогать данные приложения. Ревьюер выносит суждение — исправляет исполнитель.",
        "",
        "Закрыть карточку через kanban_complete: вердикт одной строкой + путь отчёта.",
    ])


def fix_card_body(p: Paths, step: dict, round_no: int, report_rel: str) -> str:
    plan_rel = p.cfg.rel(Path(p.plan_file))
    return "\n".join([
        f"ПРАВКИ ПО РЕВЬЮ шага {step['id']} плана {p.plan_id} — {step['title']} (круг {round_no})",
        "",
        f"Ревьюер вернул REQUEST_CHANGES. Отчёт: `{report_rel}`.",
        f"Контракт шага — `{plan_rel}` §{step['id']}, критерий: {step.get('dod', '')}",
        "",
        "ЧТО ДЕЛАТЬ",
        "- Исправить БЛОКИРУЮЩИЕ находки полностью; значимые — по существу (не косметикой).",
        "- Ничего не расширять за пределы шага; тесты не ослаблять и не удалять.",
        "- Гейты шага перегнать и показать фактические цифры:",
        *[f"  - {g}" for g in step.get("gates") or []],
        "",
        "ЗАВЕРШЕНИЕ",
        "1. Коммит и пуш ТОЛЬКО файлов правок (`git add <файлы>`, НЕ `-A`), сообщение на русском.",
        "2. kanban_complete: какие находки закрыты (по номерам), прогоны с цифрами, sha коммита.",
        "   Журнал плана не трогать. Следующий круг ревью ставит страж.",
        "",
        "ВАЖНО: это последний автоматический круг правок. Второй REQUEST_CHANGES останавливает",
        "конвейер и уходит владельцу — доведи находки до конца.",
    ])


def fix_comment_body(p: Paths, step: dict, round_no: int, report_rel: str,
                     review_card_id: str | None) -> str:
    """Комментарий к карточке правок: находки ревьюера там, где идёт работа (S2)."""
    report = Path(p.repo) / report_rel
    lines = [
        f"ПРАВКИ ПО РЕВЬЮ: шаг {step['id']} плана {p.plan_id} — {step['title']} "
        f"(круг {round_no})",
        f"Отчёт ревью: {report_rel}",
        f"Карточка ревью: {review_card_id or '—'}",
        "",
    ]
    blocking = _blocking_findings(report, FINDINGS_LIMIT)
    if blocking:
        lines.append("Блокирующие находки — закрыть полностью, ответить на каждую:")
        lines += [f"{num}. {finding}" for num, finding in enumerate(blocking, 1)]
    else:
        others = _review_findings(report, 3)
        if others:
            lines.append("Блокирующих находок с пометкой в отчёте нет — замечания из отчёта:")
            lines += [f"{num}. {finding}" for num, finding in enumerate(others, 1)]
        else:
            lines.append("Отчёт без пунктов: разбирать целиком.")
    lines += ["",
              "Ответ на находку — комментарий в этой карточке по её номеру; "
              "отчёт ревью не переписывать."]
    return "\n".join(lines)[:COMMENT_MAX_LEN]


# ───────────────────────────── решения ─────────────────────────────

def collector(p: Paths, step: str | None = None) -> dict:
    cmd = [_python(p), str(Path(__file__).resolve().parent / "next_step.py"),
           "--plan", p.plan_id, "--json"]
    if step:
        cmd += ["--step", step]
    res = _run(cmd, Path(p.repo), timeout=120)
    if res.returncode != 0:
        raise SystemExit(f"next_step.py упал: {(res.stderr or res.stdout)[-400:]}")
    return json.loads(res.stdout)


def build_ctx(p: Paths) -> dict:
    state = load_state(p)
    ctx: dict = {"state": state, "stop": load_stop(p), "hold": list(state.get("hold_steps") or []),
                 # Шаги, допуск по которым владелец уже снимал: автоматический риск-hold
                 # по ним не ставится повторно (D4).
                 "released": list(state.get("hold_released") or [])}
    ctx["plan_id"] = p.plan_id
    ctx["review_prefix"] = p.review_prefix
    # Файл плана: причина hold обязана ссылаться на раздел плана, а у `decide` нет `Paths`.
    ctx["plan_file"] = p.cfg.rel(Path(p.plan_file))
    # Утверждение считаем ДО сборщика: при его сбое решение всё равно должно знать,
    # что план не утверждён.
    approval = registry.approval_status(p.cfg, p.spec)
    approval["started"] = registry.plan_started(p.cfg, p.spec)
    ctx["approval"] = approval
    try:
        pkg = collector(p)
    except SystemExit as exc:
        ctx.update(next=None, collector_error=str(exc), cards=[], verdict=None,
                   journal_closed=False, prev_closed=None, prev_verdict=None)
        return ctx
    step = pkg.get("next")
    ctx["next"] = step
    ctx["steps_total"] = int(pkg.get("steps_total") or 0)
    ctx["no_steps"] = bool(pkg.get("steps_total") is not None and ctx["steps_total"] == 0)
    ctx["note"] = pkg.get("note") or ""
    ctx["closed"] = [s.upper() for s in (pkg.get("closed") or [])]
    ctx["prev_closed"] = ctx["closed"][-1] if ctx["closed"] else None
    ctx["prev_verdict"] = verdict_of(p, ctx["prev_closed"]) if ctx["prev_closed"] else None
    ctx["verdict"] = verdict_of(p, step["id"]) if step else None
    ctx["journal_closed"] = bool(step) and journal_closed(p, step["id"])
    ctx["cards"] = read_cards(p, f"planloop:{p.plan_id}:{step['id']}:") if step else []
    ctx["prev_cards"] = (read_cards(p, f"planloop:{p.plan_id}:{ctx['prev_closed']}:")
                         if ctx.get("prev_closed") else [])
    return ctx


def stop_kind(stop: dict | None) -> str:
    if not stop:
        return ""
    return stop.get("kind") or ("owner" if stop.get("source") == "owner" else "incident")


def decide(ctx: dict) -> dict:
    """Чистое решение по состоянию: одно действие (или ожидание). Без побочных эффектов.

    Стоп-флаг владельца блокирует всё. Инцидентный флаг (его ставит сам страж) держит
    конвейер, но снимается автоматически, как только причина исчезла.
    """
    stop = ctx.get("stop")
    if stop and stop_kind(stop) == "owner":
        return {"action": "idle", "reason": f"стоп-флаг владельца: {stop.get('reason', '')}"}
    base = _decide_base(ctx)
    if stop:
        if base["action"] == "stop":
            return {"action": "idle", "incident": True,
                    "reason": f"инцидент ждёт устранения: {base.get('reason', '')}"}
        return dict(base, clear_incident=True,
                    reason=f"стоп-причина снята (было: {stop.get('reason', '')}); "
                           f"дальше — {base['action']}")
    return base


def _decide_base(ctx: dict) -> dict:
    """Решение без учёта стоп-флага."""
    step = ctx.get("next")
    if not step:
        if ctx.get("collector_error"):
            return {"action": "stop",
                    "reason": f"сборщик шагов не работает: {ctx['collector_error'][:200]}"}
        if ctx.get("no_steps"):
            return {"action": "idle",
                    "reason": f"у плана {ctx.get('plan_id')} не распознан раздел шагов — "
                              f"конвейер его не ведёт"}
        return {"action": "idle", "reason": "план закрыт — следующих шагов нет", "plan_done": True}
    sid = step["id"]
    approval = ctx.get("approval") or {}
    if approval.get("state") in ("missing", "changed") and not approval.get("started"):
        # Неутверждённый план не заказывает карточку. Решение именно `idle`, а не
        # отдельное действие: иначе пропуск съедал бы единственное действие тика (D5)
        # и вытеснял остальные планы на каждом тике.
        return {"action": "idle", "step": sid,
                "reason": approval.get("reason") or "план не утверждён",
                "approval": approval.get("state"), "approval_blocked": True}
    prev, prev_verdict = ctx.get("prev_closed"), ctx.get("prev_verdict")
    if prev and prev_verdict and prev_verdict["verdict"] == "REQUEST_CHANGES":
        # Журнал закрыл шаг, а ревью говорит «нужны правки»: вперёд идти нельзя.
        # Даём ОДИН автоматический повторный круг ревью.
        prev_cards = ctx.get("prev_cards") or []
        rereviews = [c for c in prev_cards if (c.get("key") or "").endswith(":rereview:1")]
        prev_fix = card_of(prev_cards, "fix")
        if not rereviews:
            if prev_fix and prev_fix[-1]["status"] not in TERMINAL:
                return {"action": "idle",
                        "reason": f"правки шага {prev} в работе: карточка {prev_fix[-1]['id']} "
                                  f"({prev_fix[-1]['status']})"}
            if prev_fix and prev_fix[-1]["status"] != "done":
                detail = (prev_fix[-1].get("error") or prev_fix[-1].get("result") or "")[:300]
                return {"action": "stop", "step": sid,
                        "reason": f"карточка правок {prev_fix[-1]['id']} в статусе "
                                  f"{prev_fix[-1]['status']}: {detail}"}
            prev_steps = card_of(prev_cards, "step")
            parent = (prev_fix[-1]["id"] if prev_fix
                      else (prev_steps[-1]["id"] if prev_steps else None))
            return {"action": "create_rereview", "step": prev, "verdict": prev_verdict,
                    "parent": parent,
                    "reason": f"шаг {prev} закрыт при REQUEST_CHANGES — ставится повторное ревью"}
        if rereviews[-1]["status"] not in TERMINAL:
            return {"action": "idle",
                    "reason": f"повторное ревью шага {prev} идёт: карточка {rereviews[-1]['id']}"}
        return {"action": "stop", "step": sid,
                "reason": f"повторное ревью шага {prev} снова REQUEST_CHANGES "
                          f"({Path(prev_verdict['path']).name}) — нужно решение владельца"}
    if sid.upper() in {str(h).upper() for h in (ctx.get("hold") or [])}:
        return {"action": "idle", "reason": f"шаг {sid} в списке ручного допуска владельца"}

    cards = ctx.get("cards") or []
    steps = card_of(cards, "step")
    if not steps:
        # Рискованный шаг не получает НИ ОДНОЙ карточки (включая заказ постановки),
        # пока владелец не допустил: проверка стоит внутри «карточки шага ещё нет»,
        # иначе уже начатый шаг застрял бы на hold и не дошёл до ревью (D5).
        risk = str(step.get("risk") or "normal").lower()
        held = {str(h).upper() for h in (ctx.get("hold") or [])}
        released = {str(r).upper() for r in (ctx.get("released") or [])}
        # ANCHOR:hold_risk_decision — стабильная ссылка для спеки и ревью
        if risk == "high" and sid.upper() not in held and sid.upper() not in released:
            where = ctx.get("plan_file") or f"план {ctx.get('plan_id') or '?'}"
            return {"action": "hold_risk", "step": sid, "data": step, "risk": risk,
                    "reason": (f"шаг {sid} помечен «Риск: high» — нужен допуск владельца; "
                               f"раздел плана: `{where}` §{sid}; снять: "
                               f"`python .pipeline/gate.py --unhold {sid}`")}
        return {"action": "create_step", "step": sid, "data": step}
    last_step = steps[-1]
    if last_step["status"] not in TERMINAL:
        return {"action": "idle", "reason": f"шаг {sid} в работе: карточка {last_step['id']} "
                                           f"({last_step['status']})"}
    if last_step["status"] != "done":
        detail = (last_step.get("error") or last_step.get("result") or "")[:300]
        return {"action": "stop", "step": sid,
                "reason": f"карточка шага {last_step['id']} в статусе {last_step['status']}: {detail}"}

    fixes = card_of(cards, "fix")
    attempt = len(fixes) + 1
    this_fix = [c for c in fixes if (c.get("key") or "").endswith(f":fix:{attempt - 1}")]
    reviews = [c for c in card_of(cards, "review")
               if (c.get("key") or "").endswith(f":review:{attempt}")]
    if not reviews:
        if this_fix and this_fix[-1]["status"] not in TERMINAL:
            return {"action": "idle",
                    "reason": f"правки шага {sid} в работе: карточка {this_fix[-1]['id']} "
                              f"({this_fix[-1]['status']})"}
        if this_fix and this_fix[-1]["status"] != "done":
            detail = (this_fix[-1].get("error") or this_fix[-1].get("result") or "")[:300]
            return {"action": "stop", "step": sid,
                    "reason": f"карточка правок {this_fix[-1]['id']} в статусе "
                              f"{this_fix[-1]['status']}: {detail}"}
        return {"action": "create_review", "step": sid, "data": step, "attempt": attempt,
                "parent": this_fix[-1]["id"] if this_fix else last_step["id"]}
    last_review = reviews[-1]
    if last_review["status"] not in TERMINAL:
        return {"action": "idle", "reason": f"ревью {sid} идёт: карточка {last_review['id']}"}
    if last_review["status"] != "done":
        detail = (last_review.get("error") or last_review.get("result") or "")[:300]
        return {"action": "stop", "step": sid,
                "reason": f"ревью {sid} упало ({last_review['status']}): {detail}"}

    verdict = ctx.get("verdict")
    if verdict is None:
        prefix = ctx.get("review_prefix") or "?"
        return {"action": "stop", "step": sid,
                "reason": f"ревью {sid} закрыто, но отчёта {prefix}-{sid}.md нет"}
    if verdict["verdict"] in ("NONE", "AMBIGUOUS"):
        what = "строки вердикта нет" if verdict["verdict"] == "NONE" else "строк вердикта несколько"
        return {"action": "stop", "step": sid,
                "reason": f"вердикт {sid} не распознан ({what}): {Path(verdict['path']).name}"}
    if verdict["verdict"] == "APPROVE":
        return {"action": "close_step", "step": sid, "data": step, "verdict": verdict,
                "journal_closed": ctx.get("journal_closed")}
    if not fixes:
        return {"action": "create_fix", "step": sid, "data": step, "round": 1,
                "parent": last_review["id"]}
    findings = _review_findings(Path(verdict["path"]), 5)
    reason = f"второй REQUEST_CHANGES по шагу {sid} — автоматический круг правок исчерпан"
    details = [f"Замечания ревью ({Path(verdict['path']).name}):"]
    details += [f"  - {f}" for f in findings] or ["  - пунктов в отчёте не нашёл, смотри отчёт целиком"]
    recs = _review_recommendations(Path(verdict["path"]), 3)
    if recs:
        details += ["Рекомендации ревьюера:"] + [f"  - {r}" for r in recs]
    details += [
        "Что делать (выбери одно):",
        "  1) принять как есть, если замечания непринципиальны: gate.py --resume и закрыть шаг;",
        "  2) переписать постановку шага в docs/prompts/ и вернуть шаг в работу;",
        "  3) доправить вручную по замечаниям и запустить тик: ревью пойдёт заново.",
        "Стоп инцидентный: снимется сам, как только появится вердикт APPROVE.",
    ]
    return {"action": "stop", "step": sid, "reason": reason, "details": chr(10).join(details)}


def _report_items(text: str) -> list[tuple[str, bool]]:
    """Пункты списков отчёта: нормализованный текст и «идёт под разделом про блокирующие»."""
    items: list[tuple[str, bool]] = []
    under_blocking = False
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("|"):
            continue
        if line.startswith("#"):
            # Заголовок переключает флаг: подраздел «Блокирующие» — да, любой другой — нет.
            under_blocking = "блокирующ" in line.lower()
            continue
        if line[:2] in ("- ", "* ") or (line[:1].isdigit() and line[1:3] in (". ", ") ")):
            item = line[2:].strip() if line[:2] in ("- ", "* ") else line[3:].strip()
            item = (item.replace("`", "").replace("**", "").replace("__", "")
                    .strip(" .;"))
            if len(item) >= 12:
                items.append((item, under_blocking))
    return items


def _review_findings(path: Path, limit: int = 5) -> list[str]:
    """Выжимка замечаний из отчёта ревью: пункты списков без воды."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    items: list[str] = []
    for item, _ in _report_items(text):
        short = item[:180]
        if short not in items:
            items.append(short)
        if len(items) >= limit:
            break
    return items


def _blocking_findings(path: Path, limit: int = 5) -> list[str]:
    """Блокирующие находки отчёта: метка в самом пункте ИЛИ подраздел «Блокирующие»."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    items: list[str] = []
    for item, under_blocking in _report_items(text):
        if not under_blocking and "блокирующ" not in item.lower():
            continue
        short = item[:180]
        if short not in items:
            items.append(short)
        if len(items) >= limit:
            break
    return items


def _review_recommendations(path: Path, limit: int = 3) -> list[str]:
    """Раздел «Рекомендации» из отчёта ревью: что владельцу делать, а не что не так."""
    items = _review_findings(path, 50)
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    picked: list[str] = []
    inside = False
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("#"):
            inside = "рекомендац" in line.lower()
            continue
        if not inside or not line:
            continue
        if line[:2] in ("- ", "* ") or (line[:1].isdigit() and line[1:3] in (". ", ") ")):
            item = (line[2:] if line[:2] in ("- ", "* ") else line[3:]).strip()
            item = item.replace("`", "").strip(" .;")
            if len(item) >= 8 and item not in picked:
                picked.append(item[:200])
        if len(picked) >= limit:
            break
    return picked


def _step_summary(step: dict, limit: int = 260) -> str:
    """Суть шага одной фразой: цель, а если её нет — критерий готовности."""
    def norm(text: str) -> str:
        return " ".join(str(text or "").replace("`", "").split())

    for source in ("goal", "dod"):
        text = norm(step.get(source))
        if not text:
            continue
        cut = text.find(". ")
        if 0 < cut < limit:
            return text[:cut + 1]
        return text[:limit].rstrip(" ,;") + ("…" if len(text) > limit else "")
    return norm(step.get("title"))[:limit]

def status_text(ctx: dict, decision: dict) -> str:
    step = ctx.get("next") or {}
    plan_id = ctx.get("plan_id") or "?"
    lines = [
        f"План {plan_id}: следующий открытый шаг — {step.get('id') or 'нет'} {step.get('title') or ''}",
        f"Закрыто в журнале: {', '.join(ctx.get('closed') or []) or '—'}",
        f"Карточек конвейера: {len(ctx.get('cards') or [])}",
    ]
    for c in ctx.get("cards") or []:
        lines.append(f"  - {c['id']} {c['status']:<10} {c['key']}")
    # Риск и ручной допуск видны всегда: по ним владелец решает, снимать ли hold.
    hold = [str(h).upper() for h in (ctx.get("hold") or [])]
    lines.append(f"Ручной допуск (hold): {', '.join(hold) or '—'}")
    # Шага нет — и риска нет: «normal» у пустой строки читается как «риск обычный».
    lines.append(f"Риск шага: {((step.get('risk') or 'normal') if step else '—')}")
    if ctx.get("verdict"):
        v = ctx["verdict"]
        lines.append(f"Вердикт: {v['verdict']} (строк: {v['occurrences']}, "
                     f"коммит: {v['commit'] or '—'})")
    else:
        lines.append("Вердикт: отчёта нет")
    if ctx.get("prev_verdict"):
        lines.append(f"Прошлый закрытый шаг {ctx.get('prev_closed')}: "
                     f"вердикт {ctx['prev_verdict']['verdict']}")
    if ctx.get("stop"):
        lines.append(f"СТОП-ФЛАГ: {ctx['stop'].get('reason')} (с {ctx['stop'].get('at')})")
    lines.append(f"Решение: {decision['action']} — {decision.get('reason', '')}")
    return "\n".join(lines)


# ───────────────────────────── побочные действия ─────────────────────────────

def _comment(p: Paths, card_id: str, text: str) -> bool:
    """Комментарий в карточку борды. Отказ — предупреждение `[comment]`, не исключение (S2)."""
    # Тело уходит одной строкой: обёртка CLI `.cmd` на Windows режет командную строку по
    # переводу строки и молча теряет хвост, а тела из файла у `comment` нет. Пункты
    # разделяются «; » — содержание и адреса находок сохраняются полностью.
    lines = [" ".join(line.split()) for line in text.splitlines()]
    text = "; ".join(line for line in lines if line)
    if not card_id or not text:
        return False
    cmd = [_hermes(), "kanban", "--board", p.cfg.board, "comment", card_id, text,
           "--author", COMMENT_AUTHOR, "--max-len", str(COMMENT_MAX_LEN)]
    try:
        res = _run(cmd, Path(p.repo), timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        # Нет `hermes` в PATH или команда не ответила: карточка уже создана, тик живёт.
        print(f"[comment] не отправлен в {card_id}: {str(exc)[:300]}", file=sys.stderr)
        return False
    if res.returncode != 0:
        out = ((res.stdout or "") + (res.stderr or "")).strip()
        print(f"[comment] не отправлен в {card_id}: {out[:300]}", file=sys.stderr)
        return False
    return True


def _create_card(p: Paths, title: str, body: str, assignee: str, key: str,
                 skills: tuple[str, ...], parent: str | None = None,
                 max_runtime: str | None = None) -> str | None:
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as fh:
        fh.write(body)
        body_path = fh.name
    cmd = [_hermes(), "kanban", "--board", p.cfg.board,
           "create", title, "--body-file", body_path,
           "--assignee", assignee, "--tenant", p.cfg.tenant,
           "--workspace", f"dir:{Path(p.repo).as_posix()}",
           "--idempotency-key", key,
           "--max-runtime", max_runtime or p.cfg.step_runtime]
    for skill in skills:
        cmd += ["--skill", skill]
    if parent:
        cmd += ["--parent", parent]
    res = _run(cmd, Path(p.repo), timeout=180)
    out = (res.stdout or "") + (res.stderr or "")
    m = re.search(r"t_[0-9a-f]{6,}", out)
    if res.returncode != 0 and not m:
        print(f"[card] не создано: {out.strip()[:300]}", file=sys.stderr)
        return None
    return m.group(0) if m else None


def notify(cfg: config_mod.Config, text: str) -> bool:
    """Уведомление владельцу. Выключено — печатаем (cron --deliver покажет в выводе)."""
    prefix = str(cfg.notify.get("prefix") or "")
    msg = f"{prefix}{text}" if prefix else text
    if not cfg.notify.get("enabled"):
        # stderr, не stdout: `--json` должен остаться чистым JSON для UI и инструментов
        print(f"[notify] {msg}", file=sys.stderr)
        return True
    token = os.environ.get(str(cfg.notify.get("token_env") or "PIPELINE_TG_TOKEN"))
    chat = os.environ.get(str(cfg.notify.get("chat_env") or "PIPELINE_TG_CHAT"))
    if not token or not chat:
        print(f"[notify] нет токена/чата, сообщение не отправлено: {msg}", file=sys.stderr)
        return False
    data = urllib.parse.urlencode({"chat_id": chat, "text": msg,
                                   "disable_web_page_preview": "true"}).encode()
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    for attempt in range(3):
        try:
            with urllib.request.urlopen(url, data=data, timeout=20) as resp:
                if 200 <= resp.status < 300:
                    return True
        except (urllib.error.URLError, OSError) as exc:
            if attempt == 2:
                print(f"[notify] не доставлено: {exc}", file=sys.stderr)
        time.sleep(2)
    return False


# ───────────────── сторож простоя и дневная сводка ─────────────────

WATCHDOG_DEFAULT_HOURS = 3          # умолчание §S9, ключ notify.watchdog_hours
WATCHDOG_NOTICE_GAP = 3600          # одно уведомление на причину — не чаще раза в час
DIGEST_DEFAULT = "09:00"            # умолчание §S9, ключ notify.digest_at
DIGEST_FILE = "digest.json"         # отметка «сводка за сутки отправлена» (как tg_offset.json)


def _now() -> float:
    """Текущее время (epoch). Единственное чтение часов в новом коде.

    Отдельная функция, а не time.time() на месте: тесты подменяют gate._now, и без
    единой точки подмены «сутки простоя» иначе не проверить.
    """
    return time.time()


def _local_date(ts: float) -> str:
    """Дата от переданного момента: формат журнала, 02.11.2026."""
    return time.strftime("%d.%m.%Y", time.localtime(ts))


def _local_stamp(ts: float) -> str:
    """Момент для сообщения владельцу: 02.11.2026 03:00."""
    return time.strftime("%d.%m.%Y %H:%M", time.localtime(ts))


def _ago(seconds: float) -> str:
    """Длительность по-русски: «4 ч 10 мин», «4 ч», «45 мин», «меньше минуты»."""
    total = max(0, int(seconds))
    hours, minutes = total // 3600, total % 3600 // 60
    if hours and minutes:
        return f"{hours} ч {minutes} мин"
    if hours:
        return f"{hours} ч"
    if minutes:
        return f"{minutes} мин"
    return "меньше минуты"


def _positive(raw, default: float) -> float:
    """Число из pipeline.json; битое или неположительное значение — умолчание (D4)."""
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def max_actions_of(cfg: config_mod.Config) -> int:
    """Предел действий за тик из pipeline.json; нет ключа или значение битое — 1 (D1).

    Ключ читается сырым словарём конфига (`cfg.state`, D3): типизированного поля в
    `Config` нет, и шаг не выходит за свои файлы. Исключений не бросает — битый конфиг
    не должен валить тик (сравни `_positive`).
    """
    raw = (cfg.state or {}).get("max_actions")
    if raw is None or isinstance(raw, bool):
        return MAX_ACTIONS_DEFAULT
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return MAX_ACTIONS_DEFAULT
    return value if value >= 1 else MAX_ACTIONS_DEFAULT


def _digest_at(cfg: config_mod.Config) -> tuple[int, int]:
    """Час и минута сводки из notify.digest_at; битое значение — умолчание (D4)."""
    raw = str(cfg.notify.get("digest_at") or DIGEST_DEFAULT).strip()
    match = re.match(r"^(\d{1,2}):(\d{2})$", raw)
    if not match:
        match = re.match(r"^(\d{1,2}):(\d{2})$", DIGEST_DEFAULT)
    hour, minute = int(match.group(1)), int(match.group(2))
    return (hour, minute) if hour < 24 and minute < 60 else (9, 0)


def watch_idle(cfg: config_mod.Config, p: Paths, ctx: dict, decision: dict,
               say=None) -> dict:
    """Простой: запомнить ожидание и разбудить владельца, если ждём дольше watchdog_hours.

    waiting_since ставится один раз за полосу простоя и снимается первым же действием
    (см. touch). Уведомление — одно на причину, не чаще раза в час (D6). Закрытый план
    простоем не считается: там работы нет по определению (D10).
    """
    if decision.get("plan_done"):
        return {"notice": False, "waiting_since": None}
    state = ctx.get("state") or {}
    now = _now()
    reason = str(decision.get("reason") or "нет работы")
    started = not isinstance(state.get("waiting_since"), (int, float))
    since = float(now if started else state["waiting_since"])
    changed = state.get("waiting_reason") != reason
    state["waiting_since"] = since
    state["waiting_reason"] = reason
    hours = _positive(cfg.notify.get("watchdog_hours"), WATCHDOG_DEFAULT_HOURS)
    notice_at = state.get("watchdog_notice_at")
    gap_passed = (not isinstance(notice_at, (int, float))
                  or now - float(notice_at) >= WATCHDOG_NOTICE_GAP)
    notice = now - since >= hours * 3600 and (changed or gap_passed)
    if notice:
        text = (f"⏳ простой {_ago(now - since)}: {reason} "
                f"(план {p.plan_id}, ждём с {_local_stamp(since)})")
        (say or (lambda value: notify(cfg, value)))(text)
        state["watchdog_notice_at"] = now
        state["watchdog_notice_reason"] = reason
    if started or changed or notice:
        save_state(p, state)
    return {"notice": notice, "waiting_since": since}


APPROVAL_NOTICE_KEY = registry.APPROVAL_NOTICE_KEY


def inherit_approval(p: Paths, approval: dict) -> dict:
    """Записать унаследованную запись утверждения для уже идущего плана.

    Ключи — те же, что у `plan_accept._write_approval`, плюс `inherited`: два формата
    одной записи недопустимы.
    """
    record = {
        "plan": str(p.plan_id),
        "file": p.cfg.rel(Path(p.plan_file)),
        "sha256": approval.get("sha256") or registry.sha256_file(p.plan_file),
        "approved_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "profile": os.environ.get("HERMES_PROFILE") or "default",
        "inherited": True,
    }
    path = registry.approval_file_for(p.cfg, p.plan_id)
    try:
        state_io.save_json(path, record)
    except OSError as exc:
        raise GateError(f"запись утверждения не пишется: {path}: {exc}", p.plan_id) from exc
    return record


def ensure_approval(p: Paths, ctx: dict) -> dict | None:
    """Дочитать утверждение для идущего плана: одна запись и одна строка в stdout.

    Только исполняющий проход (`--run`): `--status` состояние не меняет. Повторный
    вызов после записи ничего не пишет — в ctx уже `approved`.
    """
    approval = ctx.get("approval") or {}
    if approval.get("state") == "approved" or not approval.get("started"):
        return None
    record = inherit_approval(p, approval)
    approval.update(state="approved", inherited=True, record=record, reason="")
    ctx["approval"] = approval
    print(f"план {p.plan_id}: запись утверждения унаследована "
          f"(inherited, sha256 {str(record.get('sha256') or '')[:12]})")
    return record


def approval_notice(p: Paths, ctx: dict, decision: dict, say=None) -> bool:
    """Одно уведомление владельцу на план о пропуске по утверждению.

    Отметка в состоянии плана (`approval_notice`) не зависит от `notify.enabled` —
    так же работают сторож простоя и сводка. `touch()` этот ключ не снимает, поэтому
    «одно на план» держится и после действий по другим шагам.
    """
    if not decision.get("approval_blocked"):
        return False
    state = ctx.get("state") or {}
    if state.get(APPROVAL_NOTICE_KEY):
        return False
    reason = str(decision.get("reason") or "план не утверждён")
    (say or (lambda text: notify(p.cfg, text)))(
        f"⛔ план {p.plan_id} не ведётся: {reason}")
    state[APPROVAL_NOTICE_KEY] = {
        "at": time.strftime("%d.%m.%Y %H:%M:%S"),
        "state": decision.get("approval") or "missing",
        "reason": reason[:300],
    }
    ctx["state"] = state
    save_state(p, state)
    return True


def digest_rows(cfg: config_mod.Config) -> list[dict]:
    """Строка сводки по каждому ведомому плану: закрыто за сутки, ожидание, стоп.

    «Закрыто за сутки» — карточки шагов со completed_at в последних 24 часах (D8):
    дата журнала хранится без времени и для суточного окна не годится.
    """
    now = _now()
    rows: list[dict] = []
    for spec in registry.driven_specs(cfg):
        p = resolve_paths(cfg, spec.id)
        state = load_state(p)
        closed = 0
        for card in read_cards(p, f"planloop:{spec.id}:"):
            done_at = float(card.get("completed_at") or 0)
            if (str(card.get("key") or "").endswith(":step")
                    and card.get("status") == "done" and 0 <= now - done_at <= 86400):
                closed += 1
        rows.append({"id": spec.id, "closed": closed,
                     "waiting_since": state.get("waiting_since"),
                     "waiting_reason": state.get("waiting_reason"),
                     "stop": load_stop(p)})
    return rows


def digest_text(cfg: config_mod.Config, rows: list[dict]) -> str:
    """Текст суточной сводки: шапка, сколько закрыто, строка на план."""
    now = _now()
    lines = [f"📊 сводка за сутки, {_local_date(now)}",
             f"закрыто за сутки: {sum(row['closed'] for row in rows)}"]
    for row in rows:
        waiting = "ничего"
        if isinstance(row.get("waiting_since"), (int, float)):
            waiting = (f"{row.get('waiting_reason') or 'ждём'} "
                       f"({_ago(now - float(row['waiting_since']))})")
        stop = "нет"
        if row.get("stop"):
            stop = f"{stop_kind(row['stop'])}: {row['stop'].get('reason', '')}"
        lines.append(f"план {row['id']}: закрыто {row['closed']}, "
                     f"ждёт — {waiting}, стоп — {stop}")
    return "\n".join(lines)


def send_digest(cfg: config_mod.Config, say=None) -> bool:
    """Суточная сводка: одна на проект, после notify.digest_at, отметка в state_dir (D2, D7)."""
    marker = Path(cfg.state_dir) / DIGEST_FILE
    try:
        sent = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        sent = {}
    now = _now()
    today = _local_date(now)
    if isinstance(sent, dict) and sent.get("date") == today:
        return False
    local = time.localtime(now)
    if (local.tm_hour, local.tm_min) < _digest_at(cfg):
        return False
    rows = digest_rows(cfg)
    if not rows:                       # ведомых планов нет — сообщать нечего
        return False
    (say or (lambda value: notify(cfg, value)))(digest_text(cfg, rows))
    state_io.save_json(marker, {"date": today, "at": _local_stamp(now)})
    return True


# ───────────────────── команды владельцу (Telegram, вход) ─────────────────────

TG_TEXT_LIMIT = 3900

TG_HELP = (
    "Команды конвейера:\n"
    "/status — состояние: решение стража, стоп-флаг и таблица планов\n"
    "/plans — только таблица планов\n"
    "/stop [причина] — остановиться после текущего шага\n"
    "/resume — снять стоп-флаг\n"
    "/hold S3 — шаг требует ручного допуска\n"
    "/unhold S3 — снять ручной допуск\n"
    "/reset — начать диалог с агентом заново\n"
    "Свободный текст без «/» уходит агенту: он ответит в этот же чат.\n"
    "Работают только из чата, указанного в настройках."
)


def _tg_call(token: str, method: str, http_timeout: int = 20, **params) -> dict:
    """Один вызов Bot API. Любой сбой — {"ok": False, "description": ...}, без исключений."""
    body = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None}).encode()
    url = f"https://api.telegram.org/bot{token}/{method}"
    try:
        with urllib.request.urlopen(url, data=body, timeout=http_timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace") or "{}")
            return data if isinstance(data, dict) else {"ok": False, "description": "пустой ответ"}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:200]
        return {"ok": False, "error_code": exc.code, "description": detail}
    except (urllib.error.URLError, OSError) as exc:
        return {"ok": False, "description": str(exc)[:200]}


def tg_target(cfg: config_mod.Config) -> tuple[str, str] | None:
    """Токен и чат из окружения (имена переменных — из pipeline.json)."""
    token = os.environ.get(str(cfg.notify.get("token_env") or "PIPELINE_TG_TOKEN"))
    chat = os.environ.get(str(cfg.notify.get("chat_env") or "PIPELINE_TG_CHAT"))
    if not token or not chat:
        return None
    return str(token), str(chat)


def commands_enabled(cfg: config_mod.Config) -> bool:
    """Приём команд: по умолчанию включён там, где включены уведомления."""
    commands = cfg.notify.get("commands")
    if isinstance(commands, dict):
        return bool(commands.get("enabled", True))
    return bool(cfg.notify.get("enabled"))


def _offset_path(cfg: config_mod.Config) -> Path:
    return Path(cfg.state_dir) / "tg_offset.json"


def _read_offset(path: Path) -> int:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return int(data.get("offset") or 0)
    except (OSError, json.JSONDecodeError, ValueError, TypeError):
        return 0


def _write_offset(path: Path, offset: int) -> None:
    try:
        state_io.save_json(path, {"offset": int(offset)})
    except OSError as exc:
        print(f"[commands] offset не сохранён: {exc}", file=sys.stderr)


# ───────────────────────── диалог с агентом (S4) ─────────────────────────

DIALOG_STATE_NAME = "tg_dialog.json"
COMMANDS_OFF_NAME = "tg_commands_off.json"

# Умолчания берутся из `notify.dialog` (config.py), чтобы числа шага жили в одном месте:
# сюда попадает только запасной вариант для конфига, собранного до S4 (ключа `dialog` там
# ещё нет), и `DEFAULTS` остаётся единственным источником значений.
DIALOG_DEFAULTS = {
    "enabled": True,
    "max_turns": 8,
    "run_budget": 200,
    "timeout": 240,
    "session_max_messages": 40,
    "session_max_tokens": 120000,
    "tokens_per_char": 0.34,
    "toolsets": "web,file,skills,todo,memory,session_search",
}


def _as_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def dialog_settings(cfg: config_mod.Config) -> dict:
    """Числа диалога: умолчания D3 поверх `notify.dialog`; мусор в конфиге не ломает."""
    raw = cfg.notify.get("dialog") if isinstance(cfg.notify, dict) else None
    raw = raw if isinstance(raw, dict) else {}
    defaults = DIALOG_DEFAULTS
    return {
        "enabled": raw.get("enabled", defaults["enabled"]) is not False,
        "max_turns": _as_int(raw.get("max_turns"), defaults["max_turns"]),
        "run_budget": _as_int(raw.get("run_budget"), defaults["run_budget"]),
        "timeout": _as_int(raw.get("timeout"), defaults["timeout"]),
        "session_max_messages": _as_int(raw.get("session_max_messages"),
                                        defaults["session_max_messages"]),
        "session_max_tokens": _as_int(raw.get("session_max_tokens"),
                                      defaults["session_max_tokens"]),
        "tokens_per_char": _as_float(raw.get("tokens_per_char"), defaults["tokens_per_char"]),
        "toolsets": str(raw.get("toolsets") or defaults["toolsets"]),
    }


def _dialog_path(cfg: config_mod.Config) -> Path:
    return Path(cfg.state_dir) / DIALOG_STATE_NAME


def _read_dialogs(cfg: config_mod.Config) -> dict:
    try:
        data = json.loads(_dialog_path(cfg).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_dialogs(cfg: config_mod.Config, data: dict) -> None:
    try:
        state_io.save_json(_dialog_path(cfg), data)
    except OSError as exc:
        print(f"[commands] состояние диалога не сохранено: {exc}", file=sys.stderr)


def _estimate_tokens(text: str, per_char: float) -> int:
    """Оценка токенов по символам (D6): ceil(len * tokens_per_char)."""
    return int(math.ceil(max(0, len(text or "")) * max(0.0, per_char)))


def _dialog_item(dialogs: dict, key: str) -> dict:
    """Запись диалога как словарь: испорченное значение не ломает разбор."""
    item = dialogs.get(key) if isinstance(dialogs, dict) else None
    return dict(item) if isinstance(item, dict) else {}


def dialog_session(cfg: config_mod.Config, chat: str, text: str = "") -> dict:
    """Сессия диалога чата; при исчерпании даты/сообщений/токенов — следующая (D6).

    Имя сессии `tg-<chat>-<дата>-<N>`: `--continue` получает именно его, поэтому после
    ротации агент начинает чистый диалог. Токены проецируются вместе с текущим вопросом —
    иначе вопрос-простыня уходил бы в сессию, которая по счёту уже переполнена.
    """
    settings = dialog_settings(cfg)
    today = time.strftime("%Y%m%d")
    dialogs = _read_dialogs(cfg)
    key = str(chat)
    item = _dialog_item(dialogs, key)
    serial = _as_int(item.get("serial"), 0)
    messages = _as_int(item.get("messages"), 0)
    tokens = _as_int(item.get("tokens"), 0)
    date = str(item.get("date") or "")
    fresh = serial < 1
    rotated = False
    if fresh:
        serial, messages, tokens, date = 1, 0, 0, today
    else:
        projected = tokens + _estimate_tokens(text, settings["tokens_per_char"])
        rotated = (date != today
                   or messages + 2 > settings["session_max_messages"]
                   or projected >= settings["session_max_tokens"])
        if rotated:
            serial, messages, tokens, date = serial + 1, 0, 0, today
    session = {"chat": key, "date": date, "serial": serial, "messages": messages,
               "tokens": tokens, "name": f"tg-{key}-{date}-{serial}",
               "started": str(item.get("started") or "")}
    if fresh or rotated or session["started"] != item.get("started"):
        session["started"] = time.strftime("%d.%m.%Y %H:%M:%S")
        dialogs[key] = dict(session)
        _save_dialogs(cfg, dialogs)
    return session


def save_dialog_session(cfg: config_mod.Config, session: dict) -> None:
    """Сохранить счётчики сессии (сообщения и токены) после ответа агента."""
    dialogs = _read_dialogs(cfg)
    dialogs[str(session.get("chat") or "")] = dict(session)
    _save_dialogs(cfg, dialogs)


def reset_dialog(cfg: config_mod.Config, chat: str) -> str:
    """`/reset`: следующая реплика начнёт новую сессию (serial + 1)."""
    today = time.strftime("%Y%m%d")
    dialogs = _read_dialogs(cfg)
    key = str(chat)
    serial = _as_int(_dialog_item(dialogs, key).get("serial"), 0) + 1
    dialogs[key] = {"chat": key, "date": today, "serial": serial, "messages": 0,
                    "tokens": 0, "name": f"tg-{key}-{today}-{serial}",
                    "started": time.strftime("%d.%m.%Y %H:%M:%S")}
    _save_dialogs(cfg, dialogs)
    return "диалог сброшен: следующий вопрос начнёт новую сессию"


def _only_dialog_chat(cfg: config_mod.Config) -> str:
    """Единственный чат в состоянии диалога — для вызова `/reset` без chat."""
    dialogs = _read_dialogs(cfg)
    return str(next(iter(dialogs))) if len(dialogs) == 1 else ""


# ─────────────── запрет приёма команд после 409 (вебхук/второй поллер) ───────────────

def _commands_off_path(cfg: config_mod.Config) -> Path:
    return Path(cfg.state_dir) / COMMANDS_OFF_NAME


def commands_off(cfg: config_mod.Config) -> dict:
    """Причина, по которой приём команд выключен (пусто — приём включён)."""
    try:
        data = json.loads(_commands_off_path(cfg).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def clear_commands_off(cfg: config_mod.Config) -> bool:
    """Снять маркер «приём команд выключен» (`--commands-reset`). True — снят сейчас."""
    path = _commands_off_path(cfg)
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False
    except OSError as exc:
        print(f"[commands] маркер не снят: {exc}", file=sys.stderr)
        return False


def _log_chat(cfg: config_mod.Config, action: str, detail: str,
              started: float | None = None) -> None:
    """Действие из чата — строкой в историю тиков (S3). Сбой записи ответ не ломает."""
    try:
        plan_id = _plan_paths(cfg).plan_id
    except Exception:  # noqa: BLE001 - истории нужен план, но ответ владельцу важнее
        plan_id = ""
    duration = 0.0 if started is None else max(0.0, time.monotonic() - started)
    try:
        append_history(cfg, {"ts": time.time(), "at": time.strftime("%d.%m.%Y %H:%M:%S"),
                             "plan": plan_id, "step": "", "action": action,
                             "duration_s": round(duration, 3),
                             "detail": str(detail or "")[:200], "commit": ""})
    except OSError as exc:
        print(f"[commands] история не записана: {exc}", file=sys.stderr)


def plans_of(cfg: config_mod.Config) -> list:
    """Все ведомые планы; ошибки разбора не мешают ответить на команду."""
    try:
        return list(registry.driven_specs(cfg))
    except Exception as exc:  # noqa: BLE001 - статус обязан отвечать
        print(f"[commands] планы не прочитаны: {exc}", file=sys.stderr)
        return []


def tg_status(cfg: config_mod.Config) -> str:
    """Короткая сводка для чата: стоп-флаг и таблица планов."""
    lines = [f"Конвейер: {cfg.project} (борда {cfg.board})"]
    stops = []
    for spec in plans_of(cfg):
        try:
            p = resolve_paths(cfg, spec.id)
        except Exception:  # noqa: BLE001
            continue
        stop = load_stop(p)
        if stop:
            kind = "владелец" if stop_kind(stop) == "owner" else "инцидент"
            stops.append(f"план {spec.id} — стоп ({kind}, {stop.get('at', '')}): "
                         f"{str(stop.get('reason', ''))[:200]}")
    lines.append("\n".join(stops) if stops else "стоп-флагов нет")
    lines.append("")
    try:
        lines.append(plans_table(cfg))
    except Exception as exc:  # noqa: BLE001
        lines.append(f"(таблица планов недоступна: {exc})")
    # Решение стража — то, что конвейер сделает дальше; `execute=False` на диск не пишет.
    try:
        p = _plan_paths(cfg)
        dec = (run_gate(p, execute=False) or {}).get("decision") or {}
        lines.append("")
        lines.append(f"Решение стража (план {p.plan_id}): {dec.get('action') or '—'} — "
                     f"{str(dec.get('reason') or '')[:200]}")
    except (Exception, SystemExit) as exc:  # noqa: BLE001 - статус обязан отвечать
        lines.append(f"решение стража недоступно: {exc}")
    return "\n".join(lines)


def _plan_paths(cfg: config_mod.Config, plan: str | None = None) -> Paths:
    """Пути плана; без --plan берётся план по умолчанию, иначе первый ведомый."""
    target = plan or cfg.default_plan
    if target is None:
        specs = plans_of(cfg)
        target = specs[0].id if specs else None
    if target is None:
        raise registry.PlanNotFound("нет ни одного плана с журналом")
    return resolve_paths(cfg, target)


def _hold_change(cfg: config_mod.Config, step: str, add: bool) -> str:
    p = _plan_paths(cfg)
    state = load_state(p)
    hold = {str(item).upper() for item in (state.get("hold_steps") or [])}
    released = {str(item).upper() for item in (state.get("hold_released") or [])}
    key = step.strip().upper()
    if not key:
        return "укажи шаг: /hold S3"
    if add:
        hold.add(key)
    else:
        hold.discard(key)
        # Снятие допуска из чата — то же действие владельца, что `--unhold`: без отметки
        # риск-hold вернулся бы на следующем тике (D4).
        released.add(key)
    state["hold_steps"] = sorted(hold)
    state["hold_released"] = sorted(released)
    save_state(p, state)
    return "шаги с ручным допуском: " + (", ".join(sorted(hold)) or "—")


def handle_command(cfg: config_mod.Config, text: str, chat: str | None = None) -> str:
    """Разобрать одну команду владельца и вернуть текст ответа.

    `chat` нужен только `/reset` (диалог живёт на чат); вызов без него работает на
    единственном чате из состояния, а если его нет — новая сессия начнётся и так.
    """
    head, _, rest = (text or "").strip().partition(" ")
    cmd = head.split("@", 1)[0].lower()
    arg = rest.strip()
    if cmd in ("/start", "/help"):
        return TG_HELP
    if cmd == "/status":
        return tg_status(cfg)
    if cmd == "/plans":
        return plans_table(cfg)
    if cmd == "/stop":
        p = _plan_paths(cfg)
        payload = write_stop(p, arg or "остановка владельцем из чата", "owner")
        return f"⏸ стоп поставлен: {payload['reason']}"
    if cmd == "/resume":
        p = _plan_paths(cfg)
        return "▶️ стоп снят, конвейер продолжит" if clear_stop(p) else "стоп-флага не было"
    if cmd in ("/hold", "/unhold"):
        return _hold_change(cfg, arg, add=cmd == "/hold")
    if cmd == "/reset":
        target = str(chat or _only_dialog_chat(cfg))
        if not target:
            return "диалог сброшен: следующий вопрос начнёт новую сессию"
        return reset_dialog(cfg, target)
    return "не знаю такую команду. /help — список."


def owner_ids(cfg: config_mod.Config) -> set[str]:
    """Кто имеет право управлять ботом: только владелец (или несколько, через запятую)."""
    env_name = str(cfg.notify.get("owner_env") or "STEP_PIPELINE_TELEGRAM_OWNER_ID")
    raw = (os.environ.get(env_name) or "").replace(";", ",")
    ids = {part.strip() for part in raw.split(",") if part.strip()}
    if not ids:
        chat = os.environ.get(str(cfg.notify.get("chat_env") or "PIPELINE_TG_CHAT"))
        if chat:
            ids = {str(chat)}
    return ids


ANSI_RE = re.compile(chr(27) + r"\[[0-9;?]*[A-Za-z]")

# Служебные строки одноразового ответа (`--cli --oneshot`): шапка профиля, строки сессии,
# отчёты о вызовах инструментов. D4: такой строки в чате быть не должно.
SERVICE_PREFIXES = (
    "session_id:", "Session", "Hermes Agent", "Provider:", "Model:", "Mode:", "Toolsets:",
    "Workspace:", "Resumed session", "↻", "⟳", "⚙", "🔧", "🛠", "📄", "🧠", "⎿", "↳",
    "┌", "┐", "└", "┘", "│", "├", "┤", "─",
)


def clean_agent_output(text: str) -> str:
    """Ответ агента без служебных строк, CR и ANSI-цветов — чтобы чат читался."""
    raw = (text or "").replace(chr(13) + chr(10), chr(10)).replace(chr(13), chr(10))
    clean = ANSI_RE.sub("", raw)
    lines = [line for line in clean.splitlines()
             if not line.strip().startswith(SERVICE_PREFIXES)]
    return chr(10).join(lines).strip()




def split_message(text: str, limit: int = 3500) -> list[str]:
    """Режет длинный ответ по строкам под лимит Telegram (без разрыва слов, если можно)."""
    chunks: list[str] = []
    current = ""
    for line in (text or "").splitlines():
        while len(line) > limit:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:limit])
            line = line[limit:]
        candidate = (current + chr(10) + line) if current else line
        if len(candidate) > limit:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks or [""]

def _timeout_answer(exc: subprocess.TimeoutExpired) -> str:
    """Человеческий ответ на срез по времени (D5): без имени команды и цифр таймаута.

    Частичный вывод из исключения сохраняется: если агент успел что-то сказать, владелец
    это увидит, а не пустоту.
    """
    parts = []
    for chunk in (getattr(exc, "output", None), getattr(exc, "stderr", None)):
        if not chunk:
            continue
        if isinstance(chunk, bytes):
            chunk = chunk.decode("utf-8", "replace")
        parts.append(str(chunk))
    partial = clean_agent_output(chr(10).join(parts))[:500]
    head = ("не успел ответить за отведённое время (notify.dialog.timeout): диалог разросся."
            " Начать заново — /reset")
    return f"{head} что успел сказать: {partial}" if partial else head


def ask_agent(cfg: config_mod.Config, text: str, chat: str,
              timeout: int | None = None) -> str:
    """Свободный текст владельца — в диалог с агентом профиля, сессия на чат.

    Потолки D3 держат ответ в тех же секундах, что и срез процесса: `--max-turns` и
    `--run-budget` не дают агенту уйти в проект, `-t` снимает terminal/execute_code/
    pipeline_run. `-Q` не ставится: частичный вывод нужен при срезе (D5).
    """
    settings = dialog_settings(cfg)
    if not settings["enabled"]:
        return "диалог выключен (notify.dialog.enabled)"
    limit = _as_int(timeout, settings["timeout"])
    session = dialog_session(cfg, str(chat), text)
    cmd = [_hermes(), "-p", str(cfg.driver), "chat", "-q", text, "--oneshot", "--cli",
           "--continue", session["name"], "--create-if-missing",
           "--max-turns", str(settings["max_turns"]),
           "--run-budget", str(settings["run_budget"]),
           "-t", settings["toolsets"]]
    env = dict(os.environ, NO_COLOR="1", TERM="dumb", CLICOLOR="0")
    try:
        res = _run(cmd, None, timeout=limit, env=env)
    except subprocess.TimeoutExpired as exc:
        return _timeout_answer(exc)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        return (f"агент не ответил: сбой запуска ({type(exc).__name__}). "
                "/reset — начать диалог заново.")
    out = clean_agent_output((res.stdout or "") + (res.stderr or ""))
    if not out:
        return "агент не ответил (пустой ответ). /reset — начать диалог заново."
    session["messages"] = _as_int(session.get("messages"), 0) + 2
    session["tokens"] = _as_int(session.get("tokens"), 0) + _estimate_tokens(
        text + out, settings["tokens_per_char"])
    save_dialog_session(cfg, session)
    return out[: TG_TEXT_LIMIT - 100]


def poll_commands(cfg: config_mod.Config, wait: int = 0, max_updates: int = 10) -> dict:
    """Один опрос getUpdates: выполнить команды владельца и ответить в тот же чат.

    Offset хранится в state_dir/tg_offset.json, поэтому повторный опрос не выполняет
    команду дважды. Отвечаем только в настроенный чат; чужие сообщения игнорируются.
    Ответ 409 (вебхук или второй поллер) выключает приём команд одной записью маркера:
    предупреждение уходит один раз, дальше функция выходит до запроса в сеть.
    """
    result = {"ok": False, "handled": 0, "ignored": 0, "reason": "", "disabled": False}
    if not commands_enabled(cfg):
        result["reason"] = "приём команд выключен (notify.commands.enabled)"
        return result
    off = commands_off(cfg)
    if off:
        result["disabled"] = True
        result["reason"] = (f"приём команд выключен до устранения: {off.get('reason', '')} "
                            "(снять: --commands-reset)")
        return result
    target = tg_target(cfg)
    if target is None:
        result["reason"] = "нет токена/чата в окружении"
        return result
    token, chat = target
    path = _offset_path(cfg)
    offset = _read_offset(path)
    data = _tg_call(token, "getUpdates", http_timeout=int(wait) + 20,
                    offset=offset or None, limit=max_updates, timeout=int(wait))
    if not data.get("ok"):
        code = data.get("error_code")
        if code == 409:
            reason = ("на боте стоит вебхук или второй поллер: getUpdates запрещён (409); "
                      "приём команд выключен до перезапуска")
            result.update(disabled=True, reason=reason)
            try:
                state_io.save_json(_commands_off_path(cfg), {
                    "at": time.strftime("%d.%m.%Y %H:%M:%S"), "reason": reason})
            except OSError as exc:
                print(f"[commands] маркер не записан: {exc}", file=sys.stderr)
            notify(cfg, "⛔ " + reason
                   + ". Снять вебхук и выполнить: python .pipeline/gate.py --commands-reset")
            _log_chat(cfg, "tg_conflict", reason)
            return result
        result["reason"] = str(data.get("description") or "Telegram не ответил")
        print(f"[commands] опрос не удался: {result['reason']}", file=sys.stderr)
        return result
    newest = offset
    for update in data.get("result") or []:
        newest = max(newest, int(update.get("update_id") or 0) + 1)
        message = update.get("message") or {}
        text = str(message.get("text") or "")
        sender_chat = str((message.get("chat") or {}).get("id") or "")
        sender_id = str((message.get("from") or {}).get("id") or "")
        allowed = owner_ids(cfg)
        if sender_chat != str(chat) or (allowed and sender_id and sender_id not in allowed):
            result["ignored"] += 1
            print(f"[commands] чужой отправитель {sender_id or '?'} — игнор", file=sys.stderr)
            continue
        started = time.monotonic()
        if not text.startswith("/"):
            answer = ask_agent(cfg, text, chat)
            for chunk in split_message(answer):
                sent = _tg_call(token, "sendMessage", chat_id=chat, text=chunk,
                                disable_web_page_preview="true")
                if not sent.get("ok"):
                    print(f"[commands] ответ не доставлен: {sent.get('description')}", file=sys.stderr)
                    break
            _log_chat(cfg, "tg_dialog", text, started)
            result["handled"] += 1
            continue
        try:
            answer = handle_command(cfg, text, chat)
        except Exception as exc:  # noqa: BLE001 - команда не должна ронять тик
            answer = f"ошибка выполнения {text.split()[0]}: {exc}"
        sent = _tg_call(token, "sendMessage", chat_id=chat, text=answer[:TG_TEXT_LIMIT],
                        disable_web_page_preview="true")
        if not sent.get("ok"):
            print(f"[commands] ответ не доставлен: {sent.get('description')}", file=sys.stderr)
        _log_chat(cfg, "tg_command", text, started)
        result["handled"] += 1
    if newest > offset:
        _write_offset(path, newest)
    result["ok"] = True
    return result


def _evidence(p: Paths, verdict: dict) -> str:
    name = Path(verdict["path"]).name
    commit = f"коммит `{verdict['commit']}`" if verdict.get("commit") else "коммит не указан"
    report = p.cfg.rel(Path(verdict["path"]))
    return (f"ревью `{p.cfg.reviewer}` — APPROVE (`{report}`, {commit}); "
            f"гейты шага прогнаны ревьюером самостоятельно")


def run_gate(p: Paths, execute: bool = True, create_card=_create_card,
             notify_fn=None, commit_fn=commit_and_push, ctx: dict | None = None) -> dict:
    """Один тик конвейера. Возвращает {decision, applied, detail}."""
    ctx = ctx if ctx is not None else build_ctx(p)
    say_raw = notify_fn or (lambda text: notify(p.cfg, text))
    plan_id = p.plan_id
    # Наследование ДО решения: иначе идущий план навсегда получил бы «не утверждён».
    # Только исполняющий проход — `--status` на диск не пишет.
    if execute:
        ensure_approval(p, ctx)
    decision = decide(ctx)
    cleared = ""
    if execute and decision.get("clear_incident"):
        ctx["stop"] = None
        cleared = decision.get("reason", "")
        clear_stop(p)
        decision = decide(ctx)
    result = {"plan_id": plan_id, "decision": decision, "applied": bool(cleared),
              "detail": cleared, "incident_cleared": bool(cleared),
              "status": status_text(ctx, decision)}
    if not execute:
        result["metrics"] = history_metrics(p.cfg)
        return result

    def say(text: str) -> None:
        say_raw(text)

    if cleared:
        say(f"✅ инцидент снят ({cleared}), конвейер продолжает")
    state = ctx["state"]

    def touch(action: str) -> None:
        state["last_action"] = action
        state["last_tick"] = time.strftime("%d.%m.%Y %H:%M:%S")
        # Первое же действие снимает ожидание: сторож начинает отсчёт заново.
        for key in ("waiting_since", "waiting_reason",
                    "watchdog_notice_at", "watchdog_notice_reason"):
            state.pop(key, None)
        save_state(p, state)

    action = decision["action"]
    if action == "idle":
        # Простой виден только здесь (и в tick), поэтому и записывается здесь.
        if decision.get("approval_blocked"):
            # Сторож простоя для неутверждённого плана не запускается: иначе владелец
            # получит второе уведомление о том же плане.
            approval_notice(p, ctx, decision, say)
            result["watch"] = {"notice": False, "waiting_since": None}
            return result
        result["watch"] = watch_idle(p.cfg, p, ctx, decision, say)
        return result
    # ANCHOR:hold_risk_action — стабильная ссылка для спеки и ревью
    if action == "hold_risk":
        step = decision["data"]
        hold = {str(item).upper() for item in (state.get("hold_steps") or [])}
        hold.add(str(step["id"]).upper())
        state["hold_steps"] = sorted(hold)
        # Это ДЕЙСТВИЕ, а не простой: при нескольких действиях за тик (max_actions > 1)
        # hold ставится первым и не теряется. `touch` сохраняет ТОТ ЖЕ словарь состояния,
        # поэтому запись hold_steps уходит на диск вместе с отметкой о тике.
        touch(f"hold_risk:{step['id']}")
        reason = decision.get("reason", "")
        say("⏸ " + reason)
        result.update(applied=True, detail=reason)
        return result
    if action == "create_step":
        step = decision["data"]
        prompt = prompt_rel(p, step["id"])
        if prompt is None and p.cfg.require_prompt:
            # Шаг без постановки не начинается: заказ промптеру + инцидентный стоп,
            # который снимется сам, как только файл постановки появится.
            order_key = f"planloop:{plan_id}:{step['id']}:prompt"
            order_id = create_card(p, f"[постановка {plan_id} {step['id']}] {step['title']}",
                                   prompt_order_body(p, step), p.cfg.prompter, order_key,
                                   p.cfg.prompt_skills, max_runtime=p.cfg.prompt_runtime)
            where = f"карточка {order_id}" if order_id else "карточку заказа создать не удалось"
            reason = (f"нет постановки шага {step['id']}: заказана роли {p.cfg.prompter} ({where}); "
                      f"ожидается {p.cfg.rel(Path(p.cfg.prompts_dir))}/<дата>_{plan_id}-{step['id']}"
                      f"_prompt.md — шаг не начинаю")
            current = load_stop(p) or {}
            if current.get("reason") == reason:
                result.update(applied=False, detail=reason)
                return result
            write_stop(p, reason, "gate", kind="incident")
            result.update(applied=True, detail=reason)
            touch("stop:no_prompt")
            say("⛔ " + reason)
            return result
        key = f"planloop:{plan_id}:{step['id']}:step"
        cid = create_card(p, f"[план {plan_id} {step['id']}] {step['title']}",
                          step_card_body(p, step, prompt), _step_assignee(p, step), key,
                          p.cfg.step_skills, max_runtime=p.cfg.step_runtime)
        result.update(applied=bool(cid), detail=f"карточка шага: {cid}")
        touch(f"create_step:{cid}")
        if cid:
            summary = _step_summary(step)
            lines = [f"план {plan_id}, шаг {step['id']} взят в работу — {step['title']}",
                     f"  что делаем: {summary}"]
            dod = " ".join(str(step.get("dod") or "").split())
            if dod and dod[:60] != summary[:60]:
                lines.append(f"  готово, когда: {dod[:240]}")
            lines.append(f"  карточка {cid}")
            say(chr(10).join(lines))
        else:
            say(f"не смог создать карточку шага {step['id']} — проверь `hermes kanban ls`")
        return result
    if action == "create_review":
        step, attempt = decision["data"], decision["attempt"]
        key = f"planloop:{plan_id}:{step['id']}:review:{attempt}"
        report_rel = _report_rel(p, step["id"])
        cid = create_card(p, f"[ревью {plan_id} {step['id']} #{attempt}] {step['title']}",
                          review_card_body(p, step, attempt, report_rel,
                                           prompt_rel(p, step["id"])),
                          p.cfg.reviewer, key, p.cfg.review_skills,
                          parent=decision.get("parent"),
                          max_runtime=p.cfg.review_runtime)
        result.update(applied=bool(cid), detail=f"карточка ревью: {cid}")
        touch(f"create_review:{cid}")
        if cid:
            say(f"план {plan_id}, шаг {step['id']} исполнен, отправлен на ревью (карточка {cid})")
        else:
            say(f"не смог поставить ревью шага {step['id']} плана {plan_id}")
        return result
    if action == "create_rereview":
        prev_id = decision["step"]
        try:
            step_data = collector(p, prev_id).get("next") or {}
        except SystemExit as exc:
            reason = f"повторное ревью {prev_id} не поставить: {exc}"
            write_stop(p, reason, "gate", kind="incident")
            result.update(applied=True, detail=reason)
            touch("stop:no_contract")
            say("⛔ " + reason)
            return result
        key = f"planloop:{plan_id}:{prev_id}:rereview:1"
        report_rel = _report_rel(p, prev_id)
        cid = create_card(p, f"[ревью {plan_id} {prev_id} #2] {step_data.get('title', '')}",
                          review_card_body(p, step_data, 2, report_rel,
                                           prompt_rel(p, prev_id)),
                          p.cfg.reviewer, key, p.cfg.review_skills,
                          parent=decision.get("parent"), max_runtime=p.cfg.review_runtime)
        result.update(applied=bool(cid), detail=f"повторное ревью {prev_id}: {cid}")
        touch(f"create_rereview:{cid}")
        if cid:
            say(f"план {plan_id}, шаг {prev_id} закрыт в журнале, но ревью было REQUEST_CHANGES — "
                f"ставлю повторное ревью (карточка {cid})")
        else:
            say(f"не смог поставить повторное ревью шага {prev_id}")
        return result
    if action == "create_fix":
        step, round_no = decision["data"], decision["round"]
        key = f"planloop:{plan_id}:{step['id']}:fix:{round_no}"
        report_rel = _report_rel(p, step["id"])
        cid = create_card(p, f"[правки {plan_id} {step['id']} #{round_no}] {step['title']}",
                          fix_card_body(p, step, round_no, report_rel),
                          _step_assignee(p, step), key, p.cfg.step_skills,
                          parent=decision.get("parent"), max_runtime=p.cfg.step_runtime)
        result.update(applied=bool(cid), detail=f"карточка правок: {cid}")
        touch(f"create_fix:{cid}")
        if cid:
            findings = _review_findings(Path(p.repo) / report_rel, 3)
            msg = [f"план {plan_id}, ревью {step['id']}: нужны правки, это единственный "
                   f"автоматический круг"]
            if findings:
                msg.append("  что нашёл ревьюер:")
                msg += [f"  - {f}" for f in findings]
            else:
                msg.append("  замечания в отчёте — разбор ниже")
            msg.append(f"  подробно: {report_rel}")
            msg.append(f"  карточка правок {cid}")
            say(chr(10).join(msg))
            # Находки — в саму карточку правок: отказ комментария не меняет result и не
            # рвёт тик (в `tick` ложный `applied` обрывает цепочку действий).
            _comment(p, cid, fix_comment_body(p, step, round_no, report_rel,
                                              decision.get("parent")))
        else:
            say(f"ревью {step['id']}: REQUEST_CHANGES, но карточку правок создать не удалось")
        return result
    if action == "close_step":
        step, verdict = decision["data"], decision["verdict"]
        commit = verdict.get("commit") or ""
        if commit:
            exists = _run(["git", "cat-file", "-e", f"{commit}^{{commit}}"], Path(p.repo))
            if exists.returncode != 0:
                reason = (f"ревью {step['id']} APPROVE ссылается на несуществующий коммит "
                          f"{commit} — конвейер остановлен")
                write_stop(p, reason, "gate")
                result.update(applied=True, detail=reason)
                touch("stop:bad_commit")
                say("⛔ " + reason)
                return result
        added = append_journal_row(p, step["id"], step["title"], commit, _evidence(p, verdict))
        files = [p.cfg.rel(Path(p.plan_file)), _report_rel(p, step["id"])]
        ok, detail = commit_fn(
            p, files, f"docs(plan): закрыт шаг {step['id']} плана {plan_id} (ревью APPROVE)")
        result.update(applied=ok, detail=f"журнал: {'добавлена строка' if added else 'уже был'}; "
                                         f"коммит/пуш: {'ok ' + detail if ok else detail}")
        # sha коммита для истории: свой коммит стража из detail, иначе — коммит из ревью (D5).
        match = re.match(r"^([0-9a-f]{7,40})(?:\s|$)", detail.strip())
        result["commit"] = match.group(1) if match else commit
        touch(f"close_step:{step['id']}")
        if ok:
            say(f"✅ план {plan_id}, шаг {step['id']} закрыт (ревью APPROVE, коммит {detail}). "
                f"Следующий шаг стартует автоматически")
        else:
            state["pending_commit"] = {"files": files, "step": step["id"], "reason": detail}
            save_state(p, state)
            say(f"шаг {step['id']} принят ревью, но коммит/пуш не выполнен: {detail}. "
                f"Повторю сам, как только дерево освободится")
        return result
    if action == "stop":
        reason = decision.get("reason", "")
        previous = (ctx.get("stop") or {}).get("reason")
        write_stop(p, reason, "gate", kind="incident")
        result.update(applied=True, detail=reason)
        touch("stop")
        if previous != reason:
            extra = decision.get("details") or ""
            say("⛔ конвейер остановлен: " + reason
                + (chr(10) + chr(10) + extra if extra else "")
                + chr(10) + "После устранения причины продолжится сам; снять принудительно:"
                + " `python .pipeline/gate.py --resume`")
        return result
    return result


def retry_pending(p: Paths, notify_fn=None) -> dict | None:
    """Повторить отложенный коммит/пуш (дерево было занято чужой сессией)."""
    state = load_state(p)
    pending = state.get("pending_commit")
    if not pending:
        return None
    ok, detail = commit_and_push(
        p, pending["files"],
        f"docs(plan): закрыт шаг {pending['step']} плана {p.plan_id} (ревью APPROVE)")
    if ok:
        state.pop("pending_commit", None)
        save_state(p, state)
        say = notify_fn or (lambda text: notify(p.cfg, text))
        say(f"✅ отложенный коммит шага {pending['step']} выполнен: {detail}")
        return {"applied": True, "detail": f"pending commit ok {detail}"}
    return {"applied": False, "detail": f"pending commit всё ещё ждёт: {detail}"}


# ───────────────────────────── CLI ─────────────────────────────

def plans_rows(cfg: config_mod.Config) -> list[dict]:
    """Машиночитаемая таблица планов (для UI и внешних вызовов)."""
    driven = set(registry.driven_ids(cfg))
    out: list[dict] = []
    for spec in registry.specs(cfg):
        p = resolve_paths(cfg, spec.id)
        step_id = "—"
        risk = ""
        try:
            nxt = collector(p).get("next") or {}
            step_id = nxt.get("id") or "—"
            # Шага нет — риска нет: пустая строка, а не «normal», иначе данные врут.
            risk = str(nxt.get("risk") or "normal") if nxt else ""
        except SystemExit as exc:
            print(f"[plans] план {spec.id}: сборщик шагов не работает: {exc}", file=sys.stderr)
        stop = load_stop(p)
        hold = sorted({str(h).upper() for h in (load_state(p).get("hold_steps") or [])})
        out.append({
            "id": spec.id,
            "file": cfg.rel(Path(spec.file)),
            "driven": spec.id in driven,
            "next": step_id,
            "risk": risk,
            "hold": hold,
            "closed": len(registry.closed_steps(spec, cfg)),
            "stop": ({**stop, "kind": stop_kind(stop)} if stop else None),
        })
    return out


def plans_table(cfg: config_mod.Config) -> str:
    """Таблица планов: ведётся ли, следующий шаг, сколько закрыто, стоп-флаг."""
    rows: list[tuple[str, str, str, str, str]] = []
    for row in plans_rows(cfg):
        stop = row["stop"]
        if stop:
            flag = f"{stop['kind']}: {stop.get('reason', '')}"
        elif row["hold"]:
            flag = "hold: " + ", ".join(row["hold"])
        else:
            flag = "—"
        rows.append((row["id"], "да" if row["driven"] else "нет", str(row["next"]),
                     str(row["closed"]), flag))
    head = ("ПЛАН", "ВЕДЁТСЯ", "СЛЕДУЮЩИЙ ШАГ", "ЗАКРЫТО", "СТОП-ФЛАГ")
    widths = [max(len(head[i]), *(len(r[i]) for r in rows)) if rows else len(head[i])
              for i in range(5)]
    lines = ["  ".join(head[i].ljust(widths[i]) for i in range(4)) + "  " + head[4]]
    for row in rows:
        lines.append("  ".join(row[i].ljust(widths[i]) for i in range(4)) + "  " + row[4])
    if not rows:
        lines.append("планов с распознанным журналом не найдено")
    return "\n".join(lines)


def _step_of(decision: dict) -> str:
    """Идентификатор шага, которого касается решение (пустая строка — шага нет)."""
    step = decision.get("step")
    if not step:
        data = decision.get("data")
        step = data.get("id") if isinstance(data, dict) else None
    return str(step).upper() if step else ""


def step_key(plan_id: str, decision: dict) -> str | None:
    """Ключ шага, который трогает решение, вида `1:S1`; None — решение шага не называет.

    Чистая функция: не зависит от путей и состояния, поэтому проверяется юнит-тестом.
    Нужна инварианту «одно действие на шаг за тик» (D5): по ключу видно, что следующее
    решение адресует шаг, который в этом тике уже получал действие.
    """
    step = _step_of(decision)
    return f"{plan_id}:{step}" if step else None


# ──────────────────────── история тиков и метрики ────────────────────────

def history_lines(cfg: config_mod.Config) -> tuple[list[dict], int, int]:
    """История тиков: (валидные записи, всего непустых строк, битых строк).

    Валидная запись — объект JSON с числовым `ts` и непустым `action`. Всё остальное
    (не JSON, JSON не объект, нет `ts`/`action`) — битая строка: в подсчётах метрик она
    не участвует, но считается в `bad_lines` (D7). Файла нет или он не читается — пустая
    история, а не падение: метрики обязаны собираться на любом состоянии проекта.
    """
    path = history_path(cfg)
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return [], 0, 0
    records: list[dict] = []
    total = 0
    bad = 0
    for line in text.splitlines():
        if not line.strip():
            continue
        total += 1
        try:
            row = json.loads(line)
        except (ValueError, TypeError):
            bad += 1
            continue
        if not isinstance(row, dict):
            bad += 1
            continue
        ts = row.get("ts")
        if (not isinstance(ts, (int, float)) or isinstance(ts, bool)
                or not str(row.get("action") or "")):
            bad += 1
            continue
        records.append(row)
    return records, total, bad


def history_size(cfg: config_mod.Config) -> int:
    """Размер файла истории в байтах; файла нет — ноль."""
    try:
        return int(history_path(cfg).stat().st_size)
    except OSError:
        return 0


def append_history(cfg: config_mod.Config, record: dict) -> None:
    """Дописать строку истории и уложить файл в пределы ротации (D6).

    Обрезка идёт по строкам, а не по байтам: разрезанная строка JSONL — это битая строка.
    Последняя записанная строка не выбрасывается никогда, даже если одна превышает предел
    размера. Запись атомарная, через `state_io.atomic_write_text`.
    """
    path = history_path(cfg)
    try:
        old = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        old = []
    lines = [line for line in old if line.strip()]
    lines.append(json.dumps(record, ensure_ascii=False))
    if len(lines) > HISTORY_MAX_LINES:
        lines = lines[-HISTORY_MAX_LINES:]
    # Размер считаем по байтам ровно так, как их запишет `atomic_write_text`
    # (он переводит `\n` в os.linesep), и вычитаем отброшенные строки без пересчёта.
    sep = os.linesep
    size = len(sep.join(lines).encode("utf-8")) + len(sep)
    drop = 0
    while drop < len(lines) - 1 and size > HISTORY_MAX_BYTES:
        size -= len(lines[drop].encode("utf-8")) + len(sep)
        drop += 1
    lines = lines[drop:]
    state_io.atomic_write_text(path, "\n".join(lines) + "\n")


def _median(values: list[float]) -> int:
    """Медиана, округлённая до целых; чётное число — среднее двух центральных."""
    ordered = sorted(values)
    mid = len(ordered) // 2
    value = ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2
    return int(round(value))


def _plan_metrics(p: Paths, records: list[dict]) -> dict:
    """Метрики одного плана: закрытые по журналу шаги, время шага, правки, инциденты."""
    steps_closed = 0
    try:
        lines = Path(p.plan_file).read_text(encoding="utf-8", errors="replace").splitlines()
        steps_closed = sum(1 for i in _journal_rows(p)
                           if registry.status_of_row(lines[i]).startswith("ЗАКРЫТ"))
    except (OSError, SystemExit):
        steps_closed = 0
    first_seen: dict[str, float] = {}
    closed_at: dict[str, float] = {}
    fix_rounds = 0
    incidents = 0
    for row in records:
        if str(row.get("plan") or "") != p.plan_id:
            continue
        action = str(row.get("action") or "")
        step = str(row.get("step") or "").upper()
        ts = float(row["ts"])
        if step:
            if step not in first_seen or ts < first_seen[step]:
                first_seen[step] = ts
            if action == "close_step" and step not in closed_at:
                closed_at[step] = ts
        if action == "create_fix":
            fix_rounds += 1
        elif action == "stop":
            incidents += 1
    seconds = [closed_at[step] - first_seen[step] for step in closed_at
               if step in first_seen and closed_at[step] >= first_seen[step]]
    step_seconds = {"avg": int(round(sum(seconds) / len(seconds))) if seconds else 0,
                    "median": _median(seconds) if seconds else 0,
                    "measured": len(seconds)}
    return {"steps_closed": steps_closed, "step_seconds": step_seconds,
            "fix_rounds": fix_rounds, "incidents": incidents}


def history_metrics(cfg: config_mod.Config, now: float | None = None) -> dict:
    """Посчитать метрики по истории (D8). Ничего не пишет и не бросает наружу.

    `actions_7d` — строки не старше окна по видам действия; `steps_closed` — по журналу
    плана (поэтому совпадает с ним); `step_seconds` — по шагам, у которых в истории есть
    и первое действие, и `close_step`; `fix_rounds` и `incidents` — по всей истории.
    """
    stamp = time.time() if now is None else float(now)
    records, total, bad = history_lines(cfg)
    cutoff = stamp - METRICS_DAYS * 86400
    actions: dict[str, int] = {}
    for row in records:
        if float(row["ts"]) >= cutoff:
            action = str(row.get("action") or "")
            actions[action] = actions.get(action, 0) + 1
    plans: dict[str, dict] = {}
    for spec in registry.driven_specs(cfg):
        try:
            plans[spec.id] = _plan_metrics(resolve_paths(cfg, spec.id), records)
        except Exception:  # noqa: BLE001 - сбой по одному плану не ломает остальные
            plans[spec.id] = {"steps_closed": 0,
                              "step_seconds": {"avg": 0, "median": 0, "measured": 0},
                              "fix_rounds": 0, "incidents": 0}
    return {
        "generated_at": time.strftime("%d.%m.%Y %H:%M:%S"),
        "window_days": METRICS_DAYS,
        "actions_7d": actions,
        "actions_7d_total": sum(actions.values()),
        "plans": plans,
        "history": {"lines": total, "bad_lines": bad, "size_bytes": history_size(cfg)},
    }


def remember_action(p: Paths, plan_id: str, decision: dict, detail: str,
                    duration: float = 0.0, commit: str = "") -> None:
    """Записать применённое действие в состояние плана и в историю тиков.

    В состояние (`last_actions`, последние `LAST_ACTIONS_KEPT`) — как раньше: атомарно,
    через `load_state`/`save_state`, свежим чтением (`run_gate` уже мог сохранить
    `pending_commit`, и запись из устаревшего словаря стёрла бы его). `last_action`/
    `last_tick` из `touch()` не трогаем: на `last_action` опирается сторож простоя.

    В историю (`history.jsonl`) — строка на действие: `ts` (epoch, для окна и разностей),
    `at` тем же форматом, что в состоянии, план, шаг, действие, длительность в секундах,
    `detail` (те же 200 символов) и sha коммита, если он был (D4).
    """
    at = time.strftime("%d.%m.%Y %H:%M:%S")
    action = str(decision.get("action") or "")
    step = _step_of(decision)
    text = str(detail or "")[:200]
    state = load_state(p)
    history = state.get("last_actions")
    history = list(history) if isinstance(history, list) else []
    history.append({"at": at, "plan": plan_id, "action": action, "step": step,
                    "detail": text})
    state["last_actions"] = history[-LAST_ACTIONS_KEPT:]
    save_state(p, state)
    append_history(p.cfg, {"ts": time.time(), "at": at, "plan": plan_id, "step": step,
                           "action": action, "duration_s": round(float(duration), 3),
                           "detail": text, "commit": str(commit or "")})



def tick(cfg: config_mod.Config, execute: bool = True,
         max_actions: int | None = None) -> dict:
    """Один тик по ВСЕМ ведомым планам: до `max_actions` действий, по одному на шаг.

    Предел берётся из аргумента (флаг `--max-actions`), иначе из `max_actions` в
    `pipeline.json` проекта, иначе 1 — прежнее поведение «ровно одно действие».

    Порядок: суточная сводка (сбой не валит тик), затем обход планов. Перед каждым
    следующим действием состояние перечитывается (`build_ctx`), поэтому цепочка
    «журнал → следующий шаг → заказ постановки» проезжается одним тиком. Простаивающий
    план записывает ожидание — действием это не считается, как и `retry_pending` (D5).

    Тик заканчивается, когда: работы нет ни в одном плане; предел исчерпан; действие не
    применилось; либо следующее действие адресует шаг, который в этом тике уже получал
    действие (D5). Каждое применённое действие пишется в состояние плана.
    """
    limit = max_actions if isinstance(max_actions, int) and max_actions >= 1 \
        else max_actions_of(cfg)
    status_by_plan: dict[str, str] = {}
    if execute:
        try:
            send_digest(cfg)
        except Exception as exc:  # noqa: BLE001 - сводка не повод останавливать конвейер
            print(f"[digest] сводка не отправлена: {exc}", file=sys.stderr)
    actions: list[dict] = []
    touched: set[str] = set()
    retried_any: dict | None = None
    cleared_any = False
    no_work = "во всех планах нет работы"
    last_decision: dict = {"action": "idle", "reason": no_work}
    reason = no_work
    pass_no = 0
    while len(actions) < limit:
        pass_no += 1
        acted = False
        finish = False
        for spec in registry.driven_specs(cfg):
            p = resolve_paths(cfg, spec.id)
            if execute and pass_no == 1:
                # Отложенный коммит повторяем один раз за тик: один план — одна попытка.
                retried = retry_pending(p)
                if retried is not None:
                    retried_any = retried
                if retried and retried.get("applied"):
                    return {"plan_id": spec.id, "decision": {"action": "retry_pending"},
                            "applied": True, "detail": retried.get("detail", ""),
                            "incident_cleared": False, "retried": retried,
                            "status": retried.get("detail", ""), "actions": [],
                            "actions_count": 0, "reason": "выполнен отложенный коммит"}
            ctx = build_ctx(p)
            decision = decide(ctx)
            status_by_plan[spec.id] = status_text(ctx, decision)
            if decision["action"] == "idle":
                if execute:
                    if decision.get("approval_blocked"):
                        approval_notice(p, ctx, decision, lambda text: notify(cfg, text))
                    else:
                        watch_idle(cfg, p, ctx, decision, lambda text: notify(cfg, text))
                continue
            if not execute:
                last_decision = decision
                reason = "показ без исполнения: действие не применялось"
                finish = True
                break
            key = step_key(spec.id, decision)
            if key and key in touched:
                # Инвариант D5: шаг за тик получает не больше одного действия.
                last_decision = decision
                reason = (f"шаг {key} уже получал действие в этом тике — "
                          f"второй раз не трогаю")
                finish = True
                break
            started = time.monotonic()
            result = run_gate(p, execute=execute, ctx=ctx)
            duration = round(time.monotonic() - started, 3)
            last_decision = result.get("decision") or decision
            entry = {"plan": spec.id, "action": str(decision.get("action") or ""),
                     "step": _step_of(decision), "applied": bool(result.get("applied")),
                     "detail": str(result.get("detail") or "")}
            actions.append(entry)
            if key:
                touched.add(key)
            cleared_any = cleared_any or bool(result.get("incident_cleared"))
            if entry["applied"]:
                remember_action(p, spec.id, decision, entry["detail"], duration=duration,
                                commit=str(result.get("commit") or ""))
            if not entry["applied"]:
                reason = (f"действие {entry['action']} не применилось: "
                          f"{entry['detail'] or 'подробностей нет'} — второй попытки в этом тике нет")
                finish = True
                break
            acted = True
            if key is None:
                reason = f"действие {entry['action']} шага не адресует — тик завершён"
                finish = True
            break                       # состояние перечитываем с начала обхода
        if finish:
            break
        if not acted:
            reason = no_work
            break
    else:
        reason = f"предел действий за тик исчерпан (max_actions={limit})"

    detail = "; ".join(f"{a['action']}: {a['detail']}" for a in actions if a["applied"])
    return {
        "plan_id": actions[-1]["plan"] if actions else None,
        "decision": last_decision,
        "applied": any(a["applied"] for a in actions),
        "detail": detail,
        "incident_cleared": cleared_any,
        "retried": retried_any,
        "status": ("\n\n".join(status_by_plan.values())
                   or "планов с распознанным журналом не найдено"),
        "actions": actions,
        "actions_count": sum(1 for a in actions if a["applied"]),
        "reason": reason,
    }


def incident(cfg: config_mod.Config, args: argparse.Namespace, exc: BaseException) -> int:
    """Сбой движка вместо трейсбека: инцидентный стоп, уведомление, `--json` и код 1."""
    if isinstance(exc, GateError):
        reason = str(exc)
    else:
        reason = f"сбой движка: {type(exc).__name__}: {exc}"
    # Без времени и pid: по причине сравниваются повторы (тот же стоп — молчим).
    reason = reason[:300]
    plan_id = getattr(exc, "plan_id", None) or (args.plan or None)
    p: Paths | None = None
    try:
        plan_id = plan_id or registry.default_plan_id(cfg)
        p = resolve_paths(cfg, plan_id)
        plan_id = p.plan_id
    except Exception:  # noqa: BLE001 - путей нет: записывать некуда, причина остаётся в stderr
        p = None
    if args.run:
        # Стоп и уведомление — только для тика: чтение статуса состояние не меняет.
        current: dict = {}
        if p is not None:
            try:
                current = load_stop(p) or {}
            except Exception as stop_exc:  # noqa: BLE001 - сбой чтения не отменяет уведомление
                print(f"ошибка: {stop_exc}", file=sys.stderr)
        owner_stop = stop_kind(current) == "owner"
        # Стоп владельца инцидент не перезаписывает (его снимает только владелец),
        # поэтому о сбое сообщаем каждый тик: записать причину некуда.
        fresh = owner_stop or current.get("reason") != reason
        if fresh and p is not None and not owner_stop:
            try:
                write_stop(p, reason, "gate", kind="incident")
            except Exception as stop_exc:  # noqa: BLE001 - уведомление от записи не зависит
                print(f"ошибка: {stop_exc}", file=sys.stderr)
        if fresh:
            try:
                notify(cfg, "⛔ сбой тика: " + reason)
            except Exception as notify_exc:  # noqa: BLE001 - обработчик не даёт второй трейсбек
                print(f"ошибка: уведомление не отправлено: {notify_exc}", file=sys.stderr)
    print(f"ошибка: {reason}", file=sys.stderr)
    if args.json:
        print(json.dumps({"ok": False, "error": reason, "applied": False, "incident": True,
                          "plan_id": plan_id,
                          "decision": {"action": "incident", "reason": reason}},
                         ensure_ascii=False, indent=2))
    return 1


def main(argv: list[str] | None = None) -> int:
    config_mod.setup_stdio()
    ap = argparse.ArgumentParser(description="Страж шага: шаг → ревью → следующий шаг")
    ap.add_argument("--plan", help="номер плана (по умолчанию из pipeline.json)")
    ap.add_argument("--plans", action="store_true",
                    help="таблица планов: следующий шаг, закрыто, стоп-флаг")
    ap.add_argument("--run", action="store_true",
                    help="выполнить тик: до max_actions действий (режим cron)")
    ap.add_argument("--max-actions", type=int, default=None,
                    help="сколько действий за тик (по умолчанию — из pipeline.json)")
    ap.add_argument("--status", action="store_true", help="только показать состояние и решение")
    ap.add_argument("--json", action="store_true", help="машинный вывод")
    ap.add_argument("--stop", metavar="ПРИЧИНА", help="остановиться после текущего шага")
    ap.add_argument("--resume", action="store_true", help="снять стоп-флаг")
    ap.add_argument("--hold", metavar="ШАГ", help="шаг требует ручного допуска владельца")
    ap.add_argument("--unhold", metavar="ШАГ", help="снять ручной допуск")
    ap.add_argument("--commands", action="store_true",
                    help="принять команды владельца из Telegram (getUpdates) и ответить в чат")
    ap.add_argument("--commands-wait", type=int, default=0,
                    help="секунды длинного опроса для --commands (0 — без ожидания)")
    ap.add_argument("--commands-reset", action="store_true",
                    help="снять маркер «приём команд выключен» (после 409 от вебхука)")
    args = ap.parse_args(argv)

    try:
        cfg = config_mod.load(repo_root())
    except config_mod.ConfigError as exc:
        print(f"ошибка: {exc}", file=sys.stderr)
        return 2

    # D2: флаг важнее файла; флаг задаёт предел на один запуск и в конфиг не пишется.
    limit = args.max_actions if args.max_actions is not None else max_actions_of(cfg)

    if args.run:
        # Тик меняет план и коммитит: на разъехавшейся копии движка он не запускается.
        ok, why = verify_engine_integrity(Path(__file__).resolve().parent)
        if not ok:
            print(f"тик остановлен: {why}", file=sys.stderr)
            print("обновите движок проекта: hermes pipeline setup --upgrade --project <путь>",
                  file=sys.stderr)
            return 3

    try:
        if args.plans:
            if args.json:
                print(json.dumps(plans_rows(cfg), ensure_ascii=False, indent=2))
            else:
                print(plans_table(cfg))
            return 0

        if args.commands_reset:
            cleared = clear_commands_off(cfg)
            print("приём команд снова включён" if cleared
                  else "приём команд не был выключен")
            return 0

        if args.commands:
            result = poll_commands(cfg, wait=args.commands_wait)
            if args.json:
                print(json.dumps(result, ensure_ascii=False, indent=2))
            else:
                tail = f" ({result['reason']})" if result.get("reason") else ""
                extra = f", чужих проигнорировано: {result['ignored']}" if result.get("ignored") else ""
                print(f"команд обработано: {result['handled']}{extra}{tail}")
            # Выключенный приём команд — не сбой джобы: иначе cron будет слать ошибки,
            # пока владелец не снимет вебхук или не включит команды обратно.
            return 0 if (result.get("ok") or result.get("disabled")
                         or not commands_enabled(cfg)) else 1

        try:
            p = resolve_paths(cfg, args.plan)
        except registry.PlanNotFound as exc:
            print(f"ошибка: {exc}", file=sys.stderr)
            return 2

        owner_cmd = ("stop" if args.stop is not None
                     else "resume" if args.resume
                     else "hold" if (args.hold or args.unhold) else "")
        if owner_cmd:
            # Команды владельца меняют состояние и стоп-флаг (read-modify-write), поэтому
            # берут ту же блокировку, что и тик: поверх живого тика команда не применяется.
            lock = state_io.acquire(Path(cfg.state_dir), what=owner_cmd)
            if lock is None:
                who = state_io.holder(Path(cfg.state_dir)) or {}
                print(f"тик уже идёт (pid {who.get('pid', '?')}, с {who.get('at', '?')}, "
                      f"{who.get('what', '?')}) — команда {owner_cmd} не применена")
                return 0
            try:
                if args.stop is not None:
                    payload = write_stop(p, args.stop or "остановка владельца", "owner")
                    notify(cfg, f"⏸ остановка после текущего шага: {payload['reason']}")
                    print(f"стоп-флаг поставлен: {p.stop_file} ({payload['reason']})")
                    return 0
                if args.resume:
                    existed = clear_stop(p)
                    notify(cfg, "▶️ стоп снят, конвейер продолжит со следующего шага")
                    print("стоп-флаг снят" if existed else "стоп-флага не было")
                    return 0
                state = load_state(p)
                hold = {str(item).upper() for item in (state.get("hold_steps") or [])}
                released = {str(item).upper() for item in (state.get("hold_released") or [])}
                if args.hold:
                    hold.add(args.hold.upper())
                else:
                    hold.discard(args.unhold.upper())
                    # Отметка владельца: автоматический риск-hold по этому шагу больше
                    # не ставится, иначе снять допуск было бы невозможно (D4).
                    released.add(args.unhold.upper())
                state["hold_steps"] = sorted(hold)
                state["hold_released"] = sorted(released)
                save_state(p, state)
                print("шаги с ручным допуском: " + (", ".join(sorted(hold)) or "—"))
                return 0
            finally:
                state_io.release(lock)

        if args.run:
            # Один лок на проект, не на план: tick() и retry_pending обслуживают все планы.
            lock = state_io.acquire(Path(cfg.state_dir), what="run")
            if lock is None:
                who = state_io.holder(Path(cfg.state_dir)) or {}
                detail = "тик уже идёт"
                if args.json:
                    print(json.dumps({"locked": True, "applied": False, "detail": detail,
                                      "decision": {"action": "locked", "reason": detail}},
                                     ensure_ascii=False, indent=2))
                else:
                    print(f"{detail} (pid {who.get('pid', '?')}, с {who.get('at', '?')}) — "
                          f"второй тик ничего не делает")
                return 0
            try:
                if args.plan is None:
                    result = tick(cfg, max_actions=limit)
                    if args.json:
                        print(json.dumps(result, ensure_ascii=False, indent=2))
                        return 0
                    acted = bool(result.get("applied") or result.get("retried"))
                    if not acted and not args.status:
                        return 0
                    print(result["status"])
                    if result.get("applied"):
                        print(f"Выполнено [{result.get('plan_id')}]: {result['detail']}")
                    return 0
                retried = retry_pending(p)
                started = time.monotonic()
                result = run_gate(p, execute=True)
                duration = round(time.monotonic() - started, 3)
                if result.get("applied") and (result.get("decision") or {}).get("action") != "idle":
                    # Ветка одного плана — тоже тик: применённое действие попадает в состояние.
                    remember_action(p, p.plan_id, result.get("decision") or {},
                                    result.get("detail", ""), duration=duration,
                                    commit=str(result.get("commit") or ""))
                result["pending_commit"] = retried
            finally:
                state_io.release(lock)
        else:
            retried = None
            result = run_gate(p, execute=False)
            result["pending_commit"] = retried
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        acted = args.run and (result["applied"] or retried)
        if not acted and not args.status:
            return 0
        print(result["status"])
        if retried:
            print(f"Отложенный коммит: {retried['detail']}")
        if args.run:
            print(f"Выполнено: {result['applied']} — {result['detail']}")
        return 0
    except GateError as exc:
        return incident(cfg, args, exc)
    except Exception as exc:  # noqa: BLE001 - любой сбой тика = инцидент, а не трейсбек
        return incident(cfg, args, exc)


def repo_root() -> str:
    """Корень проекта: env PIPELINE_REPO (предпросмотр), иначе каталог над `.pipeline/`."""
    env = os.environ.get("PIPELINE_REPO")
    if env:
        return str(Path(env).resolve())
    return str(Path(__file__).resolve().parents[1])


if __name__ == "__main__":
    raise SystemExit(main())

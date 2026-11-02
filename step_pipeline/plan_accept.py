# -*- coding: utf-8 -*-
"""Черновики планов и утверждение владельцем: `hermes pipeline plan ...`.

Зачем отдельный каталог: агент-планировщик пишет план в `<plans_dir>/drafts/`, а движок
читает только `<plans_dir>/*.md` без рекурсии (`engine/registry.py:165-183`). Поэтому
неутверждённый план стражу не виден, и «план написан» ещё не значит «конвейер пошёл».

Утверждение — решение человека, а не исполнителя, поэтому перенос файла и запись
`plan_<N>_approval.json` живут в одной команде: запись с sha256 читает страж (шаг S7), и
появляться она обязана ровно тогда, когда файл уже лежит в каталоге планов. Разделять
перенос и запись нельзя: «файл на месте, записи нет» — это молча сломанный план.

Проверка делегируется линтеру `plan_lint.py`, когда он есть (шаг 1-S8). Запасной путь
(линтера ещё нет) разбирает черновик движком через временный `pipeline.json`: второй
парсер шагов в плагине не заводится (решение D1), а свои проверки — минимальные и только
про журнал и контракт шага.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import tempfile
from datetime import datetime
from pathlib import Path

try:
    from . import project as proj
except ImportError:  # pragma: no cover - отладка файлом
    import project as proj  # type: ignore

try:
    from . import plan_lint
except ImportError:  # шаг 1-S8 ещё не сделан — работают запасные проверки
    plan_lint = None  # type: ignore

_PLANS_DEFAULT = "docs/plans"
_APPROVAL_NAME = "plan_{}_approval.json"
# Номер плана в имени файла — как engine/registry.py:30.
_PLAN_NUM_RE = re.compile(r"^(\d{1,2})-")
_HEADING_RE = re.compile(r"^(?P<hashes>#{1,6})\s+(?P<title>\S.*?)\s*$")
_STEP_ID_REGEX = r"(?P<id>[A-Za-z]{1,4}\d+[A-Za-z]?(?:-\d+)?)"
JOURNAL_COLUMNS = 5
# Пункт «Тесты/гейты: —» считается пустым (plan_lint.py:48).
_EMPTY_GATES = ("", "—", "-", "−")


def plans_dir(project) -> Path:
    """Каталог планов из `paths.plans` (умолчание — `docs/plans`).

    Своя копия `plan_lint._plans_dir`, а не импорт: запасной путь обязан работать и
    тогда, когда линтера ещё нет.
    """
    cfg = proj.read_config(Path(project))
    rel = (cfg.get("paths") or {}).get("plans") or _PLANS_DEFAULT
    return Path(project) / str(rel)


def drafts_dir(project) -> Path:
    """Подкаталог черновиков: движок читает только верхний уровень каталога планов."""
    return plans_dir(project) / "drafts"


def approval_file(project, plan_id) -> Path:
    """Запись утверждения в каталоге состояния проекта (её читает страж, шаг S7)."""
    return proj.state_dir(Path(project)) / _APPROVAL_NAME.format(_normalize(plan_id))


def plan_number(name: str) -> str | None:
    """Номер плана из имени файла (`01-foo.md` → `1`); `None`, если префикса нет."""
    match = _PLAN_NUM_RE.match(str(name))
    return str(int(match.group(1))) if match else None


def now_iso() -> str:
    """Дата утверждения — отдельной функцией, чтобы тест мог её подменить.

    Иначе проверка «повторный accept не перезаписал дату первого утверждения» зависела бы
    от попадания двух вызовов в одну секунду.
    """
    return datetime.now().isoformat(timespec="seconds")


def find_draft(project, value) -> Path:
    """Черновик по номеру или по пути.

    Номер ищется только среди файлов каталога черновиков; путь принимается, если файл
    существует и оканчивается `.md`. Ошибка — `ValueError` с текстом для человека.
    """
    project = Path(project)
    text = str(value).strip()
    if text.isdigit():
        want = _normalize(text)
        folder = drafts_dir(project)
        found = sorted(p for p in folder.glob("*.md") if plan_number(p.name) == want)
        if not found:
            raise ValueError(f"черновика с номером {want} нет в каталоге черновиков")
        if len(found) > 1:
            names = ", ".join(p.name for p in found)
            raise ValueError(f"черновиков с номером {want} несколько: {names}")
        return found[0]
    path = Path(text).expanduser()
    if not path.is_absolute():
        path = project / path
    if path.is_file() and path.suffix.lower() == ".md":
        return path
    raise ValueError(f"черновик не найден: {text}")


def find_plan(project, plan_id) -> Path | None:
    """Утверждённый файл с этим номером в каталоге планов, или `None`."""
    want = _normalize(plan_id)
    for path in sorted(plans_dir(project).glob("*.md")):
        if plan_number(path.name) == want:
            return path
    return None


def check_draft(project, draft) -> list[str]:
    """Ошибки черновика; пустой список — чисто.

    Линтер, если он есть, — единственный источник проверок: свои мини-проверки нужны
    только до шага 1-S8, иначе в проекте завелись бы два расходящихся линтера.
    """
    project = Path(project)
    draft = Path(draft)
    if plan_lint is not None:
        return [str(problem) for problem in plan_lint.lint_project(project, str(draft)).problems]
    return _fallback_errors(project, draft)


def accept(project, value) -> int:
    """Утвердить план по номеру: проверки → перенос → запись с sha256.

    Печатает результат сам и возвращает код 0/1 (2 — аргумент не номер). Порядок важен:
    сначала проверки, потом перенос, потом запись; при любой ошибке файл и запись не
    трогаются, чтобы не оставить полуутверждённый план.
    """
    project = Path(project)
    text = str(value).strip()
    if not text.isdigit():
        print(f"--accept принимает номер плана, получено: {value}", file=sys.stderr)
        return 2
    number = _normalize(text)
    final = find_plan(project, number)
    missing = ""
    try:
        draft: Path | None = find_draft(project, number)
    except ValueError as exc:
        draft = None
        missing = str(exc)
    if draft is None and final is None:
        print(f"ошибка: {missing}", file=sys.stderr)
        return 1

    errors = check_draft(project, draft or final)
    if errors:
        for problem in errors:
            print(f"  ✗ {problem}")
        print(f"ИТОГ: ошибок {len(errors)} — файл не перенесён, черновик остался на месте")
        return 1

    if draft is not None and final is not None:
        return _accept_existing(project, number, draft, final)
    if draft is not None:
        return _move_draft(project, number, draft)
    if final is None:  # недостижимо: выше отсечены «нет ни черновика, ни плана»
        print(f"ошибка: черновика с номером {number} нет в каталоге черновиков", file=sys.stderr)
        return 1
    return _accept_final(project, number, final)


# ───────────────────────────── ветки утверждения ─────────────────────────────

def _accept_existing(project: Path, number: str, draft: Path, final: Path) -> int:
    """Черновик и утверждённый файл уже есть: сравниваем имена и содержимое."""
    if final.name != draft.name:
        print(f"ошибка: с номером {number} уже есть другой файл: {final.name}", file=sys.stderr)
        return 1
    if _sha256(final) != _sha256(draft):
        print("ошибка: файл уже есть и отличается содержимым", file=sys.stderr)
        return 1
    record = _read_approval(project, number)
    if record is None:
        # Запись потеряли — восстанавливаем её, иначе страж (S7) навсегда заблокирует план.
        _write_approval(project, number, final)
        print(f"План {number} утверждён: {_shown(final, project)}")
        print(f"Запись утверждения: {approval_file(project, number)}")
        return 0
    print(f"План {number}: уже утверждён ({record.get('approved_at', '')}) — ничего не изменено")
    return 0


def _move_draft(project: Path, number: str, draft: Path) -> int:
    """Перенести черновик в каталог планов и записать утверждение."""
    target = plans_dir(project) / draft.name
    target.parent.mkdir(parents=True, exist_ok=True)
    os.replace(draft, target)
    _write_approval(project, number, target)
    print(f"План {number} утверждён: {_shown(target, project)}")
    print(f"Запись утверждения: {approval_file(project, number)}")
    return 0


def _accept_final(project: Path, number: str, final: Path) -> int:
    """Черновика нет, утверждённый файл есть: сверить хэш с записью."""
    record = _read_approval(project, number)
    if record is None:
        _write_approval(project, number, final)
        print(f"План {number} утверждён: {_shown(final, project)}")
        print(f"Запись утверждения: {approval_file(project, number)}")
        return 0
    if record.get("sha256") == _sha256(final):
        print(f"План {number}: уже утверждён ({record.get('approved_at', '')}) — ничего не изменено")
        return 0
    _write_approval(project, number, final)
    print(f"План {number} переутверждён (содержимое изменилось)")
    return 0


# ───────────────────────────── запись утверждения ─────────────────────────────

def _write_approval(project: Path, number: str, path: Path) -> dict:
    """Записать `plan_<N>_approval.json`: ключи фиксированы контрактом шага S7."""
    payload = {
        "plan": str(number),
        "file": _shown(path, project),
        "sha256": _sha256(path),
        "approved_at": now_iso(),
        "profile": os.environ.get("HERMES_PROFILE") or "default",
    }
    _atomic_write_text(approval_file(project, number),
                       json.dumps(payload, ensure_ascii=False, indent=2))
    return payload


def _read_approval(project: Path, number: str) -> dict | None:
    """Прочитать запись утверждения; нет файла или он битый — `None`."""
    path = approval_file(project, number)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _atomic_write_text(path: Path, text: str) -> None:
    """Атомарная запись: временный файл рядом, fsync, `os.replace`.

    Копия структуры `plan_mode._atomic_write_text` сознательная: плагин не импортирует
    движок как модуль (`project.py:1-6`), а общий помощник плагина ради одной записи
    заводить незачем.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
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


# ───────────────────────────── запасные проверки ─────────────────────────────

def _fallback_errors(project: Path, draft: Path) -> list[str]:
    """Проверки без линтера: журнал, разбор движком, контракт каждого шага."""
    try:
        lines = draft.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        return [f"файл не читается: {exc}"]

    journal_errors, fatal = _journal_errors(project, lines)
    if fatal:
        return journal_errors
    return journal_errors + _engine_errors(project, draft)


def _journal_errors(project: Path, lines: list[str]) -> tuple[list[str], bool]:
    """Мини-разбор журнала: пять колонок, шаг во второй, без повторов.

    Второй аргумент — «дальше не идти»: битая регулярка шага делает бессмысленным и
    журнал, и вызов движка (движок упадёт на том же конфиге).
    """
    cfg = proj.read_config(project)
    marker = str((cfg.get("markers") or {}).get("journal") or "журнал").lower()
    raw = str(cfg.get("step_id_regex") or _STEP_ID_REGEX)
    pattern = raw if raw.startswith("^") else "^" + raw
    try:
        step_re = re.compile(pattern)
    except re.error:
        return [f"step_id_regex не компилируется: {pattern}"], True
    if "id" not in step_re.groupindex:
        return [f"step_id_regex не компилируется: {pattern}"], True

    found = _headings(lines)
    rows = _journal_rows(lines, found, marker)
    if rows is None:
        return [f"нет раздела-журнала со словом «{marker}»"], False

    errors: list[str] = []
    steps: set[str] = set()
    for index in rows:
        cells = [cell.strip() for cell in lines[index].strip().strip("|").split("|")]
        if cells and set("".join(cells)) <= set("-: "):
            continue
        if cells and cells[0].lower() in ("дата", "date"):
            continue
        if len(cells) != JOURNAL_COLUMNS:
            errors.append(f"строка журнала без {JOURNAL_COLUMNS} колонок (нашлось {len(cells)})")
        if len(cells) < 2:
            errors.append("строка журнала без шага во второй колонке")
            continue
        match = step_re.match(cells[1])
        if match is None:
            errors.append("строка журнала без шага во второй колонке")
            continue
        step_id = match.group("id").upper()
        if len(cells) < JOURNAL_COLUMNS:
            continue
        if step_id in steps:
            errors.append(f"шаг {step_id} повторяется в журнале")
            continue
        steps.add(step_id)
    if not steps:
        errors.append("в журнале нет ни одной строки шага")
    return errors, False


def _engine_errors(project: Path, draft: Path) -> list[str]:
    """Разбор черновика движком и контракт каждого шага (решение D1).

    Движок видит только каталог планов, поэтому черновик показывается ему через временный
    `pipeline.json` с абсолютным `paths.plans`: `PIPELINE_CONFIG` поддержан с 0.1.0, и
    вторая копия движка не нужна.
    """
    number = plan_number(draft.name)
    if number is None:
        return [f"черновик без номера в имени: {draft.name}"]
    with tempfile.TemporaryDirectory() as tmp:
        cfg = proj.read_config(project)
        paths = dict(cfg.get("paths") or {})
        paths["plans"] = str(draft.resolve().parent)
        cfg["paths"] = paths
        cfg_path = Path(tmp) / "pipeline.json"
        cfg_path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
        env = {"PIPELINE_CONFIG": str(cfg_path)}
        try:
            package = proj.engine_json(project, "next_step.py", "--plan", number, extra_env=env)
        except RuntimeError as exc:
            return [f"движок не разобрал черновик: {exc}"]
        if not isinstance(package, dict):
            return ["движок не разобрал черновик: пустой ответ"]
        if not package.get("steps_total"):
            return ["раздел шагов не распознан (заголовки «### S1. Название»)"] + _contract_errors(
                project, number, env, [])
        return _contract_errors(project, number, env, package.get("steps") or [])


def _contract_errors(project: Path, number: str, env: dict[str, str],
                     steps: list[dict]) -> list[str]:
    """Контракт каждого шага: файлы, гейты, критерий приёмки."""
    errors: list[str] = []
    seen: set[str] = set()
    for step in steps:
        step_id = str(step.get("id") or "").upper()
        if not step_id:
            continue
        if step_id in seen:
            errors.append(f"шаг {step_id} повторяется в разделе шагов")
            continue
        seen.add(step_id)
        try:
            package = proj.engine_json(project, "next_step.py", "--plan", number,
                                       "--step", step_id, extra_env=env)
        except RuntimeError as exc:
            errors.append(f"движок не разобрал шаг {step_id}: {exc}")
            continue
        contract = (package or {}).get("next") if isinstance(package, dict) else None
        if not isinstance(contract, dict):
            errors.append(f"шаг {step_id}: движок не отдал контракт")
            continue
        if not contract.get("files"):
            errors.append(f"шаг {step_id}: нет пункта «Файл»")
        gates = contract.get("gates") or []
        if not gates or all(str(gate).strip() in _EMPTY_GATES for gate in gates):
            errors.append(f"шаг {step_id}: нет пункта «Тесты/гейты»")
        if not str(contract.get("dod") or "").strip():
            errors.append(f"шаг {step_id}: нет критерия «Готово =»")
    return errors


def _headings(lines: list[str]) -> list[tuple[int, int, str]]:
    """(индекс строки, уровень, заголовок) — как registry.py:82-89."""
    found: list[tuple[int, int, str]] = []
    for index, line in enumerate(lines):
        match = _HEADING_RE.match(line)
        if match:
            found.append((index, len(match.group("hashes")), match.group("title")))
    return found


def _section_end(found: list[tuple[int, int, str]], pos: int, limit: int) -> int:
    """Первый заголовок того же или более высокого уровня — граница раздела."""
    level = found[pos][1]
    for index in range(pos + 1, len(found)):
        if found[index][1] <= level:
            return found[index][0]
    return limit


def _journal_rows(lines: list[str], found: list[tuple[int, int, str]],
                  marker: str) -> list[int] | None:
    """Индексы строк таблицы раздела-журнала; `None` — раздела с маркером нет."""
    chosen: list[int] | None = None
    for pos, (index, level, title) in enumerate(found):
        if level < 2 or marker not in title.lower():
            continue
        end = _section_end(found, pos, len(lines))
        rows = [i for i in range(index + 1, end) if lines[i].strip().startswith("|")]
        if rows:
            return rows
        if chosen is None:
            chosen = rows
    return chosen


# ───────────────────────────── мелочи ─────────────────────────────

def _normalize(plan_id) -> str:
    """Номер в каноническом виде: `01` → `1` (как registry.normalize)."""
    text = str(plan_id).strip()
    if not text.isdigit():
        raise ValueError(f"номер плана должен быть числом, получено: {plan_id!r}")
    return str(int(text))


def _sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _shown(path: Path, project: Path) -> str:
    """Путь для показа: относительно проекта в posix-виде, как plan_lint._shown."""
    try:
        return Path(path).resolve().relative_to(Path(project).resolve()).as_posix()
    except (ValueError, OSError):
        return Path(path).as_posix()

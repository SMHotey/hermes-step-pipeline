# -*- coding: utf-8 -*-
"""Реестр планов: какой план ведёт конвейер, где у него журнал и файлы состояния.

Что считается планом (без списков номеров в коде):
    1. файл `<plans_dir>/<НОМЕР>-<имя>.md`;
    2. есть раздел, в заголовке которого встречается маркер журнала («журнал»);
    3. в таблице раздела хотя бы одна строка со идентификатором шага во ВТОРОЙ колонке.

Журнал проекта устроен так: `| Дата | Шаг | Коммиты | Прогоны и доказательства | Статус |`.
Ограничение обобщения сознательное: колонка шага обязана быть второй.
Идентификатор шага в строке журнала распознаётся выражением `step_id_regex` из
`pipeline.json`, а строка журнала — ровно пять колонок (`JOURNAL_COLUMNS`).

Файлы состояния — по одному комплекту на план: `plan_<НОМЕР>_state.json` и
`plan_<НОМЕР>_stop.json` в каталоге `paths.state`.

Здесь же читается запись утверждения плана (`plan_<НОМЕР>_approval.json` в том же
каталоге состояния): модуль только сверяет её с файлом плана по sha256. Пишет запись
не он — утверждает владелец командой `hermes pipeline plan --accept N`, а страж лишь
читает её и для уже идущих планов сам пишет унаследованную запись (см. `gate.py`).

Модуль только ЧИТАЕТ файлы планов и ничего не меняет.
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

# Плоский импорт: скрипт запускают файлом (`python .pipeline/gate.py`), пакетом — реже.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import config as config_mod  # noqa: E402 - модуль рядом

_PLAN_FILE_RE = re.compile(r"^(?P<id>\d{1,2})-")
_HEADING_RE = re.compile(r"^(?P<hashes>#{1,6})\s+(?P<title>\S.*?)\s*$")
# Строка журнала: `Дата | Шаг | Коммиты | Прогоны и доказательства | Статус`.
JOURNAL_COLUMNS = 5


class PlanNotFound(LookupError):
    """Запрошенный номер плана не найден среди планов с распознанным журналом."""


@dataclass(frozen=True)
class PlanSpec:
    """Один план: чем он является для конвейера и где лежит его состояние."""

    id: str
    file: Path
    journal_heading: str
    state_file: Path
    stop_file: Path
    review_prefix: str

    def review_name(self, step_id: str) -> str:
        return f"{self.review_prefix}-{step_id}.md"


def normalize(plan_id: object) -> str:
    """Номер плана в каноническом виде: `01`→`1`, ` 31 `→`31`."""
    text = str(plan_id).strip()
    if not text.isdigit():
        raise PlanNotFound(f"номер плана должен быть числом, получено: {plan_id!r}")
    return str(int(text))


def default_plan_id(cfg: config_mod.Config) -> str:
    if cfg.default_plan:
        return normalize(cfg.default_plan)
    found = sorted(discover(cfg), key=lambda s: int(s.id))
    if not found:
        raise PlanNotFound(f"в {cfg.plans_dir} нет планов с распознанным журналом")
    return found[0].id


def state_file_for(cfg: config_mod.Config, plan_id: str) -> Path:
    return cfg.state_dir / f"plan_{normalize(plan_id)}_state.json"


def stop_file_for(cfg: config_mod.Config, plan_id: str) -> Path:
    return cfg.state_dir / f"plan_{normalize(plan_id)}_stop.json"


# ───────────────────────────── утверждение плана ─────────────────────────────

APPROVAL_TEMPLATE = "plan_{}_approval.json"
# Отметка стража «владельцу уже сказали, что план не ведётся» — в состоянии плана.
APPROVAL_NOTICE_KEY = "approval_notice"


def approval_file_for(cfg: config_mod.Config, plan_id: str) -> Path:
    """Путь записи утверждения плана в каталоге состояния проекта."""
    return cfg.state_dir / APPROVAL_TEMPLATE.format(normalize(plan_id))


def sha256_file(path) -> str:
    """sha256 сырых байтов файла плана (без перекодирования: CRLF не мешает)."""
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read_approval(cfg: config_mod.Config, plan_id: str) -> dict | None:
    """Прочитать запись утверждения; нет файла, он битый или не словарь — `None`."""
    path = approval_file_for(cfg, plan_id)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def approval_status(cfg: config_mod.Config, spec: PlanSpec) -> dict:
    """Сверить запись утверждения с файлом плана.

    `state`: `approved` | `missing` (записи нет или она не читается) | `changed`
    (запись есть, но sha256 в ней отсутствует или не совпал с фактическим).
    """
    fact = sha256_file(spec.file)
    record = read_approval(cfg, spec.id)
    result = {"state": "approved", "reason": "", "record": record, "sha256": fact}
    if record is None:
        result["state"] = "missing"
        result["reason"] = (
            f"план {spec.id} не утверждён: нет записи "
            f"{APPROVAL_TEMPLATE.format(spec.id)} (утверждает владелец: "
            f"hermes pipeline plan --accept {spec.id})")
        return result
    saved = str(record.get("sha256") or "")
    if saved != fact:
        result["state"] = "changed"
        result["reason"] = (
            f"план {spec.id} изменён после утверждения: sha256 {fact[:12]} не совпал "
            f"с записью {saved[:12] or '—'} (переутвердить: "
            f"hermes pipeline plan --accept {spec.id})")
    return result


def plan_started(cfg: config_mod.Config, spec: PlanSpec) -> bool:
    """План уже идёт: есть файл состояния ИЛИ закрытые шаги в журнале.

    Файл состояния, в котором лежит ТОЛЬКО отметка о пропуске по утверждению
    (`approval_notice`), работой по плану не считается: её пишет сам страж, и без этой
    оговорки неутверждённый план «становился идущим» через тик после первого пропуска.
    """
    state_path = Path(spec.state_file)
    if state_path.is_file():
        try:
            data = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return True          # битое состояние — план уже трогали, разбираться не нам
        if not isinstance(data, dict) or set(data) - {APPROVAL_NOTICE_KEY}:
            return True
    return bool(closed_steps(spec, cfg))


# ───────────────────────────── разбор журнала ─────────────────────────────

def headings(lines: list[str]) -> list[tuple[int, int, str]]:
    """(индекс строки, уровень, заголовок) по всем заголовкам файла."""
    found: list[tuple[int, int, str]] = []
    for i, line in enumerate(lines):
        m = _HEADING_RE.match(line)
        if m:
            found.append((i, len(m.group("hashes")), m.group("title")))
    return found


def section_end(found: list[tuple[int, int, str]], pos: int, limit: int) -> int:
    """Первый заголовок того же или более высокого уровня — граница раздела."""
    level = found[pos][1]
    for idx in range(pos + 1, len(found)):
        if found[idx][1] <= level:
            return found[idx][0]
    return limit


def journal_section(lines: list[str], marker: str,
                    step_re: re.Pattern) -> tuple[str, list[int]] | None:
    """Заголовок первого раздела-журнала со строками шагов и индексы его таблицы.

    Разделом может быть только заголовок уровня не ниже 2 (H1 — заголовок документа),
    а строка шага распознаётся выражением `step_id_regex` из `pipeline.json`.
    """
    found = headings(lines)
    for pos, (_idx, level, title) in enumerate(found):
        if level < 2 or marker not in title.lower():
            continue
        start = found[pos][0]
        end = section_end(found, pos, len(lines))
        rows = [i for i in range(start + 1, end) if lines[i].strip().startswith("|")]
        if any(step_of_row(lines[i], step_re) for i in rows):
            return lines[start].strip(), rows
    return None


def step_of_row(line: str, step_re: re.Pattern) -> str | None:
    """Идентификатор шага из строки журнала (вторая колонка) или None."""
    cells = [c.strip() for c in line.strip().strip("|").split("|")]
    if len(cells) < JOURNAL_COLUMNS:
        return None
    m = step_re.match(cells[1])
    return m.group("id").upper() if m else None


def status_of_row(line: str) -> str:
    """Статус шага — последняя колонка строки журнала (в верхнем регистре)."""
    cells = [c.strip() for c in line.strip().strip("|").split("|")]
    return cells[-1].upper() if len(cells) >= JOURNAL_COLUMNS else ""


def closed_steps(spec: PlanSpec, cfg: config_mod.Config) -> list[str]:
    """Закрытые шаги плана по его журналу (статус начинается с «ЗАКРЫТ»)."""
    path = Path(spec.file)
    if not path.is_file():
        return []
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    found = journal_section(lines, cfg.journal_marker, cfg.step_regex)
    if not found:
        return []
    closed: set[str] = set()
    for i in found[1]:
        step = step_of_row(lines[i], cfg.step_regex)
        if step and status_of_row(lines[i]).startswith("ЗАКРЫТ"):
            closed.add(step)
    return sorted(closed)


# ───────────────────────────── реестр ─────────────────────────────

def _make_spec(cfg: config_mod.Config, plan_id: str, path: Path, heading: str) -> PlanSpec:
    return PlanSpec(
        id=plan_id,
        file=path,
        journal_heading=heading,
        state_file=state_file_for(cfg, plan_id),
        stop_file=stop_file_for(cfg, plan_id),
        review_prefix=plan_id,
    )


def discover(cfg: config_mod.Config) -> list[PlanSpec]:
    """Все планы проекта с распознанным журналом (порядок — по имени файла)."""
    found: list[PlanSpec] = []
    plans_dir = Path(cfg.plans_dir)
    if not plans_dir.is_dir():
        return found
    for path in sorted(plans_dir.glob("*.md")):
        m = _PLAN_FILE_RE.match(path.name)
        if not m:
            continue
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        section = journal_section(lines, cfg.journal_marker, cfg.step_regex)
        if not section:
            continue
        found.append(_make_spec(cfg, normalize(m.group("id")), path, section[0]))
    return found


def specs(cfg: config_mod.Config) -> list[PlanSpec]:
    """Планы по возрастанию номера; план по умолчанию — первым."""
    found = discover(cfg)
    try:
        want = default_plan_id(cfg)
    except PlanNotFound:
        return found
    found.sort(key=lambda s: s.id)
    return sorted(found, key=lambda s: (s.id != want, int(s.id)))


def driven_ids(cfg: config_mod.Config) -> tuple[str, ...]:
    """Номера планов, которые `--run` без `--plan` обслуживает сам.

    Появление нового файла плана НЕ включает его автоматически: иначе любой
    черновик начинал бы исполняться на следующем тике cron. Список берётся из
    `driven_plans` (`["*"]` — все распознанные) или переменной `PIPELINE_PLANS`.
    """
    if "*" in cfg.driven:
        return tuple(spec.id for spec in discover(cfg))
    return tuple(normalize(pid) for pid in cfg.driven)


def driven_specs(cfg: config_mod.Config) -> list[PlanSpec]:
    allowed = set(driven_ids(cfg))
    return [spec for spec in specs(cfg) if spec.id in allowed]


def get(cfg: config_mod.Config, plan_id: object) -> PlanSpec:
    """Спек одного плана; неизвестный номер — ошибка со списком доступных."""
    available = specs(cfg)
    want = normalize(plan_id)
    for spec in available:
        if spec.id == want:
            return spec
    names = ", ".join(s.id for s in available) or "нет ни одного"
    raise PlanNotFound(f"план {want} не найден в {cfg.plans_dir} "
                       f"(планы с журналом: {names})")

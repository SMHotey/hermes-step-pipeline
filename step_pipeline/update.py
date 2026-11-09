# -*- coding: utf-8 -*-
"""Обновление плагина из GitHub: проверка релиза и установка новой версии.

Плагин живёт копией в `<hermes_home>/plugins/step_pipeline`, поэтому обновление — это
не `git pull`, а «скачать архив тега, распаковать пакет, переписать файлы». Прежняя
копия уезжает в `<hermes_home>/plugin-backups/` — откат остаётся возможным руками.

Сеть: только `api.github.com` (номер последнего релиза) и `github.com` (архив тега).
Внешних зависимостей нет — stdlib. Тесты в сеть не ходят: `fetch` подменяется.
"""
from __future__ import annotations

import functools
import io
import json
import os
import re
import shutil
import ssl
import tarfile
import tempfile
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

#: Репозиторий плагина. Сюда же смотрит `hermes pipeline update` и кнопка в панели.
REPO = "SMHotey/hermes-step-pipeline"
RELEASES_API = f"https://api.github.com/repos/{REPO}/releases/latest"
TAGS_API = f"https://api.github.com/repos/{REPO}/tags?per_page=100"
ARCHIVE = "https://github.com/{repo}/archive/refs/tags/{tag}.tar.gz"
PACKAGE = "step_pipeline"
DEFAULT_TIMEOUT = 8.0
ARCHIVE_TIMEOUT = 90.0
USER_AGENT = "hermes-step-pipeline-updater"
VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")


class UpdateError(RuntimeError):
    """Проверка или установка обновления не удалась (сеть, архив, формат)."""


def package_dir(explicit: Path | str | None = None) -> Path:
    """Каталог установленного пакета: по умолчанию — рядом с этим файлом."""
    return Path(explicit).resolve() if explicit else Path(__file__).resolve().parent


def parse_version(text: object) -> tuple[int, ...]:
    """`v0.17.0` → `(0, 17, 0)`; пустое/мусор — пустой кортеж (сравнивать нельзя)."""
    match = VERSION_RE.search(str(text or ""))
    return tuple(int(part) for part in match.groups()) if match else ()


def local_version(explicit: Path | str | None = None) -> str:
    """Версия установленного пакета из его `plugin.yaml` (`0`, если прочитать нельзя)."""
    try:
        text = (package_dir(explicit) / "plugin.yaml").read_text(encoding="utf-8")
    except OSError:
        return "0"
    match = re.search(r"(?m)^version:\s*['\"]?([^'\"\s]+)", text)
    return match.group(1) if match else "0"


def github_token() -> str:
    """Токен только для private-репозиториев: `GITHUB_TOKEN`/`GH_TOKEN` из окружения.

    Публичному репозиторию токен не нужен вовсе; без него приватный отвечает 404 на
    `releases/latest` и `tags`, и обновление молчит, как будто выпусков нет. Токен
    берётся из `.env` профиля (или переменной окружения) и никуда не пишется в лог.
    """
    for name in ("GITHUB_TOKEN", "GH_TOKEN"):
        value = (os.environ.get(name) or "").strip()
        if value:
            return value
    return ""


# Python без явного набора корней доверяет хранилищу ОС, а в нём бывают просроченные
# сертификаты (в Windows — DST Root CA X3 и другие старые корни): OpenSSL строит через
# них просроченный путь для Let's Encrypt, и проверка обновления падает с
# `CERTIFICATE_VERIFY_FAILED: certificate has expired`. Корни берём из явного набора.
CA_ENV_VARS = ("STEP_PIPELINE_CA_BUNDLE", "HERMES_CA_BUNDLE", "SSL_CERT_FILE",
               "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE")


def ca_bundle() -> str:
    """Путь к набору корневых сертификатов; пусто — доверять хранилищу ОС."""
    for name in CA_ENV_VARS:
        path = (os.environ.get(name) or "").strip()
        if path and os.path.isfile(path):
            return path
    try:
        import certifi
    except ImportError:
        return ""
    path = certifi.where()
    return path if os.path.isfile(path) else ""


@functools.lru_cache(maxsize=1)
def ssl_context() -> ssl.SSLContext:
    """Контекст TLS с найденным набором корней (строится один раз на процесс)."""
    bundle = ca_bundle()
    return ssl.create_default_context(cafile=bundle) if bundle else ssl.create_default_context()


def _http_get(url: str, timeout: float) -> bytes:
    """GET с User-Agent: GitHub без него отвечает 403, а Cloudflare режет запросы."""
    headers = {"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"}
    token = github_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=timeout, context=ssl_context()) as response:
        return response.read()


def _json(fetch, url: str, timeout: float):
    return json.loads(fetch(url, timeout).decode("utf-8", "replace"))


def _missing(exc: Exception) -> bool:
    """404 — «такого адреса нет», а не сбой сети: у репозитория ещё нет выпусков/тегов."""
    return getattr(exc, "code", None) == 404 or "404" in str(exc)


def latest(fetch=None, timeout: float = DEFAULT_TIMEOUT) -> tuple[str, str, str]:
    """Последняя версия: `(версия, тег, источник)`.

    Сначала `/releases/latest` — так владелец объявляет выпуск (в тексте релиза бывают
    заметки). Если релизов нет, берём максимальный тег `vX.Y.Z`. Сбой обоих путей —
    `UpdateError`, а не «обновлений нет»: молчание про недоступную сеть выглядело бы
    как «всё актуально» и обновление не приезжало бы никогда. Исключение — 404 на обоих
    адресах: репозиторий без единого выпуска, это не сбой, а «пока нечего ставить».
    """
    fetch = fetch or _http_get
    problem = ""
    not_found = False
    try:
        data = _json(fetch, RELEASES_API, timeout)
        tag = str((data or {}).get("tag_name") or "").strip() if isinstance(data, dict) else ""
        if parse_version(tag):
            return _clean(tag), tag, "release"
        problem = "в последнем релизе нет тега вида vX.Y.Z"
    except Exception as exc:                                   # noqa: BLE001
        not_found = _missing(exc)
        problem = f"релизы: {type(exc).__name__}: {exc}"
    try:
        data = _json(fetch, TAGS_API, timeout)
    except Exception as exc:                                   # noqa: BLE001
        if not_found and _missing(exc):
            return "", "", ""                                  # выпусков ещё нет
        raise UpdateError(f"{problem}; теги: {type(exc).__name__}: {exc}") from exc
    tags = [str(item.get("name") or "") for item in data if isinstance(item, dict)] \
        if isinstance(data, list) else []
    versions = sorted(((parse_version(tag), tag) for tag in tags if parse_version(tag)),
                      reverse=True)
    if not versions:
        raise UpdateError(f"{problem}; на GitHub нет тегов вида vX.Y.Z")
    key, tag = versions[0]
    return ".".join(str(part) for part in key), tag, "tags"


def _clean(tag: str) -> str:
    """`v0.17.0` → `0.17.0`; версия без префикса остаётся как есть."""
    match = VERSION_RE.search(tag)
    return ".".join(match.groups()) if match else tag


def check(explicit: Path | str | None = None, fetch=None,
          timeout: float = DEFAULT_TIMEOUT) -> dict:
    """Сравнить установленную версию с последним выпуском. Сеть не обязательна."""
    current = local_version(explicit)
    info = {"current": current, "latest": "", "tag": "", "source": "",
            "update_available": False, "error": "",
            "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    try:
        version, tag, source = latest(fetch, timeout)
    except Exception as exc:                                   # noqa: BLE001
        info["error"] = str(exc) or type(exc).__name__
        return info
    if not version:
        return info                       # выпусков на GitHub ещё нет — молчим
    info.update(latest=version, tag=tag, source=source,
                update_available=parse_version(version) > parse_version(current))
    return info


def archive_url(tag: str) -> str:
    return ARCHIVE.format(repo=REPO, tag=tag)


def extract_package(blob: bytes, dest: Path) -> Path:
    """Распаковать пакет `step_pipeline/` из архива тега в *dest* и вернуть *dest*.

    Из архива берётся только каталог пакета: репозиторий содержит ещё docs/, tests/ и
    CI, и ставить их в plugins/ незачем. Симлинки и пути с `..` пропускаются — архив
    чужой, доверять его разметке нельзя.
    """
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tar:
        members = [member for member in tar.getmembers() if member.isfile()]
        prefix = ""
        for member in members:
            parts = Path(member.name).parts
            if len(parts) >= 2 and parts[-2] == PACKAGE and parts[-1] == "plugin.yaml":
                prefix = "/".join(parts[:-1]) + "/"
                break
        if not prefix:
            raise UpdateError("в архиве нет пакета step_pipeline (plugin.yaml не найден)")
        for member in tar.getmembers():
            if not member.name.startswith(prefix):
                continue
            relative = member.name[len(prefix):].strip("/")
            if not relative or Path(relative).is_absolute() or ".." in Path(relative).parts:
                continue
            target = dest / relative
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            if not member.isfile():
                continue
            source = tar.extractfile(member)
            if source is None:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(source.read())
    return dest


def _backup_root(package: Path) -> Path:
    """Куда класть прежнюю копию: рядом с `plugins/`, как это делает мастер установки."""
    return package.parent.parent / "plugin-backups" if package.parent.name == "plugins" \
        else package.parent / "plugin-backups"


def _backup(package: Path, current: str) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    target = _backup_root(package) / f"{PACKAGE}-{current or '0'}-{stamp}"
    shutil.copytree(package, target,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    return target


def _replace_files(source: Path, target: Path) -> None:
    """Переписать файлы пакета. Лишние файлы прежней версии не удаляются.

    Удаление «того, чего нет в новой версии» задевало бы чужие файлы, если владелец
    что-то дописал в каталог плагина; осиротевший модуль без импорта безвреден.
    """
    for path in sorted(source.rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts:
            continue
        destination = target / path.relative_to(source)
        data = path.read_bytes()
        if destination.is_file() and destination.read_bytes() == data:
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)


def install(explicit: Path | str | None = None, fetch=None,
            timeout: float = DEFAULT_TIMEOUT, tag: str = "") -> dict:
    """Скачать выпуск *tag* (по умолчанию последний) и переписать пакет."""
    fetch = fetch or _http_get
    package = package_dir(explicit)
    current = local_version(package)
    if not tag:
        _version, tag, _source = latest(fetch, timeout)
    try:
        blob = fetch(archive_url(tag), ARCHIVE_TIMEOUT)
    except Exception as exc:                                   # noqa: BLE001
        raise UpdateError(f"архив {tag}: {type(exc).__name__}: {exc}") from exc
    with tempfile.TemporaryDirectory(prefix="step_pipeline-update-") as tmp:
        fresh = extract_package(blob, Path(tmp) / "pkg")
        new_version = local_version(fresh)
        if not parse_version(new_version):
            raise UpdateError("в архиве нет версии (plugin.yaml)")
        if parse_version(new_version) <= parse_version(current):
            return {"ok": True, "updated": False, "from": current, "to": current,
                    "latest": new_version, "error": "",
                    "message": f"уже актуально: v{current}"}
        backup = _backup(package, current)
        _replace_files(fresh, package)
    after = local_version(package)
    return {"ok": True, "updated": after != current, "from": current, "to": after,
            "latest": new_version, "backup": str(backup), "error": "",
            "message": f"плагин обновлён: v{current} → v{after}",
            "restart": "перезапусти сессию Hermes; движок проектов — "
                       "`hermes pipeline setup --upgrade`"}


def auto(explicit: Path | str | None = None, fetch=None,
         timeout: float = DEFAULT_TIMEOUT) -> dict:
    """Проверить и обновить, если вышла новая версия. Молча, когда обновления нет."""
    info = check(explicit, fetch=fetch, timeout=timeout)
    if info["error"] or not info["update_available"]:
        info.update(ok=not info["error"], updated=False)
        return info
    result = install(explicit, fetch=fetch, timeout=timeout, tag=info["tag"])
    result.update(current=result.get("to") or info["current"], latest=info["latest"],
                  tag=info["tag"], source=info["source"], checked_at=info["checked_at"])
    return result

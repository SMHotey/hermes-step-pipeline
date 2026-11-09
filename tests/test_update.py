# -*- coding: utf-8 -*-
"""Автообновление плагина из GitHub: проверка релиза, архив, установка, CLI.

Сеть не трогается: `fetch` подменяется словарём «адрес → байты», а архив тега
собирается в памяти тем же tarfile. Проверяем то, что ломается молча: разбор версий,
выбор между релизом и тегами, отказ на битом архиве и то, что установка кладёт ровно
пакет (без docs/ и tests/), сохраняя прежнюю копию.
"""
from __future__ import annotations

import io
import json
import tarfile
import urllib.error
from argparse import Namespace
from pathlib import Path

import helpers  # noqa: F401  (загружает плагин и алиасы)

import cli
from step_pipeline import update


def _plugin_yaml(version: str) -> bytes:
    return ("name: step_pipeline\nversion: %s\ndescription: тест\n" % version).encode("utf-8")


def _archive(tag: str, version: str, extra: dict[str, bytes] | None = None) -> bytes:
    """Архив в форме GitHub: `<repo>-<tag>/step_pipeline/...` плюс лишние каталоги."""
    root = f"hermes-step-pipeline-{version}"
    files = {
        f"{root}/step_pipeline/plugin.yaml": _plugin_yaml(version),
        f"{root}/step_pipeline/ui/index.html": b"<html>new</html>",
        f"{root}/docs/CONTEXT.md": b"internal",
        f"{root}/tests/test_x.py": b"def test_x(): pass",
    }
    files.update(extra or {})
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def _installed(tmp_path: Path, version: str = "0.1.0") -> Path:
    """Копия плагина как её ставит мастер: `<home>/plugins/step_pipeline`."""
    package = tmp_path / "home" / "plugins" / update.PACKAGE
    (package / "ui").mkdir(parents=True)
    (package / "plugin.yaml").write_bytes(_plugin_yaml(version))
    (package / "ui" / "index.html").write_bytes(b"<html>old</html>")
    (package / "engine").mkdir()
    (package / "engine" / "gate.py").write_bytes(b"# old engine\n")
    return package


def _fetch(routes: dict[str, bytes]):
    calls: list[str] = []

    def fetch(url: str, timeout: float) -> bytes:
        calls.append(url)
        if url not in routes:
            raise update.UpdateError(f"нет маршрута: {url}")
        return routes[url]

    fetch.calls = calls  # type: ignore[attr-defined]
    return fetch


def _release(version: str) -> bytes:
    return json.dumps({"tag_name": f"v{version}", "name": f"v{version}"}).encode("utf-8")


def test_parse_version_orders_and_ignores_junk():
    assert update.parse_version("v0.17.0") == (0, 17, 0)
    assert update.parse_version("1.2.10") > update.parse_version("1.2.9")
    assert update.parse_version("") == ()
    assert update.parse_version("nightly") == ()


def test_latest_prefers_release_then_falls_back_to_tags():
    fetch = _fetch({update.RELEASES_API: _release("0.18.0")})
    assert update.latest(fetch) == ("0.18.0", "v0.18.0", "release")

    tags = json.dumps([{"name": "v0.9.0"}, {"name": "v0.10.0"}, {"name": "мусор"}]).encode()
    fetch = _fetch({update.TAGS_API: tags})
    assert update.latest(fetch)[0] == "0.10.0"
    assert update.latest(fetch)[2] == "tags"


def test_latest_without_network_is_an_error_not_up_to_date():
    def broken(url: str, timeout: float) -> bytes:
        raise OSError("сети нет")

    info = update.check(fetch=broken, timeout=0.1)
    assert info["update_available"] is False
    assert "сети нет" in info["error"]


def test_github_token_is_taken_from_env_and_never_required(monkeypatch):
    """Токен — только для private-репо: берём из окружения, пустое значение — «нет»."""
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    assert update.github_token() == ""
    monkeypatch.setenv("GITHUB_TOKEN", "  ")
    monkeypatch.setenv("GH_TOKEN", "gh-token")
    assert update.github_token() == "gh-token"
    monkeypatch.setenv("GITHUB_TOKEN", "main-token")
    assert update.github_token() == "main-token"


def test_no_releases_yet_is_quiet_not_an_error(tmp_path: Path):
    """Репозиторий без выпусков и тегов: 404 на обоих адресах — «нечего ставить», не сбой."""
    def nothing(url: str, timeout: float) -> bytes:
        raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)  # type: ignore[arg-type]

    package = _installed(tmp_path, "0.1.0")
    info = update.check(package, fetch=nothing, timeout=0.1)
    assert info["error"] == "" and info["latest"] == ""
    assert info["update_available"] is False


def test_check_sees_newer_release(tmp_path: Path):
    package = _installed(tmp_path, "0.1.0")
    info = update.check(package, fetch=_fetch({update.RELEASES_API: _release("0.2.0")}))
    assert info["current"] == "0.1.0" and info["latest"] == "0.2.0"
    assert info["update_available"] is True and info["error"] == ""


def test_extract_keeps_only_package_and_drops_escaping_paths(tmp_path: Path):
    blob = _archive("v0.2.0", "0.2.0", {"hermes-step-pipeline-0.2.0/step_pipeline/../../evil.py": b"x"})
    root = update.extract_package(blob, tmp_path / "pkg")
    names = sorted(path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file())
    assert names == ["plugin.yaml", "ui/index.html"]
    assert (root / "plugin.yaml").read_bytes() == _plugin_yaml("0.2.0")


def test_extract_refuses_archive_without_package(tmp_path: Path):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        info = tarfile.TarInfo("repo/README.md")
        info.size = 2
        tar.addfile(info, io.BytesIO(b"#!"))
    try:
        update.extract_package(buffer.getvalue(), tmp_path / "pkg")
    except update.UpdateError as exc:
        assert "step_pipeline" in str(exc)
    else:                                                       # pragma: no cover
        raise AssertionError("битый архив принят молча")


def test_install_rewrites_package_and_keeps_backup(tmp_path: Path):
    package = _installed(tmp_path, "0.1.0")
    fetch = _fetch({update.RELEASES_API: _release("0.2.0"),
                    update.archive_url("v0.2.0"): _archive("v0.2.0", "0.2.0")})
    result = update.install(package, fetch=fetch)

    assert result["ok"] is True and result["updated"] is True
    assert result["from"] == "0.1.0" and result["to"] == "0.2.0"
    assert (package / "plugin.yaml").read_bytes() == _plugin_yaml("0.2.0")
    assert (package / "ui" / "index.html").read_bytes() == b"<html>new</html>"
    # Движка в архиве нет — прежний файл остаётся: удалять чужое установка не вправе.
    assert (package / "engine" / "gate.py").is_file()
    backup = Path(result["backup"])
    assert (backup / "plugin.yaml").read_bytes() == _plugin_yaml("0.1.0")
    assert "setup --upgrade" in result["restart"]


def test_install_same_version_does_not_touch_files(tmp_path: Path):
    package = _installed(tmp_path, "0.2.0")
    stamp = (package / "plugin.yaml").stat().st_mtime_ns
    blob = _archive("v0.2.0", "0.2.0")
    fetch = _fetch({update.RELEASES_API: _release("0.2.0"),
                    update.archive_url("v0.2.0"): blob})
    result = update.install(package, fetch=fetch)
    assert result["updated"] is False and "актуально" in result["message"]
    assert (package / "plugin.yaml").stat().st_mtime_ns == stamp
    assert not (tmp_path / "home" / "plugin-backups").exists()


def test_auto_skips_install_when_up_to_date(tmp_path: Path):
    package = _installed(tmp_path, "0.2.0")
    fetch = _fetch({update.RELEASES_API: _release("0.2.0")})
    result = update.auto(package, fetch=fetch)
    assert result["updated"] is False and result["ok"] is True
    assert fetch.calls == [update.RELEASES_API]                # архив не качался


def test_cli_check_reports_without_installing(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setattr(update, "check", lambda **_: {
        "current": "0.1.0", "latest": "0.2.0", "tag": "v0.2.0", "source": "release",
        "update_available": True, "error": "", "checked_at": "2026-11-09T00:00:00+00:00"})

    def forbidden(*_args, **_kwargs):                           # pragma: no cover
        raise AssertionError("--check не должен ставить обновление")

    monkeypatch.setattr(update, "install", forbidden)
    code = cli.cmd_update(Namespace(check=True, json=False, timeout=0))
    out = capsys.readouterr().out
    assert code == 0
    assert "v0.1.0 → v0.2.0" in out and "hermes pipeline update" in out


def test_cli_update_installs_and_prints_restart(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setattr(update, "check", lambda **_: {
        "current": "0.1.0", "latest": "0.2.0", "tag": "v0.2.0", "source": "release",
        "update_available": True, "error": "", "checked_at": "2026-11-09T00:00:00+00:00"})
    monkeypatch.setattr(update, "install", lambda **_kwargs: {
        "ok": True, "updated": True, "from": "0.1.0", "to": "0.2.0",
        "message": "плагин обновлён: v0.1.0 → v0.2.0", "backup": "/tmp/backup",
        "restart": "перезапусти сессию Hermes"})
    code = cli.cmd_update(Namespace(check=False, json=False, timeout=0))
    out = capsys.readouterr().out
    assert code == 0
    assert "плагин обновлён: v0.1.0 → v0.2.0" in out
    assert "перезапусти сессию Hermes" in out


def test_cli_update_reports_network_failure(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setattr(update, "check", lambda **_: {
        "current": "0.1.0", "latest": "", "tag": "", "source": "",
        "update_available": False, "error": "OSError: сети нет",
        "checked_at": "2026-11-09T00:00:00+00:00"})
    assert cli.cmd_update(Namespace(check=False, json=False, timeout=0)) == 1
    assert "сети нет" in capsys.readouterr().err


def test_parser_has_update_command():
    """Подкоманда `update` объявлена в парсере плагина, флаги — как обещает README."""
    import argparse
    parser = argparse.ArgumentParser()
    cli.build_parser(parser)
    args = parser.parse_args(["update", "--check"])
    assert args.handler is cli.cmd_update and args.check is True and args.timeout == 0

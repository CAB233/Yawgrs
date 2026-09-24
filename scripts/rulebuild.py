#!/usr/bin/env python3
"""Build isolated rule packages described by rules/*/build.toml."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import tomllib
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
IDENTIFIER = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
HASH = re.compile(r"^[0-9a-fA-F]{64}$")
DEBUG_ENABLED = False
LEVEL_COLORS = {"DEBUG": "36", "INFO": "32", "WARNING": "33", "ERROR": "31"}
ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
TOOL_LEVEL = re.compile(r"^(?:\[(DEBUG|INFO|WARN|WARNING|ERROR|FATAL)\]|(DEBUG|INFO|WARN|WARNING|ERROR|FATAL)(?=\[|\s))\s*")


def log(level: str, message: str) -> None:
    if level == "DEBUG" and not DEBUG_ENABLED:
        return
    prefix = f"[{level}]"
    if sys.stderr.isatty() and not os.environ.get("NO_COLOR") and os.environ.get("TERM") != "dumb":
        color = LEVEL_COLORS.get(level)
        if color:
            prefix = f"\x1b[{color}m{prefix}\x1b[0m"
    print(f"{prefix} {message}", file=sys.stderr, flush=True)


def log_tool_line(line: str, label: str) -> None:
    line = ANSI.sub("", line.rstrip("\r\n"))
    if not line:
        return
    match = TOOL_LEVEL.match(line)
    level = "INFO"
    if match:
        level = match.group(1) or match.group(2)
        level = {"FATAL": "ERROR", "WARN": "WARNING"}.get(level, level)
        line = line[match.end():]
    log(level, f"{label}: {line}")


class BuildError(Exception):
    pass


def read_toml(path: Path) -> dict:
    with path.open("rb") as stream:
        return tomllib.load(stream)


def check_id(value: object, context: str) -> str:
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise BuildError(f"{context}: expected lowercase letters, digits, - or _: {value!r}")
    return value


def check_commands(value: object, context: str, required: bool = False) -> list[str]:
    if value is None and not required:
        return []
    if not isinstance(value, list) or not value or any(not isinstance(item, str) or not item.strip() for item in value):
        raise BuildError(f"{context}: command must be a nonempty array of strings")
    return value


def source_filenames(sources: dict) -> dict[str, str]:
    filenames = {}
    for source_id, source in sources.items():
        filename = source.get("rename", source_id)
        if (not isinstance(filename, str) or not filename or filename in (".", "..")
                or "/" in filename or "\\" in filename or "\x00" in filename):
            raise BuildError(f"source.{source_id}.rename: expected a filename without directories")
        if filename in filenames.values():
            raise BuildError(f"source.{source_id}: duplicate source filename: {filename}")
        filenames[source_id] = filename
    return filenames


def discover(root: Path = ROOT) -> dict[str, tuple[Path, dict]]:
    packages = {}
    for manifest in sorted((root / "rules").glob("*/build.toml")):
        data = read_toml(manifest)
        name = check_id(data.get("name"), str(manifest))
        if name != manifest.parent.name or name in packages:
            raise BuildError(f"{manifest}: name must equal its unique directory name")
        if not isinstance(data.get("description"), str):
            raise BuildError(f"{manifest}: description must be a string")
        dependencies = data.get("depends", [])
        if not isinstance(dependencies, list) or len(dependencies) != len(set(dependencies)):
            raise BuildError(f"{manifest}: depends must be an array of unique names")
        for dependency in dependencies:
            check_id(dependency, f"{name}.depends")
        sources = data.get("source", {})
        if not isinstance(sources, dict):
            raise BuildError(f"{manifest}: source must be a table")
        for source_id, source in sources.items():
            check_id(source_id, f"{name}.source")
            if not isinstance(source, dict) or not isinstance(source.get("src"), str):
                raise BuildError(f"{name}.source.{source_id}: src must be a string")
            digest = source.get("sha256")
            if digest != "SKIP" and (not isinstance(digest, str) or not HASH.fullmatch(digest)):
                raise BuildError(f"{name}.source.{source_id}: sha256 must be 64 hex characters or SKIP")
            src = source["src"]
            if not src.startswith(("https://", "http://")):
                local = Path(src)
                resolved = (manifest.parent / local).resolve()
                if local.is_absolute() or manifest.parent.resolve() not in resolved.parents or not resolved.is_file():
                    raise BuildError(f"{name}.source.{source_id}: local file must stay within package directory")
        source_filenames(sources)
        build = data.get("build")
        if not isinstance(build, dict):
            raise BuildError(f"{manifest}: [build] is required")
        kind = check_id(build.get("type"), f"{name}.build.type")
        if kind == "self":
            check_commands(build.get("command"), f"{name}.build", required=True)
        else:
            if "command" in build:
                raise BuildError(f"{name}.build: command is only allowed when type = self")
            if not (root / "templates" / f"{kind}.sh").is_file():
                raise BuildError(f"{name}.build: missing templates/{kind}.sh")
            for key, value in build.items():
                if key == "type":
                    continue
                if not re.fullmatch(r"[a-z][a-z0-9_]*", key) or not isinstance(value, (str, int, bool)):
                    raise BuildError(f"{name}.build.{key}: template parameter must be a scalar with a shell-safe key")
        for phase in ("prepare", "beyond"):
            section = data.get(phase, {})
            if not isinstance(section, dict):
                raise BuildError(f"{name}.{phase}: expected a table")
            check_commands(section.get("command"), f"{name}.{phase}")
        packages[name] = (manifest.parent, data)
    if not packages:
        raise BuildError("no rules/*/build.toml packages found")
    return packages


def order_packages(packages: dict, selected: list[str] | None = None) -> list[str]:
    state: dict[str, int] = {}
    result: list[str] = []

    def visit(name: str) -> None:
        if name not in packages:
            raise BuildError(f"unknown package or dependency: {name}")
        if state.get(name) == 1:
            raise BuildError(f"dependency cycle at {name}")
        if state.get(name) == 2:
            return
        state[name] = 1
        for dependency in packages[name][1].get("depends", []):
            visit(dependency)
        state[name] = 2
        result.append(name)

    for name in selected or sorted(packages):
        visit(name)
    return result


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def fetch_sources(name: str, package: Path, sources: dict, srcdir: Path, locked: dict) -> list[dict]:
    records = []
    filenames = source_filenames(sources)
    for source_id, source in sources.items():
        target = srcdir / filenames[source_id]
        location = source["src"]
        log("INFO", f"{name}: source {source_id} -> {target.name}")
        log("DEBUG", f"{name}: source destination {target}")
        if location.startswith(("https://", "http://")):
            headers = {"User-Agent": "Yawgrs/1"}
            token = os.environ.get("GITHUB_TOKEN")
            if token and "github.com" in location:
                headers["Authorization"] = f"Bearer {token}"
            request = urllib.request.Request(location, headers=headers)
            with urllib.request.urlopen(request, timeout=120) as response, target.open("wb") as output:
                shutil.copyfileobj(response, output)
        else:
            shutil.copyfile(package / location, target)
        actual = sha256(target)
        expected = source["sha256"]
        if expected != "SKIP" and actual.lower() != expected.lower():
            raise BuildError(f"{name}/{source_id}: SHA-256 mismatch: expected {expected}, got {actual}")
        lock_digest = locked.get((name, source_id))
        if lock_digest and actual != lock_digest:
            raise BuildError(f"{name}/{source_id}: content differs from lock file")
        log("DEBUG", f"{name}/{source_id}: sha256={actual}, verification={'SKIP' if expected == 'SKIP' else 'OK'}")
        records.append({"package": name, "id": source_id, "src": location, "filename": filenames[source_id], "sha256": actual, "verified": expected != "SKIP"})
    return records


def run_commands(commands: list[str], cwd: Path, env: dict[str, str], label: str) -> None:
    if not commands:
        log("DEBUG", f"{label}: skipped (no commands)")
        return
    log("INFO", label)
    log("DEBUG", f"{label}: working directory {cwd}")
    command = ["bash", "-euo", "pipefail", "-c", "\n".join(commands)]
    with subprocess.Popen(command, cwd=cwd, env=env, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, text=True, errors="replace", bufsize=1) as process:
        for line in process.stdout:
            log_tool_line(line, label)
        returncode = process.wait()
    if returncode:
        raise BuildError(f"{label}: failed (exit code {returncode})")
    log("DEBUG", f"{label}: completed")


def build_package(name: str, package: Path, data: dict, stage: Path, workspace: Path, locked: dict) -> tuple[list[dict], list[dict]]:
    base = workspace / name
    srcdir, builddir, pkgdir, depsdir = (base / part for part in ("src", "build", "pkg", "deps"))
    for directory in (srcdir, builddir, pkgdir, depsdir):
        directory.mkdir(parents=True)
        log("DEBUG", f"{name}: created {directory}")
    for dependency in data.get("depends", []):
        shutil.copytree(stage / dependency, depsdir / dependency)
    env = dict(os.environ)
    env.update({
        "RULEDIR": str(package.resolve()), "SRCDIR": str(srcdir.resolve()),
        "BUILDDIR": str(builddir.resolve()), "PKGDIR": str(pkgdir.resolve()),
        "DEPSDIR": str(depsdir.resolve()), "RULE_NAME": name,
    })
    records = fetch_sources(name, package, data.get("source", {}), srcdir, locked)
    env["RULE_SOURCES_JSON"] = json.dumps({
        item["id"]: str((srcdir / item["filename"]).resolve()) for item in records
    })
    run_commands(data.get("prepare", {}).get("command", []), builddir, env, f"{name}: prepare")
    build = data["build"]
    if build["type"] == "self":
        run_commands(build["command"], builddir, env, f"{name}: build")
    else:
        for key, value in build.items():
            if key != "type":
                if not isinstance(value, (str, int, bool)):
                    raise BuildError(f"{name}.build.{key}: template parameter must be scalar")
                env[f"RULE_PARAM_{key.upper()}"] = str(value)
        template = ROOT / "templates" / f"{build['type']}.sh"
        run_commands([f"bash {shlex.quote(str(template))}"], builddir, env, f"{name}: template {build['type']}")
    run_commands(data.get("beyond", {}).get("command", []), builddir, env, f"{name}: beyond")
    entries = sorted(pkgdir.rglob("*"))
    files = [path for path in entries if path.is_file()]
    if not files or any(path.is_symlink() or not (path.is_file() or path.is_dir()) for path in entries):
        raise BuildError(f"{name}: PKGDIR must contain regular files and directories")
    destination = stage / name
    shutil.copytree(pkgdir, destination)
    artifacts = []
    for path in sorted(item for item in destination.rglob("*") if item.is_file()):
        artifacts.append({"package": name, "path": path.relative_to(stage).as_posix(), "sha256": sha256(path), "size": path.stat().st_size})
    log("INFO", f"{name}: collected {len(artifacts)} files")
    return records, artifacts


def build_all(args: argparse.Namespace, packages: dict, ordered: list[str]) -> None:
    output = args.output.resolve()
    allowed = ROOT in output.parents or Path("/tmp") in output.parents
    protected = [ROOT / "rules", ROOT / "templates", ROOT / "scripts", ROOT / "tests", ROOT / ".github", ROOT / ".git"]
    if not allowed or any(path == output or path in output.parents for path in protected) or output == ROOT:
        raise BuildError("output must be a path beneath the repository or /tmp, outside source directories")
    if output.exists() and not output.is_dir():
        raise BuildError(f"output exists and is not a directory: {output}")
    locked = {}
    if args.lock:
        for source in json.loads(args.lock.read_text(encoding="utf-8"))["sources"]:
            locked[(source["package"], source["id"])] = source["sha256"]
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".yawgrs-build-", dir=output.parent) as temp:
        workspace = Path(temp)
        stage = workspace / "stage"
        stage.mkdir()
        sources: list[dict] = []
        artifacts: list[dict] = []
        for name in ordered:
            log("INFO", f"Building {name}")
            package, data = packages[name]
            source_rows, artifact_rows = build_package(name, package, data, stage, workspace, locked)
            sources.extend(source_rows)
            artifacts.extend(artifact_rows)
        (stage / "sources.lock.json").write_text(json.dumps({"schema": 1, "sources": sources}, indent=2, sort_keys=True) + "\n")
        (stage / "index.json").write_text(json.dumps({"schema": 1, "artifacts": artifacts}, indent=2, sort_keys=True) + "\n")
        if output.exists():
            backup = workspace / "previous-output"
            output.rename(backup)
            try:
                stage.rename(output)
            except BaseException:
                if output.exists():
                    shutil.rmtree(output)
                backup.rename(output)
                raise
        else:
            stage.rename(output)
    log("INFO", f"Built {len(ordered)} packages, {len(artifacts)} files into {output}")


def main() -> int:
    global DEBUG_ENABLED
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["validate", "build"])
    parser.add_argument("--package", action="append")
    parser.add_argument("--output", type=Path, default=ROOT / "dist")
    parser.add_argument("--lock", type=Path)
    parser.add_argument("--debug", action="store_true", help="show DEBUG logs including workspace paths and checksums")
    args = parser.parse_args()
    DEBUG_ENABLED = args.debug
    try:
        packages = discover()
        ordered = order_packages(packages, args.package)
        if args.command == "build":
            build_all(args, packages, ordered)
        else:
            log("INFO", f"Validated {len(packages)} packages")
    except (BuildError, OSError, subprocess.CalledProcessError, ValueError, tomllib.TOMLDecodeError) as error:
        log("ERROR", str(error))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

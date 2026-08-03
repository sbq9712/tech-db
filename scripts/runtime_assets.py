#!/usr/bin/env python3
"""Install and verify versioned Tech-DB runtime assets from GitHub Releases.

This bootstrap intentionally uses only the Python standard library so it can run
before the project's virtual environment and third-party dependencies exist.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tarfile
import time
import urllib.error
import urllib.request
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = REPO_ROOT / "runtime-assets.json"
DEFAULT_RUNTIME_DIR = REPO_ROOT / "runtime"
USER_AGENT = "tech-db-runtime-installer/1"


class AssetError(RuntimeError):
    """A runtime asset could not be downloaded, verified, or installed."""


def load_config(path: Path = CONFIG_PATH) -> dict:
    config = json.loads(path.read_text(encoding="utf-8"))
    if config.get("schema_version") != 1:
        raise AssetError(f"Unsupported runtime manifest schema: {config.get('schema_version')}")
    return config


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def parse_checksums(text: str) -> dict[str, str]:
    checksums: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(maxsplit=1)
        if len(parts) != 2 or len(parts[0]) != 64:
            raise AssetError(f"Invalid checksum line: {raw_line!r}")
        name = parts[1].lstrip("* ")
        checksums[name] = parts[0].lower()
    return checksums


def _request(url: str) -> urllib.request.Request:
    return urllib.request.Request(
        url,
        headers={"Accept": "application/vnd.github+json", "User-Agent": USER_AGENT},
    )


def fetch_release_assets(repository: str, tag: str) -> dict[str, str]:
    api_url = f"https://api.github.com/repos/{repository}/releases/tags/{tag}"
    try:
        with urllib.request.urlopen(_request(api_url), timeout=30) as response:
            release = json.load(response)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise AssetError(
                f"Release {tag!r} is not available yet. Ask an administrator to run "
                "the 'Publish runtime assets' GitHub Action."
            ) from exc
        raise AssetError(f"GitHub returned HTTP {exc.code} for {api_url}") from exc
    except urllib.error.URLError as exc:
        raise AssetError(f"Cannot reach GitHub Releases: {exc.reason}") from exc

    assets = {
        asset["name"]: asset["browser_download_url"]
        for asset in release.get("assets", [])
        if asset.get("name") and asset.get("browser_download_url")
    }
    if not assets:
        raise AssetError(f"Release {tag!r} has no downloadable assets")
    return assets


def download(url: str, destination: Path, expected_sha256: str | None = None) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() and expected_sha256:
        if sha256_file(destination) == expected_sha256:
            print(f"  reuse {destination.name} (checksum OK)")
            return

    temporary = destination.with_name(destination.name + ".download")
    for attempt in range(1, 4):
        try:
            print(f"  download {destination.name} (attempt {attempt}/3)")
            with urllib.request.urlopen(_request(url), timeout=120) as response:
                with temporary.open("wb") as output:
                    shutil.copyfileobj(response, output, length=1024 * 1024)
            if expected_sha256:
                actual = sha256_file(temporary)
                if actual != expected_sha256:
                    raise AssetError(
                        f"Checksum mismatch for {destination.name}: expected "
                        f"{expected_sha256}, got {actual}"
                    )
            os.replace(temporary, destination)
            return
        except (OSError, urllib.error.URLError, AssetError) as exc:
            temporary.unlink(missing_ok=True)
            if attempt == 3:
                raise AssetError(f"Failed to download {destination.name}: {exc}") from exc
            time.sleep(attempt * 2)


def safe_extract(archive: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    root = destination.resolve()
    with tarfile.open(archive, "r:gz") as bundle:
        members = bundle.getmembers()
        for member in members:
            target = (destination / member.name).resolve()
            if target != root and root not in target.parents:
                raise AssetError(f"Unsafe path in archive: {member.name}")
            if member.issym() or member.islnk():
                raise AssetError(f"Links are not allowed in runtime archive: {member.name}")
        bundle.extractall(destination, members=members)


def combine_parts(parts: list[Path], destination: Path) -> None:
    if not parts:
        raise AssetError("No archive parts were provided")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".assembling")
    with temporary.open("wb") as output:
        for part in parts:
            print(f"  assemble {part.name}")
            with part.open("rb") as source:
                shutil.copyfileobj(source, output, length=1024 * 1024)
    os.replace(temporary, destination)


def required_paths(config: dict, component: str, runtime_dir: Path) -> tuple[list[Path], list[Path]]:
    component_config = config["components"][component]
    required_all = [runtime_dir / rel for rel in component_config.get("required_files", [])]
    required_any = [runtime_dir / rel for rel in component_config.get("required_any", [])]
    return required_all, required_any


def component_ready(config: dict, component: str, runtime_dir: Path) -> bool:
    required_all, required_any = required_paths(config, component, runtime_dir)
    return all(path.is_file() for path in required_all) and (
        not required_any or any(path.is_file() for path in required_any)
    )


def verify_runtime(config: dict, runtime_dir: Path, components: list[str]) -> None:
    failures: list[str] = []
    for component in components:
        required_all, required_any = required_paths(config, component, runtime_dir)
        failures.extend(str(path) for path in required_all if not path.is_file())
        if required_any and not any(path.is_file() for path in required_any):
            failures.append("one of: " + ", ".join(str(path) for path in required_any))
    if failures:
        raise AssetError("Runtime verification failed; missing:\n  - " + "\n  - ".join(failures))


def install_runtime(runtime_dir: Path, selected: list[str], force: bool = False) -> None:
    config = load_config()
    pending = [name for name in selected if force or not component_ready(config, name, runtime_dir)]
    if not pending:
        print("Runtime assets are already installed.")
        verify_runtime(config, runtime_dir, selected)
        return

    assets = fetch_release_assets(config["repository"], config["release_tag"])
    checksum_name = config["checksum_asset"]
    if checksum_name not in assets:
        raise AssetError(f"Release is missing {checksum_name}")

    cache_dir = runtime_dir / ".downloads" / config["release_tag"]
    checksum_file = cache_dir / checksum_name
    download(assets[checksum_name], checksum_file)
    checksums = parse_checksums(checksum_file.read_text(encoding="utf-8"))

    if "indexes" in pending:
        name = config["components"]["indexes"]["asset"]
        if name not in assets or name not in checksums:
            raise AssetError(f"Release is missing indexed asset or checksum: {name}")
        archive = cache_dir / name
        download(assets[name], archive, checksums[name])
        safe_extract(archive, runtime_dir)

    if "model" in pending:
        prefix = config["components"]["model"]["asset_prefix"]
        names = sorted(name for name in assets if name.startswith(prefix))
        if not names:
            raise AssetError(f"Release has no model parts matching {prefix}*")
        parts: list[Path] = []
        for name in names:
            if name not in checksums:
                raise AssetError(f"Release checksum list is missing {name}")
            part = cache_dir / name
            download(assets[name], part, checksums[name])
            parts.append(part)
        combined = cache_dir / "bge-m3-model.tar.gz"
        combine_parts(parts, combined)
        safe_extract(combined, runtime_dir)
        combined.unlink(missing_ok=True)

    verify_runtime(config, runtime_dir, selected)
    state = {
        "release_tag": config["release_tag"],
        "installed_components": selected,
        "installed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    (runtime_dir / "install-state.json").write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"Runtime ready at {runtime_dir}")


def create_offline_package(runtime_dir: Path, output: Path) -> None:
    config = load_config()
    verify_runtime(config, runtime_dir, ["indexes", "model"])
    output.parent.mkdir(parents=True, exist_ok=True)
    excluded_roots = {".git", ".venv", "node_modules", "dist", "runtime"}
    with tarfile.open(output, "w:gz") as archive:
        for path in sorted(REPO_ROOT.rglob("*")):
            relative = path.relative_to(REPO_ROOT)
            if relative.parts and relative.parts[0] in excluded_roots:
                continue
            archive.add(path, arcname=Path("tech-db") / relative, recursive=False)
        archive.add(runtime_dir, arcname=Path("tech-db") / "runtime")
    print(f"Offline package created: {output}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--runtime-dir",
        type=Path,
        default=Path(os.environ.get("TECH_DB_RUNTIME_DIR", DEFAULT_RUNTIME_DIR)),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    install = subparsers.add_parser("install", help="download and install release assets")
    install.add_argument(
        "--components", choices=("all", "indexes", "model"), default="all"
    )
    install.add_argument("--force", action="store_true")

    verify = subparsers.add_parser("verify", help="verify installed runtime assets")
    verify.add_argument(
        "--components", choices=("all", "indexes", "model"), default="all"
    )

    package = subparsers.add_parser("package-offline", help="create a complete offline tarball")
    package.add_argument("--output", type=Path, default=REPO_ROOT / "dist" / "tech-db-offline.tar.gz")

    combine = subparsers.add_parser("combine", help="combine a split release archive")
    combine.add_argument("--output", type=Path, required=True)
    combine.add_argument("parts", nargs="+", type=Path)

    args = parser.parse_args()
    components = ["indexes", "model"] if getattr(args, "components", "all") == "all" else [args.components]
    try:
        if args.command == "install":
            install_runtime(args.runtime_dir.resolve(), components, force=args.force)
        elif args.command == "verify":
            verify_runtime(load_config(), args.runtime_dir.resolve(), components)
            print(f"Runtime verification passed: {args.runtime_dir.resolve()}")
        elif args.command == "package-offline":
            create_offline_package(args.runtime_dir.resolve(), args.output.resolve())
        elif args.command == "combine":
            combine_parts([part.resolve() for part in args.parts], args.output.resolve())
    except AssetError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

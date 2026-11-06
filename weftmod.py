#!/usr/bin/env python3
"""The engine of weft-mod-action: plans, builds and packages Weft mods. Standard library only, and
Python 3.10 or newer, so that it runs on every GitHub runner as it is.

    plan        which mods to release, at which versions        (build/action.yml, on each runner)
    build       set those versions, `cargo build --release`, copy the libraries
    package     merge the runners' builds; each mod's `<id>.zip` and `<id>.mod.json`
                                                                 (release/action.yml)
    release     a GitHub release of each package
    latest-tags the newest release tag of each mod                (index-pr/action.yml)

A mod is a folder with a Cargo.toml that builds a `cdylib`; its id is that library's name. See
README.md for the rules.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

WEFT_REPO = "Weft-Rust/Weft"
ABI_FILE = "crates/abi/src/lib.rs"
PLATFORMS = ("windows-x64", "linux-x64", "macos-arm64")
LIBRARIES = {"windows-x64": "{id}.dll", "linux-x64": "lib{id}.so", "macos-arm64": "lib{id}.dylib"}
SUMMARY_MAX = 140
CORE_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)")
_PRE_ID = r"(?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*)"
SEMVER_RE = re.compile(CORE_RE.pattern + rf"(?:-{_PRE_ID}(?:\.{_PRE_ID})*)?$")
ID_RE = re.compile(r"^[a-z0-9_]+$")
WEFT_TAG_RE = re.compile(r"^v[0-9A-Za-z.\-]+$")


class Error(Exception):
    """An expected failure: printed as one `::error::` line."""


def log(msg: str) -> None:
    print(msg, file=sys.stderr)


# ---------------------------------------------------------------------------------------------
# GitHub


class GitHub:
    """GitHub's REST API, authenticated with GITHUB_TOKEN. Tests replace it with a fake."""

    def __init__(self, token: str | None = None):
        self.token = token if token is not None else os.environ.get("GITHUB_TOKEN", "")

    def api(self, path: str):
        req = urllib.request.Request("https://api.github.com" + path, headers={
            "Accept": "application/vnd.github+json", "User-Agent": "weft-mod-action"})
        if self.token:
            req.add_unredirected_header("Authorization", f"Bearer {self.token}")
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=60) as r:
                    return json.load(r)
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    return None
                if e.code < 500 or attempt == 2:
                    raise Error(f"GET {path}: HTTP {e.code} {e.reason}") from None
            except (OSError, ValueError) as e:
                if attempt == 2:
                    raise Error(f"GET {path}: {e}") from None
            time.sleep(2 ** attempt)

    def pages(self, path: str) -> list:
        out, page = [], 1
        while True:
            batch = self.api(f"{path}{'&' if '?' in path else '?'}per_page=100&page={page}") or []
            out += batch
            if len(batch) < 100:
                return out
            page += 1

    def raw(self, repo: str, commit: str, path: str) -> bytes | None:
        try:
            with urllib.request.urlopen(
                    f"https://raw.githubusercontent.com/{repo}/{commit}/{path}", timeout=60) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            raise Error(f"raw {repo}@{commit[:7]}/{path}: HTTP {e.code}") from None


def tag_exists(gh: GitHub, repo: str, tag: str) -> bool:
    return gh.api(f"/repos/{repo}/git/ref/tags/{tag}") is not None


# ---------------------------------------------------------------------------------------------
# Mods


def cargo_metadata(folder: Path, deps: bool = False) -> dict:
    cmd = ["cargo", "metadata", "--format-version", "1", "--manifest-path",
           str(folder / "Cargo.toml")]
    if not deps:
        cmd.append("--no-deps")
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise Error(f"cargo metadata in {folder}: {r.stderr.strip()}")
    return json.loads(r.stdout)


class Mod:
    """A mod folder, as `cargo metadata` describes it."""

    def __init__(self, folder: Path, metadata: dict | None = None):
        self.folder = folder
        self.metadata = metadata or cargo_metadata(folder)
        manifest = (folder / "Cargo.toml").resolve()
        self.package = next((p for p in self.metadata["packages"]
                             if Path(p["manifest_path"]).resolve() == manifest), None)
        if self.package is None:
            raise Error(f"{folder}: no package in Cargo.toml")
        lib = next((t for t in self.package["targets"] if "cdylib" in t["crate_types"]), None)
        if lib is None:
            raise Error(f"{folder}: Cargo.toml builds no cdylib, so it is no Weft mod")
        self.id = lib["name"].replace("-", "_")
        self.lib_rs = Path(lib["src_path"])
        self.version = self.package["version"]
        if not ID_RE.match(self.id):
            raise Error(f"{folder}: the library name {self.id!r} must match {ID_RE.pattern}")
        if not SEMVER_RE.match(self.version):
            raise Error(f"{folder}: version {self.version} is not X.Y.Z or X.Y.Z-<prerelease>")


def mod_folders(spec: str, root: Path) -> list[Path]:
    """The `mods` input: one folder per line (or separated by spaces), relative to the root."""
    folders = [root / f for f in spec.split()] or [root]
    for f in folders:
        if not (f / "Cargo.toml").is_file():
            raise Error(f"{f.relative_to(root) if f != root else '.'}: no Cargo.toml")
    return folders


# ---------------------------------------------------------------------------------------------
# plan


def rc_version(version: str, run: str, sha: str) -> str:
    """The release candidate made when `version` is already released: the next patch, marked with
    the run number and the commit (`g` + sha7, so that it is never a number with a leading zero)."""
    major, minor, patch = (int(x) for x in CORE_RE.match(version).groups())
    return f"{major}.{minor}.{patch + 1}-rc.{run}.g{sha[:7]}"


def tag_versions(gh: GitHub, repo: str, mod_id: str) -> list[str]:
    """The versions of the mod's `<id>-v<version>` tags, oldest first: releases and candidates."""
    refs = gh.api(f"/repos/{repo}/git/matching-refs/tags/{mod_id}-v") or []
    versions = [r["ref"].removeprefix(f"refs/tags/{mod_id}-v") for r in refs]
    return sorted((v for v in versions if SEMVER_RE.match(v)), key=semver_key)


def semver_key(version: str) -> tuple:
    core, _, pre = version.partition("-")
    nums = tuple(int(x) for x in core.split("."))
    if not pre:
        return nums, (1,)
    return nums, (0,) + tuple((0, int(p), "") if p.isdigit() else (1, 0, p)
                              for p in pre.split("."))


def changed_since(gh: GitHub, repo: str, tag: str, sha: str, paths: list[str]) -> bool:
    """Whether any file under `paths` (repository-relative; "" is everything) differs between the
    tag and the commit."""
    cmp = gh.api(f"/repos/{repo}/compare/{tag}...{sha}")
    if cmp is None or cmp.get("files") is None or len(cmp["files"]) >= 300:
        return True  # can't tell (GitHub lists at most 300 files): assume it changed
    return any(f["filename"] == p or f["filename"].startswith(p.rstrip("/") + "/") or p == ""
               for f in cmp["files"] for p in paths)


def weft_submodules(root: Path) -> list[str]:
    """The paths of the Weft submodules in .gitmodules: moving one changes every mod."""
    gitmodules = root / ".gitmodules"
    if not gitmodules.is_file():
        return []
    text = gitmodules.read_text(encoding="utf-8")
    return [m.group(1) for m in re.finditer(
        r"(?ms)^\s*path\s*=\s*(\S+)\s*$(?:(?!^\s*\[).)*?^\s*url\s*=\s*\S*Weft-Rust/Weft", text)]


def plan(gh: GitHub, root: Path, folders: list[Path], repo: str, sha: str, run: str,
         release_unchanged: bool = False) -> list[dict]:
    """One entry per mod to release: its folder, id, version and tag, `rc` or `release`, and the
    tag it follows. A mod whose version is released already becomes the next release candidate
    after it, or after the mod's newest full release if that is newer (candidates never raise it);
    unless nothing under its folder (or a Weft submodule) changed since its newest tag, when it is
    left out."""
    out = []
    submodules = weft_submodules(root)
    for folder in folders:
        mod = Mod(folder)
        rel = folder.resolve().relative_to(root.resolve()).as_posix()
        rel = "" if rel == "." else rel
        versions = tag_versions(gh, repo, mod.id)
        version, since = mod.version, f"{mod.id}-v{versions[-1]}" if versions else None
        if tag_exists(gh, repo, f"{mod.id}-v{version}"):
            if not release_unchanged and since and not changed_since(
                    gh, repo, since, sha, [rel, *submodules] if rel else [""]):
                log(f"{mod.id}: nothing changed since {since}, nothing to release")
                continue
            released = [v for v in versions if "-" not in v]
            version = rc_version(max([version, *released], key=semver_key), run, sha)
            if tag_exists(gh, repo, f"{mod.id}-v{version}"):
                raise Error(f"{mod.id}: {mod.id}-v{version} exists already; this run was released")
        kind = "release" if "-" not in version else "rc"
        out.append({"folder": rel or ".", "id": mod.id, "version": version,
                    "tag": f"{mod.id}-v{version}", "kind": kind, "since": since or ""})
        log(f"{mod.id}: {version} ({kind})")
    return out


# ---------------------------------------------------------------------------------------------
# build


def set_version(cargo_toml: Path, version: str) -> bool:
    """Sets `[package] version` in Cargo.toml, so the library carries the released version (Weft
    reads it from CARGO_PKG_VERSION). Not committed: it only changes this build's checkout."""
    text = cargo_toml.read_text(encoding="utf-8")
    m = re.search(r"(?ms)^\[package\]\s*$(.*?)(?=^\[|\Z)", text)
    if not m:
        return False
    section = m.group(1)
    new, n = re.subn(r'(?m)^(version\s*=\s*)"[^"]*"', rf'\g<1>"{version}"', section, count=1)
    if n == 0:  # e.g. version.workspace = true
        return False
    cargo_toml.write_text(text[:m.start(1)] + new + text[m.end(1):], encoding="utf-8")
    return True


def build(root: Path, entries: list[dict], out: Path, cargo_args: list[str]) -> None:
    """Builds each planned mod for this runner's system, and copies its library to `out/<id>/`."""
    for e in entries:
        folder = root / e["folder"]
        if not set_version(folder / "Cargo.toml", e["version"]):
            log(f"::warning::{e['id']}: can't set the version in {e['folder']}/Cargo.toml; the "
                f"library keeps its own")
        log(f"::group::building {e['id']} {e['version']}")
        r = subprocess.run(["cargo", "build", "--release", "--manifest-path",
                            str(folder / "Cargo.toml"), *cargo_args])
        log("::endgroup::")
        if r.returncode != 0:
            raise Error(f"{e['id']}: cargo build failed")
        target = Path(cargo_metadata(folder)["target_directory"]) / "release"
        found = [n.format(id=e["id"]) for n in LIBRARIES.values()
                 if (target / n.format(id=e["id"])).is_file()]
        if not found:
            raise Error(f"{e['id']}: the build made no {e['id']} library in {target}")
        (out / e["id"]).mkdir(parents=True, exist_ok=True)
        for name in found:
            shutil.copy2(target / name, out / e["id"] / name)


# ---------------------------------------------------------------------------------------------
# package: the zip


def _pe(head: bytes) -> str | None:
    if head[:2] != b"MZ":
        return None
    off = struct.unpack_from("<I", head, 0x3C)[0]
    if head[off:off + 4] != b"PE\0\0":
        return None
    return "windows-x64" if struct.unpack_from("<H", head, off + 4)[0] == 0x8664 else None


def _elf(head: bytes) -> str | None:
    if head[:6] != b"\x7fELF\x02\x01":
        return None
    return "linux-x64" if struct.unpack_from("<H", head, 0x12)[0] == 62 else None


def _macho(head: bytes) -> str | None:
    arm64 = 0x0100000C
    if head[:4] == b"\xcf\xfa\xed\xfe":
        return "macos-arm64" if struct.unpack_from("<I", head, 4)[0] == arm64 else None
    if head[:4] == b"\xca\xfe\xba\xbe":
        n = struct.unpack_from(">I", head, 4)[0]
        if any(struct.unpack_from(">I", head, 8 + 20 * i)[0] == arm64 for i in range(min(n, 16))):
            return "macos-arm64"
    return None


def library_platform(path: Path) -> str:
    """The platform of a library, from its header, which must match its file name."""
    head = path.read_bytes()[:4096]
    check = {".dll": _pe, ".so": _elf, ".dylib": _macho}.get(path.suffix)
    try:
        platform = check(head) if check else None
    except struct.error:
        platform = None
    if platform is None:
        raise Error(f"{path.name} is not a library for one of {', '.join(PLATFORMS)}")
    return platform


def pack(mod_id: str, libraries: Path, assets: Path, zip_path: Path) -> list[str]:
    """`<id>.zip`: the libraries at the top, then `assets/`, with fixed timestamps so that the same
    input gives the same bytes. Returns its platforms."""
    files = sorted(libraries.iterdir())
    platforms = sorted({library_platform(f) for f in files}, key=PLATFORMS.index)
    if assets.is_dir():
        files += sorted(p for p in assets.rglob("*") if p.is_file())
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for f in files:
            name = f.name if f.parent == libraries else f.relative_to(assets.parent).as_posix()
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.external_attr = 0o644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(info, f.read_bytes())
    return platforms


# ---------------------------------------------------------------------------------------------
# package: <id>.mod.json


def parse_lang(text: str) -> dict[str, str]:
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def summarize(description: str) -> str:
    """The description's first sentence on one line, without color markup, at most 140 characters:
    cut at a comma, or else a word, when it is longer."""
    text = re.sub(r"\{[^{}]*\}", "", description)
    text = re.split(r"\\n|\n", text)[0].strip()
    text = " ".join(re.split(r"(?<=[.!?])\s+", text)[0].split())
    if len(text) <= SUMMARY_MAX:
        return text
    cut = text[:SUMMARY_MAX - 1]
    comma, space = cut.rfind(", "), cut.rfind(" ")
    end = comma if comma >= SUMMARY_MAX // 2 else space if space > 0 else SUMMARY_MAX - 1
    return cut[:end].rstrip(" ,;:") + "…"


_COMMENT_RE = re.compile(r"//[^\n]*|/\*.*?\*/", re.S)
_DECLARE_RE = re.compile(
    r"(?P<attrs>(?:#\[[^\]]*\]\s*)*)(?:[A-Za-z_]\w*::)*declare_mod!\s*\(\s*deps\s*:\s*"
    r"\[(?P<deps>[^\]]*)\]")
_DEPS_RE = re.compile(r'\s*(?:"[a-z0-9_]+"\s*(?:,\s*"[a-z0-9_]+"\s*)*,?\s*)?')


def parse_deps(lib_rs: str, mod_id: str) -> list[str]:
    """The deps of the `declare_mod!` that a build with default features uses: the one without a
    `#[cfg]`, or with `#[cfg(not(feature ...))]`. Anything else is an error that says to set
    `[package.metadata.weft] deps` instead."""
    hint = f"set [package.metadata.weft] deps = [...] in {mod_id}'s Cargo.toml instead"
    found = []
    for m in _DECLARE_RE.finditer(_COMMENT_RE.sub("", lib_rs)):
        cfgs = re.findall(r"#\[\s*(cfg(?:_attr)?)\s*\(([^\]]*)\)\s*\]", m.group("attrs"))
        if any(kind == "cfg_attr" for kind, _ in cfgs):
            raise Error(f"{mod_id}: a declare_mod! under #[cfg_attr] can't be read; {hint}")
        if any(not c.replace(" ", "").startswith("not(feature") for _, c in cfgs):
            continue  # a build with a feature, or for another system
        if not _DEPS_RE.fullmatch(m.group("deps")):
            raise Error(f"{mod_id}: its declare_mod! deps aren't plain string literals; {hint}")
        found.append(re.findall(r'"([a-z0-9_]+)"', m.group("deps")))
    if len(found) != 1:
        raise Error(f"{mod_id}: {len(found)} declare_mod! for a default build, not one; {hint}")
    return found[0]


def fnv1a64(data: bytes) -> str:
    h = 0xCBF29CE484222325
    for byte in data:
        h = ((h ^ byte) * 0x100000001B3) & 0xFFFFFFFFFFFFFFFF
    return f"{h:016x}"


def weft_of(folder: Path) -> tuple[str, bytes]:
    """The Weft commit a mod builds against, and its ABI file: from `weft_abi` in `cargo metadata`.
    A git dependency on Weft gives the commit in its source; a path dependency (a submodule, say)
    is a checkout whose HEAD is the commit."""
    meta = cargo_metadata(folder, deps=True)
    abi = next((p for p in meta["packages"] if p["name"] == "weft_abi"), None)
    if abi is None:
        raise Error(f"{folder}: the mod doesn't depend on Weft (no weft_abi crate)")
    crate = Path(abi["manifest_path"]).parent
    source = abi.get("source") or ""
    if source.startswith("git+"):
        url, _, commit = source.removeprefix("git+").partition("#")
        if not re.match(rf"https://github\.com/{WEFT_REPO}(\.git)?(\?|$)", url, re.I):
            raise Error(f"{folder}: Weft comes from {url}, not https://github.com/{WEFT_REPO}")
    elif not source:
        checkout = crate.parent.parent
        r = subprocess.run(["git", "-C", str(checkout), "rev-parse", "HEAD"], capture_output=True,
                           text=True)
        if r.returncode != 0:
            raise Error(f"{folder}: Weft at {checkout} is not a git checkout")
        dirty = subprocess.run(["git", "-C", str(checkout), "status", "--porcelain"],
                               capture_output=True, text=True).stdout.strip()
        if dirty:
            raise Error(f"{folder}: the Weft checkout at {checkout} has local changes")
        commit = r.stdout.strip()
    else:
        raise Error(f"{folder}: Weft comes from {source}, not GitHub")
    return commit, (crate / "src" / "lib.rs").read_bytes()


def weft_name(gh: GitHub, commit: str) -> str:
    """The Weft release tag at `commit`, else its `git describe --tags` name `<tag>-<n>-g<sha7>`."""
    tags = {t["name"]: t["commit"]["sha"] for t in gh.pages(f"/repos/{WEFT_REPO}/tags")
            if WEFT_TAG_RE.match(t["name"])}
    exact = [t for t, c in tags.items() if c == commit]
    if exact:
        return max(exact, key=lambda t: semver_key(t[1:]) if SEMVER_RE.match(t[1:]) else ((), ()))
    best = None
    for tag in tags:
        cmp = gh.api(f"/repos/{WEFT_REPO}/compare/{tag}...{commit}")
        if cmp and cmp["status"] in ("ahead", "identical") \
                and (best is None or cmp["ahead_by"] < best[1]):
            best = (tag, cmp["ahead_by"])
    if best is None:
        raise Error(f"Weft commit {commit} descends from no Weft release tag")
    return f"{best[0]}-{best[1]}-g{commit[:7]}"


def mod_json(gh: GitHub, root: Path, entry: dict, zip_path: Path, platforms: list[str],
             repo: str, sha: str, default_branch: str) -> dict:
    folder = root / entry["folder"]
    mod = Mod(folder)
    pkg, mod_id = mod.package, entry["id"]
    prefix = "" if entry["folder"] == "." else entry["folder"] + "/"
    weft = (pkg.get("metadata") or {}).get("weft") or {}
    lang_file = folder / "assets" / "lang" / "en_us.lang"
    lang = parse_lang(lang_file.read_text(encoding="utf-8")) if lang_file.is_file() else {}
    # Each from [package.metadata.weft], else the lang file, else Cargo.toml's own field.
    name = weft.get("name") or lang.get(f"mod.{mod_id}.name") or mod_id
    description = (weft.get("summary") or lang.get(f"mod.{mod_id}.description")
                   or pkg.get("description") or "")
    lang_authors = [a.strip() for a in lang.get(f"mod.{mod_id}.authors", "").split(",")]
    cargo_authors = [re.sub(r"\s*<[^>]*>", "", a) for a in pkg.get("authors") or []]
    authors = (weft.get("authors") or [a for a in lang_authors if a] or cargo_authors
               or [repo.split("/")[0]])
    deps = weft.get("deps")
    if deps is None:
        deps = parse_deps(mod.lib_rs.read_text(encoding="utf-8"), mod_id)
    icon = folder / "assets" / "textures" / "gui" / "icon.png"
    commit, abi_source = weft_of(folder)
    data = zip_path.read_bytes()
    return {
        "schema": 1,
        "id": mod_id,
        "name": name,
        "summary": summarize(description) or name,
        "authors": authors,
        "license": pkg.get("license"),
        "homepage": pkg.get("homepage")
        or f"https://github.com/{repo}/tree/{default_branch}/{prefix}".rstrip("/"),
        "icon": f"https://raw.githubusercontent.com/{repo}/{sha}/{prefix}assets/textures/gui/icon.png"
        if icon.is_file() and icon.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n" else None,
        "version": entry["version"],
        "channel": "release" if "-" not in entry["version"] else "prerelease",
        "tag": entry["tag"],
        "published": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": {"repo": repo, "commit": sha},
        "weft": {"tag": weft_name(gh, commit), "commit": commit, "abi": fnv1a64(abi_source)},
        "deps": [{"id": d} for d in deps],
        "platforms": platforms,
        "asset": {"name": f"{mod_id}.zip",
                  "url": f"https://github.com/{repo}/releases/download/{entry['tag']}/{mod_id}.zip",
                  "size": len(data), "sha256": hashlib.sha256(data).hexdigest()},
    }


def merge_builds(artifacts: Path, libraries: Path) -> list[dict]:
    """Merges the builds of each runner (`<artifact>/plan.json` and `<artifact>/libraries/<id>/`)
    into `libraries/<id>/`. Every runner must have planned the same: returns that plan."""
    plans = {d.name: json.loads((d / "plan.json").read_text(encoding="utf-8"))
             for d in sorted(artifacts.iterdir()) if (d / "plan.json").is_file()}
    if not plans:
        raise Error(f"no build in {artifacts}: run the build action first")
    first = next(iter(plans.values()))
    if any(p != first for p in plans.values()):
        raise Error("the build runners planned different releases: "
                    + "; ".join(f"{k}: {[e['tag'] for e in v]}" for k, v in plans.items()))
    for d in plans:
        for lib in (artifacts / d / "libraries").glob("*/*"):
            (libraries / lib.parent.name).mkdir(parents=True, exist_ok=True)
            shutil.copy2(lib, libraries / lib.parent.name / lib.name)
    return first


def package(gh: GitHub, root: Path, artifacts: Path, dist: Path, repo: str, sha: str,
            require: list[str]) -> list[dict]:
    """`dist/<tag>/<id>.zip` and `<id>.mod.json` for each planned mod, from the downloaded builds,
    and `dist/plan.json`. Returns the plan."""
    libraries = dist / "libraries"
    entries = merge_builds(artifacts, libraries)
    default_branch = (gh.api(f"/repos/{repo}") or {}).get("default_branch", "main")
    for e in entries:
        zip_path = dist / e["tag"] / f"{e['id']}.zip"
        if not (libraries / e["id"]).is_dir():
            raise Error(f"{e['id']}: no libraries were built")
        platforms = pack(e["id"], libraries / e["id"], root / e["folder"] / "assets", zip_path)
        missing = [p for p in require if p not in platforms]
        if missing:
            raise Error(f"{e['id']}: no build for {', '.join(missing)}")
        info = mod_json(gh, root, e, zip_path, platforms, repo, sha, default_branch)
        (zip_path.parent / f"{e['id']}.mod.json").write_text(
            json.dumps(info, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        deps = ", ".join(d["id"] for d in info["deps"]) or "none"
        log(f"{e['tag']}: {', '.join(platforms)}; Weft {info['weft']['tag']}; deps {deps}")
    shutil.rmtree(libraries)
    (dist / "plan.json").write_text(json.dumps(entries, indent=2) + "\n", encoding="utf-8")
    return entries


# ---------------------------------------------------------------------------------------------
# release, latest-tags


def release(entries: list[dict], dist: Path, sha: str) -> None:
    """A GitHub release of each package, at the commit that was built (with `gh`)."""
    for e in entries:
        files = [str(dist / e["tag"] / f"{e['id']}.{ext}") for ext in ("zip", "mod.json")]
        cmd = ["gh", "release", "create", e["tag"], *files, "--target", sha,
               "--title", f"{e['id']} {e['version']}", "--generate-notes"]
        if e["since"]:
            cmd += ["--notes-start-tag", e["since"]]
        if e["kind"] != "release":
            cmd += ["--prerelease", "--latest=false"]
        if subprocess.run(cmd).returncode != 0:
            raise Error(f"{e['tag']}: gh release create failed")


def latest_tags(gh: GitHub, repo: str, folders: list[Path]) -> list[str]:
    """The newest GitHub release of each mod, by publication."""
    releases = [r for r in gh.pages(f"/repos/{repo}/releases") if not r["draft"]]
    out = []
    for folder in folders:
        mod_id = Mod(folder).id
        mine = [r for r in releases if r["tag_name"].startswith(f"{mod_id}-v")]
        if not mine:
            raise Error(f"{mod_id}: {repo} has no release of it yet")
        out.append(max(mine, key=lambda r: r["published_at"])["tag_name"])
    return out


# ---------------------------------------------------------------------------------------------
# CLI


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="weftmod.py")
    p.add_argument("--root", type=Path, default=Path.cwd(), help="the mod repository")
    sub = p.add_subparsers(dest="command", required=True)
    c = sub.add_parser("plan")
    c.add_argument("--mods", default=".")
    c.add_argument("--release-unchanged", action="store_true")
    c.add_argument("--out", type=Path, required=True)
    c = sub.add_parser("build")
    c.add_argument("--plan", type=Path, required=True)
    c.add_argument("--out", type=Path, required=True)
    c.add_argument("--cargo-args", default="")
    c = sub.add_parser("package")
    c.add_argument("--artifacts", type=Path, required=True)
    c.add_argument("--dist", type=Path, required=True)
    c.add_argument("--require", default=" ".join(PLATFORMS))
    c = sub.add_parser("release")
    c.add_argument("--dist", type=Path, required=True)
    c = sub.add_parser("latest-tags")
    c.add_argument("--mods", default=".")
    args = p.parse_args(argv)

    repo, sha = os.environ.get("GITHUB_REPOSITORY", ""), os.environ.get("GITHUB_SHA", "")
    try:
        gh = GitHub()
        if args.command == "plan":
            entries = plan(gh, args.root, mod_folders(args.mods, args.root), repo, sha,
                           os.environ.get("GITHUB_RUN_NUMBER", "0"), args.release_unchanged)
            args.out.write_text(json.dumps(entries, indent=2) + "\n", encoding="utf-8")
        elif args.command == "build":
            build(args.root, json.loads(args.plan.read_text()), args.out, args.cargo_args.split())
        elif args.command == "package":
            entries = package(gh, args.root, args.artifacts, args.dist, repo, sha,
                              args.require.split())
            print(" ".join(e["tag"] for e in entries))
        elif args.command == "release":
            release(json.loads((args.dist / "plan.json").read_text()), args.dist, sha)
        elif args.command == "latest-tags":
            print(" ".join(latest_tags(gh, repo, mod_folders(args.mods, args.root))))
    except Error as e:
        print(f"::error::{e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

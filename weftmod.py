#!/usr/bin/env python3
"""The engine of weft-mod-action: plans, builds and packages Weft mods. Standard library only, and
Python 3.10 or newer, so that it runs on every GitHub runner as it is.

    plan        which mods to release, at which versions        (build/action.yml, on each runner)
    build       set those versions, `cargo build --release`, copy the libraries
    package     merge the runners' builds into each mod's `<id>.zip`  (release/action.yml)
    release     a GitHub release of each zip
    submit      propose releases to the mod index, as `weft --submit-mod` does  (index-pr/action.yml)

A mod is a folder with a Cargo.toml that builds a `cdylib`; its id is that library's name. See
README.md for the rules.
"""

from __future__ import annotations

import argparse
import base64
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

PLATFORMS = ("windows-x64", "linux-x64", "macos-arm64")
LIBRARIES = {"windows-x64": "{id}.dll", "linux-x64": "lib{id}.so", "macos-arm64": "lib{id}.dylib"}
CORE_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)")
_PRE_ID = r"(?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*)"
SEMVER_RE = re.compile(CORE_RE.pattern + rf"(?:-{_PRE_ID}(?:\.{_PRE_ID})*)?$")
ID_RE = re.compile(r"^[a-z0-9_]+$")
INDEX = "Weft-Rust/Weft-Mods-Index"  # the mod index, where `submit` proposes


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

    def api(self, path: str, method: str = "GET", data: dict | None = None):
        req = urllib.request.Request("https://api.github.com" + path, method=method, headers={
            "Accept": "application/vnd.github+json", "User-Agent": "weft-mod-action"},
            data=None if data is None else json.dumps(data).encode())
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if self.token:
            req.add_unredirected_header("Authorization", f"Bearer {self.token}")
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=60) as r:
                    body = r.read()
                    return json.loads(body) if body else {}
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    return None
                if e.code < 500 or attempt == 2:
                    raise Error(f"{method} {path}: HTTP {e.code} {e.reason}{_why(e)}") from None
            except (OSError, ValueError) as e:
                if attempt == 2:
                    raise Error(f"{method} {path}: {e}") from None
            time.sleep(2 ** attempt)

    def pages(self, path: str) -> list:
        out, page = [], 1
        while True:
            batch = self.api(f"{path}{'&' if '?' in path else '?'}per_page=100&page={page}") or []
            out += batch
            if len(batch) < 100:
                return out
            page += 1


def _why(e: urllib.error.HTTPError) -> str:
    """What GitHub says is wrong, from the body of its error."""
    try:
        body = json.loads(e.read() or b"{}")
    except (OSError, ValueError):
        return ""
    finally:
        e.close()
    why = [body.get("message")] + [x.get("message") or x.get("code") for x in body.get("errors", [])
                                   if isinstance(x, dict)]
    why = [w for w in why if w]
    return f": {'; '.join(why)}" if why else ""


def tag_exists(gh: GitHub, repo: str, tag: str) -> bool:
    return gh.api(f"/repos/{repo}/git/ref/tags/{tag}") is not None


# ---------------------------------------------------------------------------------------------
# Mods


def cargo_metadata(folder: Path) -> dict:
    cmd = ["cargo", "metadata", "--format-version", "1", "--no-deps", "--manifest-path",
           str(folder / "Cargo.toml")]
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


def pack(libraries: Path, assets: Path, zip_path: Path) -> list[str]:
    """`<id>.zip`: the libraries at the top, then `assets/`, with fixed timestamps so that the
    same input gives the same bytes. Returns its platforms."""
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
# deps: what the index lists a mod as needing


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


def weft_metadata(mod: Mod) -> dict:
    return (mod.package.get("metadata") or {}).get("weft") or {}


def mod_deps(mod: Mod) -> list[str]:
    """`[package.metadata.weft] deps`, else the deps of the library's `declare_mod!`."""
    deps = weft_metadata(mod).get("deps")
    if deps is None:
        deps = parse_deps(mod.lib_rs.read_text(encoding="utf-8"), mod.id)
    if not all(isinstance(d, str) and ID_RE.match(d) for d in deps):
        raise Error(f"{mod.id}: [package.metadata.weft] deps must be mod ids, not {deps!r}")
    return deps


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


def package(root: Path, artifacts: Path, dist: Path, require: list[str]) -> list[dict]:
    """`dist/<tag>/<id>.zip` for each planned mod, from the downloaded builds, and
    `dist/plan.json`. Returns the plan."""
    libraries = dist / "libraries"
    entries = merge_builds(artifacts, libraries)
    for e in entries:
        zip_path = dist / e["tag"] / f"{e['id']}.zip"
        if not (libraries / e["id"]).is_dir():
            raise Error(f"{e['id']}: no libraries were built")
        platforms = pack(libraries / e["id"], root / e["folder"] / "assets", zip_path)
        missing = [p for p in require if p not in platforms]
        if missing:
            raise Error(f"{e['id']}: no build for {', '.join(missing)}")
        log(f"{e['tag']}: {', '.join(platforms)}")
    shutil.rmtree(libraries)
    (dist / "plan.json").write_text(json.dumps(entries, indent=2) + "\n", encoding="utf-8")
    return entries


# ---------------------------------------------------------------------------------------------
# release, latest_tags


def release(entries: list[dict], dist: Path, sha: str) -> list[str]:
    """A GitHub release of each zip, at the commit that was built (with `gh`). Returns the tags of
    the full releases (no release candidates): what can go to the index."""
    for e in entries:
        cmd = ["gh", "release", "create", e["tag"], str(dist / e["tag"] / f"{e['id']}.zip"),
               "--target", sha, "--title", f"{e['id']} {e['version']}", "--generate-notes"]
        if e["since"]:
            cmd += ["--notes-start-tag", e["since"]]
        if e["kind"] != "release":
            cmd += ["--prerelease", "--latest=false"]
        # gh prints each release's URL: to the log, not to the tags this prints.
        if subprocess.run(cmd, stdout=sys.stderr).returncode != 0:
            raise Error(f"{e['tag']}: gh release create failed")
    return [e["tag"] for e in entries if e["kind"] == "release"]


def latest_tags(gh: GitHub, repo: str, folders: list[Path]) -> list[str]:
    """The newest full release of each mod (not a prerelease, so not a release candidate), by
    publication."""
    releases = [r for r in gh.pages(f"/repos/{repo}/releases")
                if not r["draft"] and not r["prerelease"]]
    out = []
    for folder in folders:
        mod_id = Mod(folder).id
        mine = [r for r in releases if r["tag_name"].startswith(f"{mod_id}-v")]
        if not mine:
            raise Error(f"{mod_id}: {repo} has no full release of it yet")
        out.append(max(mine, key=lambda r: r["published_at"])["tag_name"])
    return out


# ---------------------------------------------------------------------------------------------
# submit: what `weft --submit-mod` does, for each release


def submit_mods(spec: str, clone: Path) -> list[Mod]:
    """The mods in the `mods` folders that hold a Cargo.toml at the commit checked out (a mod
    added later is not there at an older release), from the repository alone, as the index
    builds them: without submodules."""
    out = []
    for folder in [clone / f for f in spec.split()] or [clone]:
        if (folder / "Cargo.toml").is_file():
            try:
                out.append(Mod(folder))
            except Error as e:
                raise Error(f"{e}\nThe index builds a mod from its repository alone, without "
                            f"submodules, so it takes {SDK_HINT}") from None
    return out


def git(cwd: Path, *args: str) -> str:
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if r.returncode != 0:
        raise Error(f"git {' '.join(args)}: {r.stderr.strip()}")
    return r.stdout.strip()


SDK_GIT = "https://github.com/Weft-Rust/Weft"
SDK_HINT = f'weft_sdk = {{ git = "{SDK_GIT}", tag = "<a Weft release, such as v26.11.3>" }}'
GAME_MODS = {"base", "core"}  # ship with the game; the index's `deps` leave them out
PR_BODY = "Submitted with weft-mod-action"


# The line the index's scripts/patch_sdk.py sets to the Weft it builds against, which it needs
# once in the mod's own Cargo.toml (perhaps under [workspace.dependencies]).
_SDK_RE = re.compile(r'^\s*weft_sdk\s*=\s*\{[^}]*git\s*=\s*"https://github\.com/Weft-Rust/Weft"'
                     r'[^}]*tag\s*=\s*"([^"]*)"', re.M)


def sdk_tag(mod: Mod) -> str | None:
    """The tag of `weft_sdk = { git = "https://github.com/Weft-Rust/Weft", tag = "..." }` in the
    mod's Cargo.toml: the Weft the index builds the mod against."""
    tags = _SDK_RE.findall((mod.folder / "Cargo.toml").read_text(encoding="utf-8"))
    return tags[0] if len(tags) == 1 and tags[0] else None


def toml_str(s: str) -> str:
    """A TOML basic string: JSON's escapes are a subset of TOML's."""
    return json.dumps(s, ensure_ascii=False)


def index_file(mod_id: str, summary: str, owner: str, repo: str, folder: str, commit: str,
               weft: str, deps: list[str]) -> str:
    """`mods/<id>/<version>.toml`, as `weft --submit-mod` writes it (Weft's
    crates/app/src/submit.rs), but with the mod's `deps`, which weft leaves empty."""
    s = toml_str
    return (
        "# Shown in the launcher. The newest version's say what the mod is.\n"
        f"name = {s(mod_id)}\n"
        f"summary = {s(summary)}\n"
        f"author = {s(owner)}\n"
        "\n"
        "# What is built: the crate in `dir` of `repo` at `commit`, against the SDK of `weft`.\n"
        f"repo = {s(repo)}\n"
        f"dir = {s(folder)}\n"
        f"commit = {s(commit)}\n"
        f"weft = {s(weft)}\n"
        "# Mod ids it needs besides base, installed with it.\n"
        f"deps = {json.dumps(deps)}\n"
        "# The GitHub login the id belongs to: only they can add versions.\n"
        f"owner = {s(owner)}\n"
        "# Set by a maintainer who read the source of this commit.\n"
        'reviewed = ""\n')


def index_fork(gh: GitHub, login: str, tries: int = 60) -> tuple[str, str]:
    """The token's fork of the index (made if it has none) once GitHub has it ready, and the
    index's default branch."""
    index = gh.api(f"/repos/{INDEX}")
    if index is None:
        raise Error(f"{INDEX}: no such repository")
    base = index["default_branch"]
    fork = gh.api(f"/repos/{login}/{INDEX.split('/')[1]}")
    if not (fork and fork.get("fork") and (fork.get("parent") or {}).get("full_name") == INDEX):
        # Gives the fork the account has, under whatever name, or makes one.
        fork = gh.api(f"/repos/{INDEX}/forks", method="POST", data={"default_branch_only": True})
        log(f"{INDEX} forked to {fork['full_name']}")
    name = fork["full_name"]
    for _ in range(tries):  # GitHub forks in the background
        try:
            if gh.api(f"/repos/{name}/git/ref/heads/{base}") is not None:
                break
        except Error:  # 409 while the fork is still empty
            pass
        time.sleep(5)
    else:
        raise Error(f"{name}: the fork of {INDEX} wasn't ready after {tries * 5} seconds")
    # So that the fork has the index's newest commit, which the branch starts at (a fork shares
    # the index's objects, so the branch works without it too).
    try:
        gh.api(f"/repos/{name}/merge-upstream", method="POST", data={"branch": base})
    except Error as e:
        log(f"::warning::{name} can't be brought up to date with {INDEX}: {e}")
    return name, base


def propose(gh: GitHub, fork: str, base: str, path: str, text: str, mod_id: str,
            version: str) -> str:
    """What `weft --submit-mod` does through gh: the branch `<id>-<version>` of the fork, from the
    index's newest commit, with the one file; then the pull request. Returns its URL."""
    branch, title = f"{mod_id}-{version}", f"{mod_id} {version}"
    sha = gh.api(f"/repos/{INDEX}/git/ref/heads/{base}")["object"]["sha"]
    if gh.api(f"/repos/{fork}/git/ref/heads/{branch}") is not None:
        # Left by a run that failed after pushing, or by a closed pull request.
        gh.api(f"/repos/{fork}/git/refs/heads/{branch}", method="PATCH",
               data={"sha": sha, "force": True})
    else:
        gh.api(f"/repos/{fork}/git/refs", method="POST",
               data={"ref": f"refs/heads/{branch}", "sha": sha})
    gh.api(f"/repos/{fork}/contents/{path}", method="PUT", data={
        "message": title, "branch": branch,
        "content": base64.b64encode(text.encode("utf-8")).decode()})
    pr = gh.api(f"/repos/{INDEX}/pulls", method="POST", data={
        "title": title, "head": f"{fork.split('/')[0]}:{branch}", "base": base, "body": PR_BODY,
        "maintainer_can_modify": True})
    return pr["html_url"]


def submit(gh: GitHub, clone: Path, repo: str, spec: str, tags: list[str],
           dry_run: bool = False) -> list[str]:
    """Proposes each release to the index as `weft --submit-mod` does, from `clone` (a clone of
    the mod's repository) at the release's commit: checks the mod, writes
    `mods/<id>/<version>.toml` and opens a pull request that adds it, from the token's fork of the
    index. Each tag must have a GitHub release, which the release action makes once the mod built
    on every system: it isn't built again here. The index builds that commit at the version in its
    Cargo.toml, so the tag must have that version: a full release, not a release candidate. Leaves
    out releases the index has, and those with an open pull request. A dry run opens nothing: it
    prints each file. Returns the pull requests."""
    try:
        user = gh.api("/user")
    except Error:  # e.g. a GITHUB_TOKEN, which is no one's
        user = None
    login = (user or {}).get("login")
    if not login and not dry_run:
        raise Error("the token can't tell whose it is (GET /user); is it a personal access token?")
    if not tags:
        tags = latest_tags(gh, repo, [m.folder for m in submit_mods(spec, clone)])
    fork = None
    urls = []
    for tag in tags:
        mod_id, _, version = tag.partition("-v")
        branch = f"{mod_id}-{version}"
        path = f"mods/{mod_id}/{version}.toml"
        if gh.api(f"/repos/{INDEX}/contents/{path}") is not None:
            log(f"{tag}: in the index already")
            continue
        if login:
            open_prs = gh.api(f"/repos/{INDEX}/pulls?state=open&head={login}:{branch}")
            if open_prs:
                log(f"{tag}: proposed already, {open_prs[0]['html_url']}")
                urls.append(open_prs[0]["html_url"])
                continue
        rel = gh.api(f"/repos/{repo}/releases/tags/{tag}")
        if rel is None or rel.get("draft"):
            raise Error(f"{tag}: {repo} has no release of it. Propose a tag once the release "
                        f"action (or another) has built it and made its GitHub release")
        commit = (gh.api(f"/repos/{repo}/commits/{tag}") or {}).get("sha")
        if not commit:
            raise Error(f"{tag}: {repo} has no such tag")
        # Exactly the commit the index builds, whatever the checkout held before.
        git(clone, "checkout", "-q", "--force", "--detach", commit)
        git(clone, "clean", "-q", "-fd")
        mod = next((m for m in submit_mods(spec, clone) if m.id == mod_id), None)
        if mod is None:
            raise Error(f"{tag}: no folder in `mods` holds the mod {mod_id} at {commit[:7]}")
        if mod.package["name"] != mod.id:
            raise Error(f"{tag}: the index takes the mod id from [package] name, "
                        f"{mod.package['name']}, which must be the library's name, {mod.id}")
        if mod.version != version:
            raise Error(f"{tag}: its Cargo.toml says version {mod.version}. The index builds the "
                        f"commit at that version, so only a full release, made from the version "
                        f"in Cargo.toml, can be proposed")
        weft = sdk_tag(mod)
        if weft is None:
            raise Error(f"{tag}: weft_sdk must come from the game's repository at a release tag, "
                        f"for the index to build it, with one line in the mod's own Cargo.toml: "
                        f"{SDK_HINT}")
        deps = [d for d in mod_deps(mod) if d not in GAME_MODS]
        folder = mod.folder.relative_to(clone).as_posix()
        text = index_file(mod.id, mod.package.get("description") or "", login or repo.split("/")[0],
                          f"https://github.com/{repo}", "" if folder == "." else folder, commit,
                          weft, deps)
        if dry_run:
            log(f"{tag}: would propose {path}:\n{text}")
            continue
        if fork is None:
            fork = index_fork(gh, login)
        url = propose(gh, fork[0], fork[1], path, text, mod.id, version)
        log(f"{tag}: {url}")
        urls.append(url)
    return urls

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
    c = sub.add_parser("submit", help="--root is a clone of the mod repository, which it checks out")
    c.add_argument("--mods", default=".")
    c.add_argument("--tags", default="", help="default: the newest full release of each mod")
    c.add_argument("--dry-run", action="store_true")
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
            entries = package(args.root, args.artifacts, args.dist, args.require.split())
            print(" ".join(e["tag"] for e in entries))
        elif args.command == "release":
            print(" ".join(release(json.loads((args.dist / "plan.json").read_text()), args.dist,
                                   sha)))
        elif args.command == "submit":
            print(" ".join(submit(gh, args.root, repo, args.mods, args.tags.split(),
                                  args.dry_run)))
    except Error as e:
        print(f"::error::{e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

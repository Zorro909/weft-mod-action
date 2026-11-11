"""Unit tests of weftmod.py: `python3 -m unittest discover tests`. The plan tests need cargo."""

from __future__ import annotations

import base64
import contextlib
import io
import json
import shutil
import os
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import weftmod  # noqa: E402

FIXTURE = ROOT / "tests" / "fixture"
SHA = "0123456789abcdef0123456789abcdef01234567"


def elf() -> bytes:
    head = bytearray(64)
    head[:6] = b"\x7fELF\x02\x01"
    struct.pack_into("<H", head, 0x12, 62)
    return bytes(head)


def pe() -> bytes:
    head = bytearray(256)
    head[:2] = b"MZ"
    struct.pack_into("<I", head, 0x3C, 0x80)
    head[0x80:0x84] = b"PE\0\0"
    struct.pack_into("<H", head, 0x84, 0x8664)
    return bytes(head)


def macho() -> bytes:
    return b"\xcf\xfa\xed\xfe" + struct.pack("<I", 0x0100000C) + bytes(56)


class FakeGitHub:
    """Answers `api` from a dict: a GET by its path, anything else by "METHOD path". What isn't in
    it is a 404 (None) for a GET, and `{}` (done) for anything else. An answer that is a list of
    answers gives one per call, the last one ever after; an Error in it is raised."""

    def __init__(self, responses: dict | None = None):
        self.responses = responses or {}
        self.calls: list[str] = []
        self.sent: list[tuple[str, dict]] = []

    def api(self, path, method="GET", data=None):
        key = path if method == "GET" else f"{method} {path}"
        self.calls.append(key)
        if data is not None:
            self.sent.append((key, data))
        answer = self.responses.get(key, None if method == "GET" else {})
        if isinstance(answer, Answers):
            answer = answer.pop(0) if len(answer) > 1 else answer[0]
        if isinstance(answer, Exception):
            raise answer
        return answer

    def pages(self, path):
        return self.responses.get(path, [])


class Answers(list):
    """Answers to the same call in turn, for FakeGitHub."""


class TempDir(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()


class Versions(unittest.TestCase):
    def test_rc_is_the_next_patch_with_run_and_commit(self):
        self.assertEqual(weftmod.rc_version("0.2.1", "57", SHA), "0.2.2-rc.57.g0123456")
        self.assertEqual(weftmod.rc_version("1.0.0-rc.3", "1", SHA), "1.0.1-rc.1.g0123456")
        self.assertTrue(weftmod.SEMVER_RE.match(weftmod.rc_version("0.0.0", "1", "0" * 40)))

    def test_semver_order(self):
        ordered = ["0.1.0-rc.1", "0.1.0-rc.2", "0.1.0-rc.10", "0.1.0-rc.a", "0.1.0", "0.1.1",
                   "0.10.0"]
        self.assertEqual(sorted(reversed(ordered), key=weftmod.semver_key), ordered)


class SetVersion(TempDir):
    def test_sets_only_the_package_version(self):
        toml = self.tmp / "Cargo.toml"
        toml.write_text('[package]\nname = "m"\nversion = "0.1.0"\n\n'
                        '[dependencies]\nx = { version = "1" }\n[workspace.package]\nversion = "9.9.9"\n')
        self.assertTrue(weftmod.set_version(toml, "0.1.1-rc.4.gabcdef0"))
        text = toml.read_text()
        self.assertIn('version = "0.1.1-rc.4.gabcdef0"', text)
        self.assertIn('x = { version = "1" }', text)
        self.assertIn('[workspace.package]\nversion = "9.9.9"', text)

    def test_a_workspace_version_is_left_alone(self):
        toml = self.tmp / "Cargo.toml"
        toml.write_text('[package]\nname = "m"\nversion.workspace = true\n')
        self.assertFalse(weftmod.set_version(toml, "1.0.0"))


class Metadata(unittest.TestCase):
    def test_deps(self):
        self.assertEqual(weftmod.parse_deps('declare_mod!(deps: ["base", "cog_work"], init: i);',
                                            "m"), ["base", "cog_work"])
        self.assertEqual(weftmod.parse_deps('weft_sdk::declare_mod!(deps: [], init: i);', "m"), [])

    def test_deps_of_the_default_build(self):
        src = ('#[cfg(feature = "dev")]\ndeclare_mod!(deps: ["base", "dev"], init: i);\n'
               '#[cfg(not(feature = "dev"))]\ndeclare_mod!(deps: ["base"], init: i);\n'
               '// declare_mod!(deps: ["commented"], init: i);\n')
        self.assertEqual(weftmod.parse_deps(src, "m"), ["base"])

    def test_unreadable_deps_say_what_to_do(self):
        for src in ['declare_mod!(deps: [BASE], init: i);',
                    '#[cfg_attr(x, y)]\ndeclare_mod!(deps: ["base"], init: i);',
                    'declare_mod!(deps: ["a"], init: i);\ndeclare_mod!(deps: ["b"], init: i);',
                    'fn main() {}']:
            with self.assertRaisesRegex(weftmod.Error, r"package\.metadata\.weft"):
                weftmod.parse_deps(src, "m")


class Summary(TempDir):
    def test_summarize(self):
        self.assertEqual(weftmod.summarize("{gold}Gears{/} and belts. More later.\\nNext line"),
                         "Gears and belts.")
        s = weftmod.summarize("word " * 60)
        self.assertLessEqual(len(s), weftmod.SUMMARY_MAX)
        self.assertTrue(s.endswith("…"))
        # cogwork's: cut at a comma.
        self.assertEqual(weftmod.summarize(
            "Item tubes you can see into, engines, and machines that take over the big jobs: an "
            "excavator that mines out a whole area, an assembly bench that crafts on its own and a "
            "leveler that makes land flat. Cuboid loaders keep them working with nobody near."),
            "Item tubes you can see into, engines, and machines that take over the big jobs: an "
            "excavator that mines out a whole area…")

    def test_lang(self):
        self.assertEqual(weftmod.parse_lang("# c\na.b = x = y\n\nbad\n"), {"a.b": "x = y"})

    def mod(self, metadata=None, description=None, lang=None):
        path = self.tmp / "assets" / "lang" / "en_us.lang"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.unlink(missing_ok=True)
        if lang is not None:
            path.write_text(lang)
        package = {"metadata": {"weft": metadata or {}}, "description": description}
        return mock.Mock(id="m", folder=self.tmp, package=package)

    def test_where_the_summary_comes_from(self):
        both = "mod.m.name = Em\nmod.m.description = From {red}the{/} lang file. More.\n"
        self.assertEqual(weftmod.mod_summary(self.mod({"summary": "Set."}, "Cargo.", both)), "Set.")
        self.assertEqual(weftmod.mod_summary(self.mod({}, "Cargo.", both)), "From the lang file.")
        self.assertEqual(weftmod.mod_summary(self.mod({}, "From Cargo. More.", "mod.m.name = Em\n")),
                         "From Cargo.")
        self.assertEqual(weftmod.mod_summary(self.mod({}, None, "mod.m.name = Em\n")), "Em")
        self.assertEqual(weftmod.mod_summary(self.mod()), "m")


class Folders(TempDir):
    def test_submodules_of_weft(self):
        (self.tmp / ".gitmodules").write_text(
            '[submodule "weft"]\n\tpath = vendor/weft\n\turl = https://github.com/Weft-Rust/Weft.git\n'
            '[submodule "other"]\n\tpath = other\n\turl = https://github.com/a/b\n')
        self.assertEqual(weftmod.weft_submodules(self.tmp), ["vendor/weft"])

    def test_mod_folders(self):
        (self.tmp / "a").mkdir()
        (self.tmp / "a" / "Cargo.toml").write_text("")
        self.assertEqual(weftmod.mod_folders("a", self.tmp), [self.tmp / "a"])
        with self.assertRaisesRegex(weftmod.Error, "b: no Cargo.toml"):
            weftmod.mod_folders("a\nb", self.tmp)


@unittest.skipUnless(shutil.which("cargo"), "needs cargo")
class Plan(unittest.TestCase):
    REPO = "someone/mods"

    def plan(self, responses, **kw):
        return weftmod.plan(FakeGitHub(responses), ROOT, [FIXTURE], self.REPO, SHA, "42", **kw)

    def tags(self, *versions):
        path = f"/repos/{self.REPO}/git/matching-refs/tags/hello_weft-v"
        refs = {path: [{"ref": f"refs/tags/hello_weft-v{v}"} for v in versions]}
        refs.update({f"/repos/{self.REPO}/git/ref/tags/hello_weft-v{v}": {} for v in versions})
        return refs

    def test_a_new_version_is_released(self):
        [e] = self.plan({})
        self.assertEqual(e, {"folder": "tests/fixture", "id": "hello_weft", "version": "0.1.0",
                             "tag": "hello_weft-v0.1.0", "kind": "release", "since": ""})

    def changed(self, since):
        return {f"/repos/{self.REPO}/compare/hello_weft-v{since}...{SHA}":
                {"files": [{"filename": "tests/fixture/src/lib.rs"}]}}

    def test_a_released_version_becomes_an_rc_after_the_newest_full_release(self):
        [e] = self.plan({**self.tags("0.1.0", "0.3.0"), **self.changed("0.3.0")})
        self.assertEqual((e["version"], e["kind"], e["since"]),
                         ("0.3.1-rc.42.g0123456", "rc", "hello_weft-v0.3.0"))

    def test_candidates_dont_raise_the_next_rc(self):
        [e] = self.plan({**self.tags("0.1.0", "0.1.1-rc.3"), **self.changed("0.1.1-rc.3")})
        self.assertEqual((e["version"], e["kind"], e["since"]),
                         ("0.1.1-rc.42.g0123456", "rc", "hello_weft-v0.1.1-rc.3"))

    def test_an_unchanged_mod_is_skipped(self):
        same = {f"/repos/{self.REPO}/compare/hello_weft-v0.1.0...{SHA}":
                {"files": [{"filename": "README.md"}]}}
        self.assertEqual(self.plan({**self.tags("0.1.0"), **same}), [])
        [e] = self.plan({**self.tags("0.1.0"), **same}, release_unchanged=True)
        self.assertEqual(e["version"], "0.1.1-rc.42.g0123456")

    def test_a_path_inside_a_moved_submodule_changed(self):
        moved = {f"/repos/{self.REPO}/compare/t...{SHA}": {"files": [{"filename": "vendor/x"}]}}
        gh = FakeGitHub(moved)
        self.assertTrue(weftmod.changed_since(gh, self.REPO, "t", SHA, ["vendor/x/crates/a"]))
        self.assertFalse(weftmod.changed_since(gh, self.REPO, "t", SHA, ["vendor/xy"]))


class SharedCode(TempDir):
    """A mod changes with the crates of the repository it depends on by path."""
    REPO = "someone/mods"

    def crate(self, folder, name, deps="", lib=False):
        (self.tmp / folder / "src").mkdir(parents=True)
        (self.tmp / folder / "src" / "lib.rs").write_text("")
        (self.tmp / folder / "Cargo.toml").write_text(
            f'[package]\nname = "{name}"\nversion = "0.1.0"\nedition = "2021"\n'
            + ('[lib]\ncrate-type = ["cdylib"]\n[workspace]\n' if lib else "") + deps)

    def setUp(self):
        super().setUp()
        self.crate("repo/m", "m", lib=True, deps=(
            '[dependencies]\napi = { path = "api" }\nlib = { path = "../lib" }\n'
            'outside = { path = "../../outside" }\n'
            '[dev-dependencies]\ntesting = { path = "../testing" }\n'))
        self.crate("repo/m/api", "api")
        self.crate("repo/lib", "lib", deps='[dependencies]\nshared = { path = "../shared" }\n')
        self.crate("repo/shared", "shared")
        self.crate("repo/testing", "testing")
        self.crate("outside", "outside")
        self.root = self.tmp / "repo"

    def plan(self, changed):
        responses = {
            f"/repos/{self.REPO}/git/matching-refs/tags/m-v": [{"ref": "refs/tags/m-v0.1.0"}],
            f"/repos/{self.REPO}/git/ref/tags/m-v0.1.0": {},
            f"/repos/{self.REPO}/compare/m-v0.1.0...{SHA}": {"files": [{"filename": changed}]}}
        return weftmod.plan(FakeGitHub(responses), self.root, [self.root / "m"], self.REPO, SHA,
                            "42")

    def test_what_a_mod_is_built_from(self):
        mod = weftmod.Mod(self.root / "m")
        self.assertEqual(weftmod.watched_paths(self.root, "m", mod, ["weft"]),
                         ["m", "weft", "lib", "shared"])

    def test_a_change_to_a_crate_it_depends_on_makes_a_candidate(self):
        [e] = self.plan("shared/src/lib.rs")
        self.assertEqual(e["version"], "0.1.1-rc.42.g0123456")

    def test_a_change_to_a_dev_dependency_doesnt(self):
        self.assertEqual(self.plan("testing/src/lib.rs"), [])

    def test_a_crate_at_the_root_is_everything(self):
        (self.root / "Cargo.toml").write_text(
            '[package]\nname = "top"\nversion = "0.1.0"\nedition = "2021"\n')
        (self.root / "src").mkdir()
        (self.root / "src" / "lib.rs").write_text("")
        toml = self.root / "m" / "Cargo.toml"
        toml.write_text(toml.read_text().replace(
            "[dependencies]\n", '[dependencies]\ntop = { path = ".." }\n'))
        mod = weftmod.Mod(self.root / "m")
        self.assertEqual(weftmod.watched_paths(self.root, "m", mod, []), [""])


class Pack(TempDir):
    def libraries(self):
        libs = self.tmp / "libs"
        libs.mkdir()
        (libs / "m.dll").write_bytes(pe())
        (libs / "libm.so").write_bytes(elf())
        (libs / "libm.dylib").write_bytes(macho())
        assets = self.tmp / "assets"
        (assets / "lang").mkdir(parents=True)
        (assets / "lang" / "en_us.lang").write_text("mod.m.name = M\n")
        return libs, assets

    def test_platforms_and_layout(self):
        libs, assets = self.libraries()
        out = self.tmp / "m.zip"
        self.assertEqual(weftmod.pack(libs, assets, out),
                         ["windows-x64", "linux-x64", "macos-arm64"])
        with zipfile.ZipFile(out) as z:
            self.assertEqual(z.namelist(), ["libm.dylib", "libm.so", "m.dll",
                                            "assets/lang/en_us.lang"])

    def test_same_input_same_bytes(self):
        libs, assets = self.libraries()
        weftmod.pack(libs, assets, self.tmp / "a.zip")
        weftmod.pack(libs, assets, self.tmp / "b.zip")
        self.assertEqual((self.tmp / "a.zip").read_bytes(), (self.tmp / "b.zip").read_bytes())

    def test_a_library_must_match_its_name(self):
        libs, assets = self.libraries()
        (libs / "libm.so").write_bytes(pe())
        with self.assertRaisesRegex(weftmod.Error, "libm.so is not a library"):
            weftmod.pack(libs, assets, self.tmp / "m.zip")


class MergeBuilds(TempDir):
    def build(self, name, plan, libs):
        d = self.tmp / "artifacts" / name
        (d / "libraries" / "m").mkdir(parents=True)
        (d / "plan.json").write_text(json.dumps(plan))
        for lib in libs:
            (d / "libraries" / "m" / lib).write_bytes(b"x")

    def test_merges_the_libraries_of_one_plan(self):
        plan = [{"id": "m", "tag": "m-v1.0.0"}]
        self.build("linux", plan, ["libm.so"])
        self.build("windows", plan, ["m.dll"])
        out = self.tmp / "out"
        self.assertEqual(weftmod.merge_builds(self.tmp / "artifacts", out), plan)
        self.assertEqual(sorted(p.name for p in (out / "m").iterdir()), ["libm.so", "m.dll"])

    def test_different_plans_fail(self):
        self.build("linux", [{"id": "m", "tag": "m-v1.0.0"}], [])
        self.build("windows", [{"id": "m", "tag": "m-v1.0.1-rc.1.gabcdef0"}], [])
        with self.assertRaisesRegex(weftmod.Error, "planned different releases"):
            weftmod.merge_builds(self.tmp / "artifacts", self.tmp / "out")

    def test_nothing_planned_packages_nothing(self):
        for name in ("linux", "windows"):
            (self.tmp / "artifacts" / name / "libraries").mkdir(parents=True)
            (self.tmp / "artifacts" / name / "plan.json").write_text("[]")
        dist = self.tmp / "dist"
        self.assertEqual(weftmod.package(self.tmp, self.tmp / "artifacts", dist, []), [])
        self.assertEqual(json.loads((dist / "plan.json").read_text()), [])


# The fixture's file, as `weft --submit-mod` writes it at Weft 85f85db (crates/app/src/submit.rs),
# for the owner someone, in the folder hello of someone/mods.
WEFT_FILE = """# Shown in the launcher. The newest version's say what the mod is.
name = "hello_weft"
summary = "A mod that does nothing, for testing weft-mod-action."
author = "someone"

# What is built: the crate in `dir` of `repo` at `commit`, against the SDK of `weft`.
repo = "https://github.com/someone/mods"
dir = "hello"
commit = "{commit}"
weft = "v26.11.2"
# Mod ids it needs besides base, installed with it.
deps = []
# The GitHub login the id belongs to: only they can add versions.
owner = "someone"
# Set by a maintainer who read the source of this commit.
reviewed = ""
"""

# The fields of an index file, in order (the index's scripts/common.py).
INDEX_FIELDS = ["name", "summary", "author", "repo", "dir", "commit", "weft", "deps", "owner",
                "reviewed"]


class IndexFile(unittest.TestCase):
    def test_as_weft_writes_it(self):
        self.assertEqual(
            weftmod.index_file("hello_weft", "A mod that does nothing, for testing weft-mod-action.",
                               "someone", "https://github.com/someone/mods", "hello", SHA,
                               "v26.11.2", []),
            WEFT_FILE.format(commit=SHA))

    def test_deps_and_escapes(self):
        text = weftmod.index_file("m", 'Says "hi"\\ to\nyou, ünïcode', "o", "https://github.com/o/r",
                                  "", SHA, "v1.2.3", ["cogwork", "a_b"])
        self.assertIn('deps = ["cogwork", "a_b"]\n', text)
        self.assertIn('dir = ""\n', text)
        try:
            import tomllib
        except ImportError:  # Python 3.10
            return
        data = tomllib.loads(text)
        self.assertEqual(list(data), INDEX_FIELDS)
        self.assertEqual(data["summary"], 'Says "hi"\\ to\nyou, ünïcode')
        self.assertEqual(data["deps"], ["cogwork", "a_b"])


class SdkTag(TempDir):
    def tag(self, toml):
        (self.tmp / "Cargo.toml").write_text(toml)
        mod = mock.Mock(folder=self.tmp)
        return weftmod.sdk_tag(mod)

    def test_the_weft_release(self):
        self.assertEqual(weftmod.sdk_tag(mock.Mock(folder=FIXTURE)), "v26.11.2")
        # A mod that is its own workspace, as structgen in Zorro909/weft-mods.
        self.assertEqual(self.tag(
            '[dependencies]\nweft_sdk.workspace = true\n\n[workspace.dependencies]\n'
            'weft_sdk = { git = "https://github.com/Weft-Rust/Weft", tag = "v26.11.3" }\n'
            'base_api = { git = "https://github.com/Weft-Rust/Weft", tag = "v26.11.3" }\n'),
            "v26.11.3")

    def test_only_what_the_index_can_set(self):
        for toml in [
                'weft_sdk = { git = "https://github.com/Weft-Rust/Weft", branch = "main" }\n',
                'weft_sdk = { git = "https://github.com/Weft-Rust/Weft.git", tag = "v1.0.0" }\n',
                'weft_sdk.workspace = true\n',  # from a workspace above: not in this Cargo.toml
                'weft_sdk = { git = "https://github.com/Weft-Rust/Weft", tag = "v1.0.0" }\n'
                '[target.x.dependencies]\n'
                'weft_sdk = { git = "https://github.com/Weft-Rust/Weft", tag = "v1.0.1" }\n']:
            self.assertIsNone(self.tag(toml), toml)


@unittest.skipUnless(shutil.which("cargo") and shutil.which("git"), "needs cargo and git")
class Submit(TempDir):
    REPO = "someone/mods"
    TAG = "hello_weft-v0.1.0"
    INDEX = "/repos/Weft-Rust/Weft-Mods-Index"
    FORK = "/repos/someone/Weft-Mods-Index"
    PATH = "mods/hello_weft/0.1.0.toml"
    BRANCH = "hello_weft-0.1.0"
    MAIN = "fedcba9876543210fedcba9876543210fedcba98"

    def setUp(self):
        super().setUp()
        self.clone = self.tmp / "clone"
        shutil.copytree(FIXTURE, self.clone / "hello")
        git = ["git", "-C", str(self.clone)]
        subprocess.run(git[:1] + ["init", "-q", str(self.clone)], check=True)
        subprocess.run(git + ["add", "-A"], check=True)
        subprocess.run(git + ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "x"],
                       check=True)
        self.commit = self.head()
        self.responses = {
            "/user": {"login": "someone", "id": 5},
            f"/repos/{self.REPO}/commits/{self.TAG}": {"sha": self.commit},
            f"/repos/{self.REPO}/releases/tags/{self.TAG}": {"draft": False},
            self.INDEX: {"default_branch": "main"},
            f"{self.INDEX}/git/ref/heads/main": {"object": {"sha": self.MAIN}},
            # The fork from before.
            self.FORK: {"full_name": "someone/Weft-Mods-Index", "fork": True,
                        "parent": {"full_name": "Weft-Rust/Weft-Mods-Index"}},
            f"{self.FORK}/git/ref/heads/main": {"object": {"sha": "0" * 40}},
            f"POST {self.INDEX}/pulls": {
                "html_url": "https://github.com/Weft-Rust/Weft-Mods-Index/pull/7"}}

    def head(self):
        return subprocess.run(["git", "-C", str(self.clone), "rev-parse", "HEAD"],
                              capture_output=True, text=True, check=True).stdout.strip()

    def declare_deps(self, deps):
        """A new commit of the mod, which declares these deps, for the tag."""
        cargo = self.clone / "hello" / "Cargo.toml"
        cargo.write_text(cargo.read_text().replace(
            "[lib]", f"[package.metadata.weft]\ndeps = {json.dumps(deps)}\n\n[lib]"))
        subprocess.run(["git", "-C", str(self.clone), "-c", "user.name=t", "-c", "user.email=t@t",
                        "commit", "-qam", "d"], check=True)
        self.commit = self.head()
        self.responses[f"/repos/{self.REPO}/commits/{self.TAG}"] = {"sha": self.commit}

    def submit(self, tags=(TAG,), dry_run=False):
        """Runs submit; returns its URLs. Leaves the GitHub calls in self.calls and self.sent."""
        gh = FakeGitHub(self.responses)
        try:
            with mock.patch.object(weftmod.time, "sleep") as self.sleep, \
                    mock.patch.object(weftmod.subprocess, "run", wraps=subprocess.run) as self.run:
                return weftmod.submit(gh, self.clone, self.REPO, "hello", list(tags), dry_run)
        finally:
            self.calls, self.sent = gh.calls, dict(gh.sent)

    def writes(self):
        return [c for c in self.calls if c.split(" ", 1)[0] in ("POST", "PUT", "PATCH", "DELETE")]

    def test_proposes_the_file_from_the_fork(self):
        self.assertEqual(self.submit(), ["https://github.com/Weft-Rust/Weft-Mods-Index/pull/7"])
        self.assertEqual(self.head(), self.commit)
        # The release built it already: nothing is built here.
        self.assertFalse([c for c in self.run.call_args_list if c.args[0][:2] == ["cargo", "build"]])
        self.assertEqual(self.writes(), [
            f"POST {self.FORK}/merge-upstream",
            f"POST {self.FORK}/git/refs",
            f"PUT {self.FORK}/contents/{self.PATH}",
            f"POST {self.INDEX}/pulls"])
        self.assertEqual(self.sent[f"POST {self.FORK}/git/refs"],
                         {"ref": f"refs/heads/{self.BRANCH}", "sha": self.MAIN})
        put = self.sent[f"PUT {self.FORK}/contents/{self.PATH}"]
        self.assertEqual((put["message"], put["branch"]), ("hello_weft 0.1.0", self.BRANCH))
        # weft's file, but with the summary from the lang file, whose description comes first.
        self.assertEqual(base64.b64decode(put["content"]).decode(),
                         WEFT_FILE.format(commit=self.commit).replace(
                             "nothing, for testing weft-mod-action.", "nothing."))
        pr = self.sent[f"POST {self.INDEX}/pulls"]
        self.assertEqual((pr["title"], pr["head"], pr["base"]),
                         ("hello_weft 0.1.0", f"someone:{self.BRANCH}", "main"))
        self.assertEqual(pr["body"], "Submitted with weft-mod-action")

    def test_the_declared_deps_are_in_the_file(self):
        self.declare_deps(["base", "cogwork"])
        self.submit()
        put = self.sent[f"PUT {self.FORK}/contents/{self.PATH}"]
        self.assertIn('\ndeps = ["cogwork"]\n', base64.b64decode(put["content"]).decode())

    def test_forks_the_index_when_the_account_has_no_fork(self):
        del self.responses[self.FORK]
        self.responses[f"POST {self.INDEX}/forks"] = {"full_name": "someone/Weft-Mods-Index-1"}
        fork = "/repos/someone/Weft-Mods-Index-1"
        self.responses[f"{fork}/git/ref/heads/main"] = Answers(
            [None, weftmod.Error("HTTP 409 Conflict: Git Repository is empty."),
             {"object": {"sha": self.MAIN}}])
        self.submit()
        self.assertEqual(self.sent[f"POST {self.INDEX}/forks"], {"default_branch_only": True})
        self.assertEqual(self.sleep.call_count, 2)
        self.assertIn(f"PUT {fork}/contents/{self.PATH}", self.calls)
        self.assertEqual(self.sent[f"POST {self.INDEX}/pulls"]["head"], f"someone:{self.BRANCH}")

    def test_a_fork_that_never_gets_ready_fails(self):
        del self.responses[f"{self.FORK}/git/ref/heads/main"]
        with self.assertRaisesRegex(weftmod.Error, "wasn't ready"):
            self.submit()
        self.assertNotIn(f"POST {self.INDEX}/pulls", self.calls)

    def test_a_fork_behind_the_index_still_proposes(self):
        self.responses[f"POST {self.FORK}/merge-upstream"] = weftmod.Error("HTTP 409 Conflict")
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(len(self.submit()), 1)
        self.assertIn("::warning::", err.getvalue())
        self.assertEqual(self.sent[f"POST {self.FORK}/git/refs"]["sha"], self.MAIN)

    def test_a_stale_branch_is_moved_to_the_index(self):
        self.responses[f"{self.FORK}/git/ref/heads/{self.BRANCH}"] = {"object": {"sha": "1" * 40}}
        self.submit()
        self.assertEqual(self.sent[f"PATCH {self.FORK}/git/refs/heads/{self.BRANCH}"],
                         {"sha": self.MAIN, "force": True})
        self.assertNotIn(f"POST {self.FORK}/git/refs", self.calls)

    def test_a_dry_run_shows_the_file_but_writes_nothing(self):
        self.declare_deps(["base", "cogwork"])
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(self.submit(dry_run=True), [])
        self.assertEqual(self.writes(), [])
        self.assertIn(f"would propose {self.PATH}:\n", err.getvalue())
        self.assertIn('deps = ["cogwork"]', err.getvalue())
        self.assertIn('owner = "someone"', err.getvalue())

    def test_a_dry_run_without_a_users_token_names_the_repository_owner(self):
        self.responses["/user"] = weftmod.Error("HTTP 403 Forbidden")
        self.REPO = "theirs/mods"
        self.responses[f"/repos/{self.REPO}/commits/{self.TAG}"] = {"sha": self.commit}
        self.responses[f"/repos/{self.REPO}/releases/tags/{self.TAG}"] = {"draft": False}
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.submit(dry_run=True)
        self.assertIn('\nauthor = "theirs"\n', err.getvalue())
        self.assertIn('\nowner = "theirs"\n', err.getvalue())
        self.assertIn('\nrepo = "https://github.com/theirs/mods"\n', err.getvalue())
        self.assertNotIn(f"{self.INDEX}/pulls?state=open&head=someone:{self.BRANCH}", self.calls)
        with self.assertRaisesRegex(weftmod.Error, "personal access token"):
            self.submit()

    def test_what_the_index_has_or_is_proposed_is_left_out(self):
        self.responses[f"{self.INDEX}/contents/{self.PATH}"] = {"type": "file"}
        self.assertEqual(self.submit(), [])
        self.assertEqual(self.writes(), [])
        del self.responses[f"{self.INDEX}/contents/{self.PATH}"]
        url = "https://github.com/Weft-Rust/Weft-Mods-Index/pull/3"
        self.responses[f"{self.INDEX}/pulls?state=open&head=someone:{self.BRANCH}"] = [
            {"html_url": url}]
        self.assertEqual(self.submit(), [url])
        self.assertEqual(self.writes(), [])

    def test_a_tag_without_a_release_is_refused(self):
        for release in (None, {"draft": True}):
            self.responses[f"/repos/{self.REPO}/releases/tags/{self.TAG}"] = release
            with self.assertRaisesRegex(weftmod.Error, "has no release of it"):
                self.submit()
            self.assertEqual(self.writes(), [])

    def test_weft_sdk_must_be_a_weft_release(self):
        cargo = self.clone / "hello" / "Cargo.toml"
        cargo.write_text(cargo.read_text().replace('tag = "v26.11.2"', 'branch = "main"'))
        subprocess.run(["git", "-C", str(self.clone), "-c", "user.name=t", "-c", "user.email=t@t",
                        "commit", "-qam", "b"], check=True)
        self.responses[f"/repos/{self.REPO}/commits/{self.TAG}"] = {"sha": self.head()}
        with self.assertRaisesRegex(weftmod.Error, "weft_sdk must come from"):
            self.submit()
        self.assertEqual(self.writes(), [])

    def test_a_dirty_checkout_is_cleaned(self):
        # Cargo.lock rewritten, and an untracked file: the file must be of the tagged commit.
        (self.clone / "hello" / "Cargo.lock").write_text("# rewritten\n")
        (self.clone / "hello" / "stray.lock").write_text("# untracked\n")
        self.assertEqual(len(self.submit()), 1)
        self.assertFalse((self.clone / "hello" / "stray.lock").exists())
        self.assertNotEqual((self.clone / "hello" / "Cargo.lock").read_text(), "# rewritten\n")

    def test_a_release_candidate_is_refused(self):
        tag = "hello_weft-v0.1.1-rc.4.gabcdef0"
        self.responses[f"/repos/{self.REPO}/commits/{tag}"] = {"sha": self.commit}
        self.responses[f"/repos/{self.REPO}/releases/tags/{tag}"] = {"prerelease": True}
        with self.assertRaisesRegex(weftmod.Error, "Cargo.toml says version 0.1.0"):
            self.submit([tag])

    def test_the_default_is_the_newest_full_release(self):
        self.responses[f"/repos/{self.REPO}/releases"] = [
            {"tag_name": "hello_weft-v0.1.1-rc.1.gabcdef0", "draft": False, "prerelease": True,
             "published_at": "2026-11-02T00:00:00Z"},
            {"tag_name": self.TAG, "draft": False, "prerelease": False,
             "published_at": "2026-11-01T00:00:00Z"}]
        self.assertEqual(weftmod.latest_tags(FakeGitHub(self.responses), self.REPO,
                                             [self.clone / "hello"]), [self.TAG])
        self.assertEqual(len(self.submit(tags=())), 1)

    def test_one_fork_for_several_releases(self):
        self.declare_deps([])  # a second commit, tagged 0.2.0
        cargo = self.clone / "hello" / "Cargo.toml"
        cargo.write_text(cargo.read_text().replace('version = "0.1.0"', 'version = "0.2.0"'))
        subprocess.run(["git", "-C", str(self.clone), "-c", "user.name=t", "-c", "user.email=t@t",
                        "commit", "-qam", "v"], check=True)
        self.responses[f"/repos/{self.REPO}/commits/hello_weft-v0.2.0"] = {"sha": self.head()}
        self.responses[f"/repos/{self.REPO}/releases/tags/hello_weft-v0.2.0"] = {"draft": False}
        self.responses[f"/repos/{self.REPO}/commits/{self.TAG}"] = {"sha": self.commit}
        self.assertEqual(len(self.submit([self.TAG, "hello_weft-v0.2.0"])), 2)
        self.assertEqual(self.calls.count(self.FORK), 1)
        self.assertIn(f"PUT {self.FORK}/contents/mods/hello_weft/0.2.0.toml", self.calls)


class GitHubErrors(unittest.TestCase):
    def test_an_error_says_what_github_said(self):
        body = io.BytesIO(json.dumps({"message": "Validation Failed", "errors": [
            {"message": "A pull request already exists for someone:m-1.0.0."}]}).encode())
        err = weftmod.urllib.error.HTTPError("https://api.github.com/x", 422, "Unprocessable",
                                             {}, body)
        with mock.patch.object(weftmod.urllib.request, "urlopen", side_effect=err):
            with self.assertRaisesRegex(weftmod.Error, "HTTP 422 Unprocessable: Validation Failed; "
                                                        "A pull request already exists"):
                weftmod.GitHub("t").api("/x", method="POST", data={})


if __name__ == "__main__":
    unittest.main()

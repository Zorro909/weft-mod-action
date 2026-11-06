"""Unit tests of weftmod.py: `python3 -m unittest discover tests`. The plan tests need cargo."""

from __future__ import annotations

import json
import shutil
import struct
import sys
import tempfile
import unittest
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
    """Answers `api` from a dict of path -> JSON; anything else is a 404 (None)."""

    def __init__(self, responses: dict | None = None):
        self.responses = responses or {}
        self.calls: list[str] = []

    def api(self, path):
        self.calls.append(path)
        return self.responses.get(path)

    def pages(self, path):
        return self.responses.get(path, [])


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

    def test_summary(self):
        self.assertEqual(weftmod.summarize("{gold}Gears{/} and belts. More later.\\nNext line"),
                         "Gears and belts.")
        long = "word " * 60
        s = weftmod.summarize(long)
        self.assertLessEqual(len(s), weftmod.SUMMARY_MAX)
        self.assertTrue(s.endswith("…"))

    def test_lang(self):
        self.assertEqual(weftmod.parse_lang("# c\na.b = x = y\n\nbad\n"), {"a.b": "x = y"})

    def test_fnv(self):
        self.assertEqual(weftmod.fnv1a64(b""), "cbf29ce484222325")
        self.assertEqual(weftmod.fnv1a64(b"a"), "af63dc4c8601ec8c")


class WeftName(unittest.TestCase):
    def test_exact_tag_or_describe_name(self):
        tags = [{"name": "v26.11.1", "commit": {"sha": "a" * 40}},
                {"name": "v26.11.2", "commit": {"sha": "b" * 40}},
                {"name": "nightly", "commit": {"sha": "c" * 40}}]
        base = f"/repos/{weftmod.WEFT_REPO}"
        gh = FakeGitHub({
            f"{base}/tags": tags,
            f"{base}/compare/v26.11.1...{'c' * 40}": {"status": "ahead", "ahead_by": 9},
            f"{base}/compare/v26.11.2...{'c' * 40}": {"status": "ahead", "ahead_by": 3},
        })
        self.assertEqual(weftmod.weft_name(gh, "b" * 40), "v26.11.2")
        self.assertEqual(weftmod.weft_name(gh, "c" * 40), "v26.11.2-3-gccccccc")
        with self.assertRaises(weftmod.Error):
            weftmod.weft_name(gh, "d" * 40)


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
        self.assertEqual(weftmod.pack("m", libs, assets, out),
                         ["windows-x64", "linux-x64", "macos-arm64"])
        with zipfile.ZipFile(out) as z:
            self.assertEqual(z.namelist(),
                             ["libm.dylib", "libm.so", "m.dll", "assets/lang/en_us.lang"])

    def test_same_input_same_bytes(self):
        libs, assets = self.libraries()
        weftmod.pack("m", libs, assets, self.tmp / "a.zip")
        weftmod.pack("m", libs, assets, self.tmp / "b.zip")
        self.assertEqual((self.tmp / "a.zip").read_bytes(), (self.tmp / "b.zip").read_bytes())

    def test_a_library_must_match_its_name(self):
        libs, assets = self.libraries()
        (libs / "libm.so").write_bytes(pe())
        with self.assertRaisesRegex(weftmod.Error, "libm.so is not a library"):
            weftmod.pack("m", libs, assets, self.tmp / "m.zip")


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


if __name__ == "__main__":
    unittest.main()

"""Unit tests of weftmod.py: `python3 -m unittest discover tests`. The plan tests need cargo."""

from __future__ import annotations

import base64
import hashlib
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
        s = weftmod.summarize("word " * 60)
        self.assertLessEqual(len(s), weftmod.SUMMARY_MAX)
        self.assertTrue(s.endswith("…"))

    def test_lang(self):
        self.assertEqual(weftmod.parse_lang("# c\na.b = x = y\n\nbad\n"), {"a.b": "x = y"})

    def test_fnv(self):
        self.assertEqual(weftmod.fnv1a64(b""), "cbf29ce484222325")
        self.assertEqual(weftmod.fnv1a64(b"a"), "af63dc4c8601ec8c")


class Toml(unittest.TestCase):
    FIELDS = {"id": "m", "name": 'A "quoted" mod\n', "version": "0.1.1-rc.4.gabcdef0",
              "abi": "f470b1cbb1f9ad91", "deps": ["base", "cogwork"]}

    def test_a_raw_del_is_escaped(self):
        self.assertEqual(weftmod.to_toml({"name": "a\x7fb"}), 'name = "a\\u007fb"\n')

    def test_weft_mod_toml(self):
        self.assertEqual(weftmod.to_toml(self.FIELDS),
                         'id = "m"\nname = "A \\"quoted\\" mod\\n"\n'
                         'version = "0.1.1-rc.4.gabcdef0"\nabi = "f470b1cbb1f9ad91"\n'
                         'deps = ["base", "cogwork"]\n')

    @unittest.skipIf(sys.version_info < (3, 11), "tomllib is Python 3.11+")
    def test_reads_back_as_toml(self):
        import tomllib
        self.assertEqual(tomllib.loads(weftmod.to_toml(self.FIELDS)), self.FIELDS)


def bundle(*digests: str) -> dict:
    statement = {"_type": "https://in-toto.io/Statement/v1",
                 "subject": [{"name": "x.zip", "digest": {"sha256": d}} for d in digests]}
    return {"mediaType": "application/vnd.dev.sigstore.bundle.v0.3+json",
            "dsseEnvelope": {"payload": base64.b64encode(json.dumps(statement).encode()).decode(),
                             "payloadType": "application/vnd.in-toto+json"}}


class Attach(TempDir):
    def test_each_zip_gets_the_bundle_that_names_it(self):
        entries = []
        for mod_id in ("a", "b"):
            (self.tmp / f"{mod_id}-v1.0.0").mkdir()
            (self.tmp / f"{mod_id}-v1.0.0" / f"{mod_id}.zip").write_bytes(mod_id.encode())
            entries.append({"id": mod_id, "tag": f"{mod_id}-v1.0.0"})
        a, b = (hashlib.sha256(x).hexdigest() for x in (b"a", b"b"))
        lines = self.tmp / "attestation.jsonl"
        lines.write_text(json.dumps(bundle("0" * 64)) + "\n" + json.dumps(bundle(b, a)) + "\n")
        weftmod.attach(entries, self.tmp, weftmod.read_bundles(lines))
        for mod_id in ("a", "b"):
            info = json.loads((self.tmp / f"{mod_id}-v1.0.0" / f"{mod_id}.mod.json").read_text())
            self.assertEqual(info, {"attestation": bundle(b, a)})

    def test_a_zip_without_a_bundle_fails(self):
        one = self.tmp / "bundle.json"
        one.write_text(json.dumps(bundle("0" * 64), indent=2))
        with self.assertRaisesRegex(weftmod.Error, "no attestation"):
            weftmod.bundle_for("1" * 64, weftmod.read_bundles(one))


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
        self.assertEqual(weftmod.pack('id = "m"\n', libs, assets, out),
                         ["windows-x64", "linux-x64", "macos-arm64"])
        with zipfile.ZipFile(out) as z:
            self.assertEqual(z.namelist(), ["weft-mod.toml", "libm.dylib", "libm.so", "m.dll",
                                            "assets/lang/en_us.lang"])
            self.assertEqual(z.read("weft-mod.toml"), b'id = "m"\n')

    def test_same_input_same_bytes(self):
        libs, assets = self.libraries()
        weftmod.pack("", libs, assets, self.tmp / "a.zip")
        weftmod.pack("", libs, assets, self.tmp / "b.zip")
        self.assertEqual((self.tmp / "a.zip").read_bytes(), (self.tmp / "b.zip").read_bytes())

    def test_a_library_must_match_its_name(self):
        libs, assets = self.libraries()
        (libs / "libm.so").write_bytes(pe())
        with self.assertRaisesRegex(weftmod.Error, "libm.so is not a library"):
            weftmod.pack("", libs, assets, self.tmp / "m.zip")


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


def index_with(root: Path, mods: dict[str, tuple[str, list[str]]]) -> Path:
    """An index checkout: `mods` maps an id to the text of its mod.toml and its versions."""
    for mod_id, (meta, versions) in mods.items():
        (root / "mods" / mod_id / "versions").mkdir(parents=True)
        (root / "mods" / mod_id / "mod.toml").write_text(meta)
        for v in versions:
            (root / "mods" / mod_id / "versions" / f"{v}.toml").write_text("")
    return root


class Summaries(TempDir):
    def test_new_tags(self):
        index = index_with(self.tmp, {"a": ('summary = "A"\n', ["1.0.0"])})
        self.assertEqual(weftmod.new_tags(index, ["a-v1.0.0", "a-v1.1.0", "b-v0.1.0"], {"b": "B"}),
                         ["a-v1.1.0", "b-v0.1.0"])
        with self.assertRaisesRegex(weftmod.Error, "c is new to the index"):
            weftmod.new_tags(index, ["c-v1.0.0"], {})

    @unittest.skipIf(sys.version_info < (3, 11), "tomllib is Python 3.11+")
    def test_set_summaries_keeps_the_other_keys(self):
        index = index_with(self.tmp, {"a": ('summary = "Old"\nname = "A"\n', ["1.0.0"])})
        self.assertEqual(weftmod.set_summaries(index, {"a": "New", "b": "Not indexed"}), ["a"])
        self.assertEqual((index / "mods/a/mod.toml").read_text(), 'summary = "New"\nname = "A"\n')
        self.assertEqual(weftmod.set_summaries(index, {"a": "New"}), [])


# Stands in for the index's tool: add-release writes what the real one would, and records its
# arguments; validate passes.
FAKE_WEFTINDEX = """
import json, sys
from pathlib import Path
args = sys.argv[1:]
calls = Path("calls.json")
calls.write_text(json.dumps((json.loads(calls.read_text()) if calls.exists() else []) + [args]))
if args[0] == "add-release":
    end = args.index("--summary") if "--summary" in args else len(args)
    for tag in args[2:end]:
        mod_id, _, version = tag.partition("-v")
        (Path("mods") / mod_id / "versions").mkdir(parents=True, exist_ok=True)
        (Path("mods") / mod_id / "versions" / f"{version}.toml").write_text("")
        if not (Path("mods") / mod_id / "mod.toml").exists():
            (Path("mods") / mod_id / "mod.toml").write_text(f"summary = {json.dumps(args[end + 1])}\\n")
    Path("dist").mkdir(exist_ok=True)
    Path("dist/pr-title.txt").write_text("Add " + " ".join(args[2:end]))
    Path("dist/pr.md").write_text("checked")
"""


@unittest.skipUnless(shutil.which("cargo"), "needs cargo")
@unittest.skipIf(sys.version_info < (3, 11), "tomllib is Python 3.11+")
class Propose(TempDir):
    def index(self, mods):
        index = index_with(self.tmp, mods)
        (index / "weftindex").mkdir()
        (index / "weftindex" / "__main__.py").write_text(FAKE_WEFTINDEX)
        return index

    def propose(self, index, tags):
        return weftmod.propose(index, "someone/mods", [FIXTURE], tags)

    def calls(self, index):
        return json.loads((index / "calls.json").read_text())

    def test_a_new_mod_gets_its_summary(self):
        index = self.index({})
        title, body = self.propose(index, ["hello_weft-v0.1.0"])
        self.assertEqual(title, "Add hello_weft-v0.1.0")
        self.assertEqual(self.calls(index)[0], ["add-release", "someone/mods", "hello_weft-v0.1.0",
                                                "--summary", "A mod that does nothing."])
        self.assertEqual((index / "mods/hello_weft/mod.toml").read_text(),
                         'summary = "A mod that does nothing."\n')

    def test_a_changed_summary_alone_is_proposed(self):
        index = self.index({"hello_weft": ('summary = "Old"\n', ["0.1.0"])})
        title, body = self.propose(index, ["hello_weft-v0.1.0"])
        self.assertEqual(title, "Update the summary of hello_weft")
        self.assertIn("- hello_weft: A mod that does nothing.", body)
        self.assertEqual(self.calls(index), [["validate"]])  # no add-release

    def test_nothing_new_proposes_nothing(self):
        index = self.index({"hello_weft": ('summary = "A mod that does nothing."\n', ["0.1.0"])})
        self.assertIsNone(self.propose(index, ["hello_weft-v0.1.0"]))


if __name__ == "__main__":
    unittest.main()

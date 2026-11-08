"""Unit tests of weftmod.py: `python3 -m unittest discover tests`. The plan tests need cargo."""

from __future__ import annotations

import base64
import contextlib
import hashlib
import io
import json
import shutil
import os
import struct
import subprocess
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
        self.sent: list[tuple[str, dict]] = []

    def api(self, path, method="GET", data=None):
        self.calls.append(path if method == "GET" else f"{method} {path}")
        if data is not None:
            self.sent.append((f"{method} {path}", data))
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

    def test_with_deps(self):
        text = 'name = "m"\n# Mod ids it needs besides base.\ndeps = []\nowner = "o"\n'
        self.assertEqual(weftmod.with_deps(text, ["cogwork", "a_b"]),
                         'name = "m"\n# Mod ids it needs besides base.\n'
                         'deps = ["cogwork", "a_b"]\nowner = "o"\n')
        self.assertEqual(weftmod.with_deps(text, []), text)
        self.assertIsNone(weftmod.with_deps('name = "m"\n', ["a"]))


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


# Stands in for `weft --submit-mod`: records how it was run, and prints what weft prints.
FAKE_WEFT = """#!/bin/sh
echo "$PWD $* gh=${GH_TOKEN:-none} helper=${GIT_CONFIG_VALUE_1:-none}" >> "$(dirname "$0")/calls"
if [ -n "$GH_TOKEN" ]; then
  echo "pull request opened: https://github.com/Weft-Rust/Weft-Mods-Index/pull/7"
else
  echo "GitHub opened in the browser: fill in its name and summary if you like."
  echo "https://github.com/Weft-Rust/Weft-Mods-Index/new/main?filename=mods/hello_weft/0.1.0.toml&value=name%20%3D%20%22hello_weft%22%0Adeps%20%3D%20%5B%5D%0A"
fi
"""


@unittest.skipUnless(shutil.which("cargo") and shutil.which("git"), "needs cargo and git")
class Submit(TempDir):
    REPO = "someone/mods"
    TAG = "hello_weft-v0.1.0"
    INDEX = "/repos/Weft-Rust/Weft-Mods-Index"
    FORK_FILE = "/repos/someone/Weft-Mods-Index/contents/mods/hello_weft/0.1.0.toml"

    def setUp(self):
        super().setUp()
        self.clone = self.tmp / "clone"
        shutil.copytree(FIXTURE, self.clone / "hello")
        git = ["git", "-C", str(self.clone)]
        subprocess.run(git[:1] + ["init", "-q", str(self.clone)], check=True)
        subprocess.run(git + ["add", "-A"], check=True)
        subprocess.run(git + ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "x"],
                       check=True)
        self.commit = subprocess.run(git + ["rev-parse", "HEAD"], capture_output=True, text=True,
                                     check=True).stdout.strip()
        self.weft = self.tmp / "bin" / "weft"
        self.weft.parent.mkdir()
        self.weft.write_text(FAKE_WEFT)
        self.weft.chmod(0o755)
        self.responses = {"/user": {"login": "someone", "id": 5},
                          f"/repos/{self.REPO}/commits/{self.TAG}": {"sha": self.commit},
                          f"{self.INDEX}/pulls/7": {"head": {
                              "repo": {"full_name": "someone/Weft-Mods-Index"},
                              "ref": "hello_weft-0.1.0"}},
                          f"{self.FORK_FILE}?ref=hello_weft-0.1.0": {
                              "sha": "blob1", "content": base64.b64encode(
                                  b'name = "hello_weft"\ndeps = []\nowner = "someone"\n').decode()}}

    def declare_deps(self, deps):
        """A new commit of the mod, which declares these deps, for the tag."""
        cargo = self.clone / "hello" / "Cargo.toml"
        cargo.write_text(cargo.read_text().replace(
            "[lib]", f"[package.metadata.weft]\ndeps = {json.dumps(deps)}\n\n[lib]"))
        git = ["git", "-C", str(self.clone)]
        subprocess.run(git + ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qam", "d"],
                       check=True)
        self.responses[f"/repos/{self.REPO}/commits/{self.TAG}"] = {"sha": subprocess.run(
            git + ["rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()}

    def submit(self, tags=(TAG,), dry_run=False, env=None):
        gh = FakeGitHub(self.responses)
        old = dict(os.environ)
        os.environ.update(env if env is not None else {"GH_TOKEN": "tok"})
        try:
            urls = weftmod.submit(gh, self.clone, self.REPO, "hello", list(tags), str(self.weft),
                                  dry_run)
        finally:
            os.environ.clear()
            os.environ.update(old)
        calls = (self.weft.parent / "calls").read_text().splitlines() \
            if (self.weft.parent / "calls").exists() else []
        self.sent = gh.sent
        return urls, calls, gh.calls

    def test_runs_weft_in_the_mod_folder_at_the_tagged_commit(self):
        urls, calls, _ = self.submit()
        self.assertEqual(urls, ["https://github.com/Weft-Rust/Weft-Mods-Index/pull/7"])
        folder = self.clone / "hello"
        self.assertEqual(calls, [f"{folder} --submit-mod {folder} gh=tok "
                                 f"helper=!gh auth git-credential"])

    def test_only_base_needs_no_commit(self):
        self.submit()  # the fixture's declare_mod! has deps: ["base"]
        self.assertEqual(self.sent, [])

    def test_the_declared_deps_are_set_in_the_pull_request(self):
        self.declare_deps(["base", "cogwork"])
        self.submit()
        [(call, data)] = self.sent
        self.assertEqual(call, f"PUT {self.FORK_FILE}")
        self.assertEqual((data["branch"], data["sha"]), ("hello_weft-0.1.0", "blob1"))
        self.assertEqual(base64.b64decode(data["content"]).decode(),
                         'name = "hello_weft"\ndeps = ["cogwork"]\nowner = "someone"\n')

    def test_a_dry_run_shows_the_deps(self):
        self.declare_deps(["base", "cogwork"])
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.submit(dry_run=True)
        self.assertIn('deps = ["cogwork"]', err.getvalue())

    def test_a_dry_run_opens_nothing(self):
        urls, calls, gh_calls = self.submit(dry_run=True)
        self.assertEqual(urls, [])
        self.assertIn("gh=none helper=none", calls[0])
        self.assertNotIn("/user", gh_calls)
        self.assertEqual(weftmod.proposed_file(
            "x\nhttps://github.com/Weft-Rust/Weft-Mods-Index/new/main?filename=mods/a/1.toml"
            "&value=name%20%3D%20%22a%22%0A\n"), ("mods/a/1.toml", 'name = "a"\n'))

    def test_what_the_index_has_or_is_proposed_is_left_out(self):
        self.responses[f"{self.INDEX}/contents/mods/hello_weft/0.1.0.toml"] = {"type": "file"}
        self.assertEqual(self.submit()[:2], ([], []))
        del self.responses[f"{self.INDEX}/contents/mods/hello_weft/0.1.0.toml"]
        url = "https://github.com/Weft-Rust/Weft-Mods-Index/pull/3"
        self.responses[f"{self.INDEX}/pulls?state=open&head=someone:hello_weft-0.1.0"] = [
            {"html_url": url}]
        self.assertEqual(self.submit()[:2], ([url], []))

    def test_a_stale_branch_is_deleted_first(self):
        ref = "/repos/someone/Weft-Mods-Index/git/ref/heads/hello_weft-0.1.0"
        self.responses[ref] = {"ref": "refs/heads/hello_weft-0.1.0"}
        _, calls, gh_calls = self.submit()
        self.assertIn("DELETE /repos/someone/Weft-Mods-Index/git/refs/heads/hello_weft-0.1.0",
                      gh_calls)
        self.assertEqual(len(calls), 1)

    def test_a_release_candidate_is_refused(self):
        tag = "hello_weft-v0.1.1-rc.4.gabcdef0"
        self.responses[f"/repos/{self.REPO}/commits/{tag}"] = {"sha": self.commit}
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
        self.assertEqual(len(self.submit(tags=())[0]), 1)


if __name__ == "__main__":
    unittest.main()

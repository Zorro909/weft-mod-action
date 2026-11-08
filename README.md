# weft-mod-action

GitHub Actions that release [Weft](https://github.com/Weft-Rust/Weft) mods and propose them to the
[Weft mod index](https://github.com/Weft-Rust/Weft-Mods-Index).

- **`build`** decides which mods to release and at which version, then builds them with cargo on
  the runner it runs on. Run it on Windows, Linux and macOS.
- **`release`** packs each mod into `<id>.zip`, attests its build provenance into
  `<id>.mod.json`, and creates a GitHub release of each.
- **`index-pr`** proposes releases to the mod index with Weft's own `weft --submit-mod`.

They are composite actions, so they run as steps of your own workflow. The provenance attestation
then names your repository's workflow as its signer, which is what the index and the launcher
check.

## Releasing

Copy [`examples/release.yml`](examples/release.yml) to `.github/workflows/release.yml`. It runs on
every push to `main`:

```yaml
jobs:
  build:
    strategy:
      matrix:
        os: [windows-latest, ubuntu-22.04, macos-latest]
    runs-on: ${{ matrix.os }}
    steps:
      - uses: actions/checkout@v7
        with:
          submodules: true
      - uses: Zorro909/weft-mod-action/build@v2

  release:
    needs: build
    runs-on: ubuntu-latest
    permissions:
      contents: write
      id-token: write
      attestations: write
    steps:
      - uses: actions/checkout@v7
        with:
          submodules: true
      - uses: Zorro909/weft-mod-action/release@v2
```

A mod is a folder with a `Cargo.toml` that builds a `cdylib`. The library's name is the mod's id,
and its releases are tagged `<id>-v<version>`. With several mods in one repository, list their
folders:

```yaml
      - uses: Zorro909/weft-mod-action/build@v2
        with:
          mods: |
            cogwork
            structures
```

### Which version is released

For each mod, `build` reads the version in its `Cargo.toml`:

| The tag `<id>-v<version>` | What happens |
| --- | --- |
| doesn't exist | The mod is released at that version: a release for `X.Y.Z`, a prerelease for `X.Y.Z-rc.1` and the like. |
| exists, and the mod changed since its newest tag | A release candidate after the newer of that version and the newest full release (`X.Y.Z`; release candidates don't count): `X.Y.(Z+1)-rc.<run>.g<sha7>`, for example `0.2.2-rc.57.g1a2b3c4`. `<run>` is the workflow's run number, `<sha7>` the commit. |
| exists, and the mod didn't change since its newest tag | Nothing. Set `release-unchanged: true` to make a release candidate anyway. |

A mod changed when a file under its folder, or a Weft submodule, changed. For a mod at the
repository root, any file counts.

To release a version, set it in `Cargo.toml` and push. The build sets `[package] version` to the
version it releases, so that the library reports it, but doesn't commit that change. A
`version.workspace = true` can't be set: the library then keeps the workspace's version.

### Inputs

`build`:

| Input | Default | |
| --- | --- | --- |
| `mods` | `.` | The mod folders, one per line. |
| `release-unchanged` | `false` | Release a candidate even when a mod didn't change. |
| `cargo-args` | | More arguments for `cargo build --release`. |
| `rust-toolchain` | `stable` | The toolchain to install; empty to use the runner's (or `rust-toolchain.toml`). |

`release`:

| Input | Default | |
| --- | --- | --- |
| `github-release` | `true` | Create the GitHub releases. With `false`, the packages are only the workflow artifact `weft-mods`. |
| `attest` | `true` | Attest the zips' build provenance, into their `<id>.mod.json`. A GitHub release needs this. |
| `require-platforms` | `windows-x64 linux-x64 macos-arm64` | The platforms every zip must have. |

Both have the output `tags`: the tags released, separated by spaces. `release` also has the output
`releases`: the full releases (`X.Y.Z`, no release candidates) it made, which
[can go to the index](#on-every-full-release).

### What a release holds

Each release has two assets, which the launcher reads:

| Asset | Content |
| --- | --- |
| `<id>.zip` | The Windows, Linux and macOS libraries (`<id>.dll`, `lib<id>.so`, `lib<id>.dylib`) at its top, and the mod's `assets/` folder, as `weft --pack-mod` packs them |
| `<id>.mod.json` | `{"attestation": <bundle>}`: the Sigstore bundle of the zip's build provenance attestation, as `actions/attest-build-provenance` writes it |

When several mods release in one run, one attestation covers all their zips, and each
`<id>.mod.json` holds that bundle.

## Proposing to the index

The [Weft mod index](https://github.com/Weft-Rust/Weft-Mods-Index) builds each mod itself, from
its source at the commit proposed, and lists it in the launcher. `index-pr` proposes a release
the way the index asks: it runs [`weft --submit-mod`](https://github.com/Weft-Rust/Weft-Mods-Index#sending-a-mod)
at the commit of the release's tag. So the pull request is always in the form the current Weft
writes. Each release is one pull request, which adds `mods/<id>/<version>.toml`.

The index builds the mod's folder alone, without submodules, at the version in its `Cargo.toml`.
So a mod it takes:

- takes the SDK from the game's repository at a release tag:
  `weft_sdk = { git = "https://github.com/Weft-Rust/Weft", tag = "v26.11.3" }`;
- has a `[package] name` that is its library's name, the mod id;
- is proposed at a release of the version in its `Cargo.toml`. A release candidate that `build`
  made (`X.Y.(Z+1)-rc.<run>.g<sha7>`) has another version than its commit's `Cargo.toml`, so
  `index-pr` refuses it. By default it proposes full releases (`X.Y.Z`); a prerelease set in
  `Cargo.toml`, such as `X.Y.Z-rc.1`, goes only when named in `tags`.

It needs a token that can fork the index and open the pull request, which `GITHUB_TOKEN` can't:

1. Create a [classic personal access token](https://github.com/settings/tokens/new) with the scope
   `public_repo`. The mod ids are its account's: only it can propose more versions of them.
2. Add it to your mod's repository as the Actions secret `WEFT_INDEX_TOKEN`.

### By hand

Copy [`examples/index-pr.yml`](examples/index-pr.yml) to `.github/workflows/index-pr.yml`, and run
it from the Actions tab. It proposes the newest full release of each mod, or the tags you give.
With `dry-run`, it only shows the file each pull request would add.

### On every full release

[`examples/release.yml`](examples/release.yml) has an `index-pr` job that proposes each full
release as `release` makes it. It is off until you set the repository variable `WEFT_INDEX_PR` to
`true` (Settings → Secrets and variables → Actions → Variables) and add the secret
`WEFT_INDEX_TOKEN`:

```yaml
  release:
    # ...
    outputs:
      releases: ${{ steps.release.outputs.releases }}
    steps:
      # ...
      - id: release
        uses: Zorro909/weft-mod-action/release@v2

  index-pr:
    needs: release
    if: needs.release.outputs.releases != '' && vars.WEFT_INDEX_PR == 'true'
    runs-on: ubuntu-latest
    steps:
      - uses: Zorro909/weft-mod-action/index-pr@v2
        with:
          token: ${{ secrets.WEFT_INDEX_TOKEN }}
          tags: ${{ needs.release.outputs.releases }}
```

### What it does

For each tag, `index-pr`:

1. leaves it out when the index has that version, or when the token's account has an open pull
   request for it;
2. clones your repository and checks out the tag's commit;
3. runs `weft --submit-mod <folder>` with the token. weft builds the mod once, forks the index to
   the token's account and opens the pull request.

The `weft` binary comes from the newest Weft release. While no release has `--submit-mod` yet, it
is built from Weft's default branch, which takes a few minutes the first time and is cached
afterwards.

| Input | Default | |
| --- | --- | --- |
| `token` | | The token. |
| `tags` | the newest full release of each mod | Release tags, separated by spaces. |
| `mods` | `.` | The mod folders. |
| `weft-version` | `latest` | The Weft whose `weft --submit-mod` runs: `latest`, a release tag, or a branch or commit of Weft to build. |
| `rust-toolchain` | `stable` | The toolchain for weft's test build; empty to use the runner's. |
| `dry-run` | `false` | Only show each file; open no pull request. |

Its output `urls` is the pull requests, separated by spaces. It runs on Linux.

weft writes the file as it sees fit: at present `name` is the mod id and `summary` is
`[package] description`. Its `deps` it leaves empty, so `index-pr` sets them, with a commit on the
pull request's branch: the mod ids the mod needs besides `base`, from `[package.metadata.weft] deps`,
or else the `deps` of `declare_mod!` in the library's source. When `declare_mod!` can't be read as
plain string literals (a macro, or several under `#[cfg]`), set the deps yourself:

```toml
[package.metadata.weft]
deps = ["base", "cogwork"]
```

## Development

```sh
python3 -m unittest discover -s tests   # needs cargo for the plan and propose tests
```

`weftmod.py` does the work, with the Python standard library only, so that it runs on every runner
without a setup step. The workflow `test.yml` also builds and packs `tests/fixture` on all three
systems. Tag releases `v2.x.y` and move the `v2` tag to the newest.

## License

[MIT](LICENSE)

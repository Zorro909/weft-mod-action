# weft-mod-action

GitHub Actions that release [Weft](https://github.com/Weft-Rust/Weft) mods and propose them to the
[Weft mod index](https://github.com/Zorro909/weft-mods-index).

- **`build`** decides which mods to release and at which version, then builds them with cargo on
  the runner it runs on. Run it on Windows, Linux and macOS.
- **`release`** packs each mod into `<id>.zip` with its `<id>.mod.json`, attests their build
  provenance, and creates a GitHub release of each.
- **`index-pr`** opens a pull request on the mod index that adds releases.

They are composite actions, so they run as steps of your own workflow. The provenance attestation
then names your repository's workflow as its signer, which is what the index checks.

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
      - uses: Zorro909/weft-mod-action/build@v1

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
      - uses: Zorro909/weft-mod-action/release@v1
```

A mod is a folder with a `Cargo.toml` that builds a `cdylib`. The library's name is the mod's id,
and its releases are tagged `<id>-v<version>`. With several mods in one repository, list their
folders:

```yaml
      - uses: Zorro909/weft-mod-action/build@v1
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
| exists, and the mod changed since its newest release | A release candidate after the newer of that version and the newest release: `X.Y.(Z+1)-rc.<run>.g<sha7>`, for example `0.2.2-rc.57.g1a2b3c4`. `<run>` is the workflow's run number, `<sha7>` the commit. |
| exists, and the mod didn't change | Nothing. Set `release-unchanged: true` to make a release candidate anyway. |

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
| `attest` | `true` | Attest the zips' build provenance. The index needs this. |
| `require-platforms` | `windows-x64 linux-x64 macos-arm64` | The platforms every zip must have. |

Both have the output `tags`: the tags released, separated by spaces.

### What `<id>.mod.json` says

`release` writes it from the build, Cargo and the mod's files. See the index's
[README](https://github.com/Zorro909/weft-mods-index#idmodjson) for its fields.

| Field | From, in order |
| --- | --- |
| `name` | `[package.metadata.weft] name`; `mod.<id>.name` in `assets/lang/en_us.lang`; the id |
| `summary` | `[package.metadata.weft] summary`; the first sentence of `mod.<id>.description`; `[package] description` |
| `authors` | `[package.metadata.weft] authors`; `mod.<id>.authors` (separated by commas); `[package] authors`; the repository's owner |
| `deps` | `[package.metadata.weft] deps`; the `deps` of `declare_mod!` in the library's source |
| `license`, `homepage` | `[package]`; the homepage defaults to the mod's folder on GitHub |
| `icon` | `assets/textures/gui/icon.png`, if it is a PNG |
| `weft` | The `weft_abi` crate the mod builds against: a git dependency on `Weft-Rust/Weft`, or a path dependency into a clean Weft checkout, such as a submodule |

When `declare_mod!` can't be read as plain string literals (a macro, or several under `#[cfg]`),
set the deps yourself:

```toml
[package.metadata.weft]
deps = ["base"]
```

The zip holds the Windows, Linux and macOS libraries at its top, and the mod's `assets/` folder.

## Proposing to the index

Copy [`examples/index-pr.yml`](examples/index-pr.yml) to `.github/workflows/index-pr.yml`, and run
it from the Actions tab. It forks the index to your account, adds the newest release of each mod
(or the tags you give) with the index's own `add-release`, and opens a pull request. Running it
again for the same tags updates that pull request.

It needs a token that can push to your fork and open the pull request, which `GITHUB_TOKEN` can't:

1. Create a [classic personal access token](https://github.com/settings/tokens/new) with the scope
   `public_repo`.
2. Add it to your mod's repository as the Actions secret `WEFT_INDEX_TOKEN`.

| Input | Default | |
| --- | --- | --- |
| `token` | | The token. |
| `tags` | the newest release of each mod | Release tags, separated by spaces. |
| `mods` | `.` | The mod folders, to find their newest releases. |
| `index` | `Zorro909/weft-mods-index` | The index repository. |
| `index-ref` | `main` | Its branch. |

Its output `url` is the pull request.

## Development

```sh
python3 -m unittest discover -s tests   # needs cargo for the plan tests
```

`weftmod.py` does the work, with the Python standard library only, so that it runs on every runner
without a setup step. The workflow `test.yml` also builds and packs `tests/fixture` on all three
systems. Tag releases `v1.x.y` and move the `v1` tag to the newest.

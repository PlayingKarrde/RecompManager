# Lantern Engine (Bloodborne)

Repo: https://github.com/Awakenchan/LanternEngine · Recipe: `lantern-engine` · Researched
2026-11-07 (README and release v0.9.39-beta.31; Windows zip listed, not run).

## Releases

**Every release so far is a GitHub pre-release** (betas, several a week), so the recipe uses
`channel: prerelease`. With `stable` the server would never find a version.

| Platform key | Asset | Notes |
|---|---|---|
| `windows-x64` | `LanternEngine-<version>-win64.zip` (~233 MB) | Unpacks to `BloodbornePC/` with `Lantern Engine.exe` (launcher), `Bloodborne.exe` (starts the game directly), `Bloodborne Launcher.exe` |
| `linux-x64` | `LanternEngine-<version>-linux-x64.tar.gz` (~100 MB) | Beta. Needs Vulkan (Mesa or NVIDIA), Python 3, glibc ≥ 2.38. Layout not inspected (**unverified**) |

Each release also has `Bloodborne.Launcher.exe` and `Lantern.Engine.exe` (byte-identical, a
standalone launcher) and a tiny `package.json`. The recipe ignores them.

## Input

A **folder**: an extracted dump of CUSA03173 at update 1.09, the folder with `eboot.bin`.
Verification: `eboot.bin` and `sce_sys/param.sfo` must exist. Parsing the version from
`param.sfo` is planned (Phase 2).

## Placing the input and saves

- Windows: run `Lantern Engine.exe`, pick the game folder, press Play. `Bloodborne.exe` starts
  the game directly (for Steam).
- Linux: `./lantern.sh /path/to/CUSA03173` takes the folder on the command line, so launch
  metadata can pass it without any config file.
- Where the Windows launcher stores the chosen folder: **unknown**. Until found, the install
  plan adds an `instructions` step.
- Save location: **unknown** (not in the README; the Help & FAQ site may say).

## Strategy

`runtime` with a folder input.

## Notes

- The launcher updates itself (Linux too since 0.9.37 beta 7). A managed install may want that
  off, or the server's version will drift from what's installed.
- Licence terms require keeping the credit "Lantern Engine by Awaken".

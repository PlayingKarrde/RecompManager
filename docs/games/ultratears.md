# UltraTears (Tears of the Kingdom)

Repo: https://github.com/kuraeow/ultratears · Recipe: `ultratears` · Researched 2026-11-07
(README and release v0.3.1; Windows zip listed, not run).

## Releases

| Platform key | Asset | Notes |
|---|---|---|
| `windows-x64` | `UltraTears-<version>-windows-x86_64.zip` (~157 MB) | Unpacks to `UltraTears-<version>-windows-x86_64/` with `UltraTears Launcher.exe`, bundled Python and a shader tool |
| `linux-x64` | `UltraTears.AppImage` (~210 MB) | Same name every release. Linux and Steam Deck |

The first release (v0.1.0-beta, a pre-release) was `UltraTears-builder-0.1.0-windows-x86_64.zip`.

## Input

A **folder** with `exefs/` (`main`, `subsdk0`, `sdk`, `main.npdm`, …) and `romfs/` (~16 GB,
~304,000 files) at update 1.4.3. Verification: `exefs/main`, `exefs/main.npdm` and `romfs` exist.

## Building

- Requirements: Windows 10/11 or Linux (kernel ≥ 6.7, glibc ≥ 2.34; SteamOS 3 has both), Vulkan
  1.3, 16 GB RAM, 4 GB VRAM, ~40 GB free disk.
- Windows: `UltraTears Launcher.exe` → choose the dump → **Prepare the game** (~30 min). Choosing
  "For Linux (Steam Deck)" or "Both platforms" also makes `UltraTears-SteamDeck.tar` (~28 GB) to
  copy to the Deck.
- Linux: run the AppImage → choose the dump → Prepare the game. Then add `UltraTears.AppImage`
  from the game folder to Steam with `--play` to skip the launcher.
- Headless: `python tools/setup.py --dump <path>` builds the complete game folder into
  `build/` (from a source checkout). That's the hook for a Phase 3 `server_build`.
- Updates: the launcher finds new versions and rebuilds only what changed. On Linux you can drop
  a newer AppImage into the game folder and the launcher offers the update.

## Saves

`userdata/` in the game folder. Kept across updates; never overwrite it.

## Strategy

- Windows: `client_build`.
- Linux / Steam Deck: `client_build` for now. Phase 3: `server_build` with
  `tools/setup.py --dump`, refusing with a clear error when the server lacks 16 GB RAM or 40 GB disk.

## Server build (Phase 3)

`ultratears-linux` builder: clones the release tag and runs `tools/setup.py --dump <dump>
--output <game> --only <step>` for romfs, lift, compile, shaders, textures and finish. It skips
`graphics`: that step compiles the shader cache with the local GPU driver, so it has to happen
on the Deck. The plan tells the player to press "Prepare the game" once, which then only does
that step. The AppImage is added to the game folder and the folder is packed as a tar. Needs 16
GB RAM and about 100 GB free while building; network on (setup.py downloads ~3 GB of tools).
**Unverified** until run with a real dump.

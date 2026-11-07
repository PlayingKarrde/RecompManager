# doaxvb-re (Dead or Alive Xtreme Beach Volleyball)

Repo: https://github.com/NoRain211/doaxvb-re (the README links to `NoRain211/doaxbv-re`, a
different spelling; the API resolves `doaxvb-re`) · Recipe: `doaxvb-re` · Researched 2026-11-07
(README, `docs/building.md`, `docs/recomp-save-contract.md`, release v0.6.2; zips listed, not run).

## Releases

CLAUDE.md said there is one release zip for all platforms. **That changed:** since 0.6.0 there
are three.

| Platform key | Asset | Notes |
|---|---|---|
| `windows-x64` | `DOAXBV-<version>.zip` (~107 MB) | Bundles Python, Capstone, Git, CMake, `extract-xiso.exe`, `vc_redist`. Unpacks to `DOAXBV-<version>/` with `BuildGame.cmd`, `Launcher.cmd`, `RunGame.cmd` |
| `linux-x64` | `DOAXBV-<version>-Linux.zip` (~1.7 MB) | Scripts and source: `build_game.sh`, `launcher.sh`, `run_game.sh` in `DOAXBV-<version>-Linux/` |
| `macos-universal` | `DOAXBV-<version>-macOS.zip` (~1.7 MB) | Same scripts as Linux |

No release contains a prebuilt runner or game data: the game is always built from the ISO.

## Input

The USA Xbox ISO.

## Building

- **Windows:** `BuildGame.cmd <iso>` (or Launcher → Build). First run downloads llvm-mingw
  (190 MB, ~715 MB unpacked) and Microsoft's DXC (32 MB). No admin rights or Visual Studio.
  Afterwards `DOAXBV.exe` plays with the launcher's saved choices.
- **macOS / Linux:** `./build_game.sh /path/to/game.iso`, then `./launcher.sh` or `./run_game.sh`.
  Linux needs CMake, Ninja, clang (or any C++17 compiler), Python 3 with venv, SDL3's build
  deps and `libshaderc` (Ubuntu 24.04 package list in `docs/building.md`). SteamOS has no
  compiler: build in an Ubuntu 24.04 distrobox; the result runs on SteamOS.
- Upstream advice: build each update into a **new folder** and keep the previous install and
  its saves. Installs are therefore per version.

## Saves

`<disc-root>/.recomp-storage/partition1/UDATA/`, where disc-root is the folder holding the
extracted disc that the runner is pointed at (with a `save-undo-v1/` journal next to it). The
recipe guesses `private/imported-disc` inside the install (**unverified**).

## Strategy

- Windows: `client_build` (`BuildGame.cmd`).
- Linux: `client_build` for now; Phase 3 `server_build` in an Ubuntu 24.04 builder container
  (the documented Linux deps make this feasible).
- macOS: `client_build`.
- Cross-building the Windows runner on Linux with llvm-mingw: plausible (llvm-mingw has Linux
  hosts) but the build scripts are Windows batch files. **Not researched yet.**

## Server build (Phase 3)

`doaxvb-linux` builder: Ubuntu 24.04 with the packages from `docs/building.md`, unzips the
Linux release, runs `./build_game.sh <iso>` and packs the whole folder (the game builds in
place). Network is on (Capstone via pip; SDL3 may be fetched). The Steam Deck uses the
`linux-x64` result. **Unverified** until run with a real ISO; whether `build_game.sh` ever asks
a question on stdin isn't known (it gets `/dev/null`).

# VF2 Recomp (Virtua Fighter 2)

Repo: https://github.com/mr-finster/vf2-recomp · Recipe: `vf2-recomp` · Researched 2026-11-07
(README and release v0.1.18; release archives listed, not run).

## Releases

| Platform key | Asset | Notes |
|---|---|---|
| `windows-x64` | `VirtuaFighter2Recomp-windows.zip` | Unpacks to `VirtuaFighter2/` with `VirtuaFighter2.exe` |
| `macos-universal` | `VirtuaFighter2Recomp-mac.zip` | Apple silicon and Intel. Unpacks to `Virtua Fighter 2 Recomp/Virtua Fighter 2 Recomp.app` |
| `linux-x64`, `linux-x64-steamdeck` | `VirtuaFighter2Recomp-deck.tar.gz` | x86-64 Linux and Steam Deck. Unpacks to `VirtuaFighter2/` with `VirtuaFighter2.sh` and `roms/` |
| `linux-arm64` | `VirtuaFighter2Recomp-linux-arm64.tar.gz` | New since CLAUDE.md was written. Raspberry Pi 5 etc. Layout assumed same as x86-64 (**unverified**) |

GitHub publishes a sha256 digest for every asset; downloads are verified against it.
Releases are frequent (about one every few days in Oct–Nov 2026).

## Input

MAME `vf2` set, Version 2.1, as `vf2.zip`. No checksums are published, so inputs stay
`unverified` unless we add hashes from a known-good set.

## Placing the input and saves

- **Portable mode:** a `portable.txt` next to the program (Windows: beside `VirtuaFighter2.exe`;
  Mac: beside the app) keeps settings, saves, screenshots, `log.txt` and `roms/vf2.zip` in that
  folder.
- **Linux / Steam Deck** keeps everything in its folder already.
- Alternatives: `--roms <folder>`, the `VF2_ROMS` environment variable, or
  `roms_folder = <folder>` in `settings.ini` (command line wins, then env, then ini).
- Non-portable data folders: `%APPDATA%\Virtua Fighter 2 Recomp`,
  `~/Library/Application Support/Virtua Fighter 2 Recomp`, `~/.local/share/Virtua Fighter 2 Recomp`.

## Strategy

`runtime` on every platform. Nothing is built.

## Other

- Steam Deck: add `VirtuaFighter2.sh` as a non-Steam game and leave Proton **off**.
- Unpacking on Windows can lose Linux file permissions; clients should `chmod +x` after extracting.
- Online play needs both players on the same `vf2.zip` and a compatible version (v0.1.16+).
- An experimental Android (arm64) port is built from source in `android/`; no release asset.

# Ship of Harkinian (Ocarina of Time)

Repo: https://github.com/HarbourMasters/Shipwright (default branch `develop`) · Recipe: `shipwright`
· Researched 2026-11-07 (README, `docs/`, release 9.3.1; archives listed, not run).

## Releases

Releases are codenamed (`9.3.1` = "Dewey Bravo"), and asset names carry the codename, so rules
match the end of the name.

| Platform key | Asset (9.3.1) | Notes |
|---|---|---|
| `windows-x64` | `SoH-Dewey-Bravo-Win64.zip` | `soh.exe` and `soh.o2r` at the zip's root (no folder) |
| `macos-universal` | `SoH-Dewey-Bravo-Mac.zip` | Contains **`SoH.dmg`**: clients must mount it and copy `soh.app` |
| `linux-x64` | `SoH-Dewey-Bravo.appimage` | Since 9.3.0. Up to 9.2.x it was `SoH-<codename>-Linux.zip` |

No arm64 release builds. CI nightlies exist for Linux ARM (`soh-linux-aarch64.zip`) but they are
workflow artifacts, not releases. No Android port found in the README.

## Input

One or two ROMs: a normal Ocarina of Time ROM makes `oot.o2r`, a Master Quest ROM makes
`oot-mq.o2r`. The recipe has two input slots: `oot` and optional `oot-mq`. Supported ROMs are
listed by sha1 in `docs/supportedHashes.json` (a JSON list of `{name, sha1}`); the recipe
points `verify.hashes_url` at it so the list is fetched, not hardcoded.

## Placing the input and saves

- **Linux:** "When prompted, select your supported copy of the game." Saves, settings and
  `oot.o2r` go to `~/.local/share/soh/`, **unless** the launch folder already has
  `shipofharkinian.json`, `oot.o2r` or `oot-mq.o2r`, in which case that folder is used.
  `SHIP_HOME` overrides both. The recipe sets `SHIP_HOME={install}` to keep it portable.
- **macOS:** `soh.app` asks for the ROM with a file picker; the `.o2r` goes to
  `~/Library/Application Support/com.shipofharkinian.soh/`.
- **Windows:** launch `soh.exe`. Where it looks for the ROM isn't documented (**unverified**);
  the `.o2r` is written next to `soh.exe`.
- Config: `shipofharkinian.json` next to the executable (Windows/Linux). Mods: `mods/` folder
  with `.otr`/`.o2r` archives.

## Strategy

- Phase 2: `runtime`, placing the ROM (or, better, a generated `.o2r`) next to the executable.
- Phase 3: `server_extract`. The generated `.o2r` works on every platform (the Switch port says
  to generate it on PC). No headless mode is documented; try the AppImage under `xvfb-run` with
  the ROM next to it, or call the extractor (OTRExporter/ZAPD) directly. Not yet researched.

## Open questions

- Windows ROM placement and save location.
- Can `soh.app` find a pre-generated `.o2r` in Application Support without the picker?

## Server extraction (Phase 3)

`OTRGlobals::RunExtract` (soh/soh/OTRGlobals.cpp) takes ROM paths from the command line: on
Linux, with arguments, it goes straight to `ES_EXTRACT_ARGS`, runs `Extractor::RunFileStandalone`
(size/CRC check) and `CallTorch`, and writes `oot.o2r` or `oot-mq.o2r` into the app directory
(`SHIP_HOME`). Only afterwards does it show a "Run SoH?" popup. The `soh-extract` builder uses
that: AppImage under Xvfb, ROM(s) as arguments, wait for the archive(s) to stop growing, stop.
The archives are tied to the SoH version ("incompatible versions of SoH" forces a re-extract),
so the server extracts once per version. **Unverified** until run with a real ROM.

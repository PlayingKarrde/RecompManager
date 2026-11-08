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

`runtime` on Windows and Linux: the ROM goes next to the executable and SoH makes `oot.o2r` on
first launch. On Linux (from the source, `OTRGlobals::RunExtract`): with no `.o2r` it asks
"Generate one now?", then searches its own folder and `SHIP_HOME` for ROMs and asks to process
them, so two clicks and no file picker. macOS is `manual` (the release is a `.dmg`).

## Open questions

- Windows ROM placement and save location.
- Can `soh.app` find a pre-generated `.o2r` in Application Support without the picker?

## Server extraction (tried, dropped 2026-11-07)

`RunExtract` also takes ROM paths on the command line: on Linux it goes straight to
`ES_EXTRACT_ARGS`, runs `Extractor::RunFileStandalone` and `CallTorch`, and writes the archive
into `SHIP_HOME`. A builder ran the AppImage under Xvfb that way. On Unraid it first needed
`libOpenGL.so.0`, then stalled: `RunFileStandalone` only accepts paths ending exactly in
`.z64`/`.n64`/`.v64` that pass its size/CRC check, and answers anything else with a popup that
waits forever without a screen. That was fixable, but each run found a new way to break, to save
one click per device, so SoH went back to `runtime`. The archives are tied to the SoH version
("incompatible versions of SoH" forces a re-extract).

# Xbox ROM Manager

Steam ROM Manager, but for the Xbox PC app. It puts your emulated games into the Xbox app's **My Library**, so they show up in **Xbox mode** (the Xbox full screen experience on Windows handhelds like the ROG Xbox Ally) right next to your other games, with cover art, launching straight into the emulator.

It reads the parsers EmuDeck already sets up, so if you use EmuDeck on Windows it works out of the box: EmuDeck, but Xbox.

## Features

- **Uses your EmuDeck setup.** Reads EmuDeck's Steam ROM Manager parsers directly, so every system EmuDeck supports works, with games launched through EmuDeck's own launcher scripts. Parsers for emulators you don't have installed are skipped.
- **Your own systems too.** Add any emulator and ROM folder in `config.toml`, no EmuDeck needed.
- **Cover art.** From SteamGridDB (free API key) and Steam. Each game is looked up once.
- **Art for games you added by hand.** Optionally fills in art for games you added to the Xbox app yourself, only when the title matches exactly.
- **Safe to re-run.** Adds new ROMs, updates changed ones and removes games whose ROM is gone. It only ever touches entries it added, keeps the last 10 backups of your library, and has a `--dry-run` mode.
- **Fixes EmuDeck's broken PS3 parser.** Installed PSN games (PKG) are read from each game's PARAM.SFO, so they get real names, and disc-game updates and DLC folders no longer show up as games.
- **No GUI.** A plain command-line tool with plain text output, so it works well with screen readers and in scripts.

## Requirements

- Windows 10 or 11 with the Xbox app, recent enough to have "Add games to library" (rolled out from March 2026).
- Python 3.11 or newer. No extra packages needed.
- Optional: [EmuDeck for Windows](https://www.emudeck.com/).
- Optional: a free [SteamGridDB](https://www.steamgriddb.com/) API key for ROM cover art.

## Setup

1. Download or clone this repo.
2. Run it once to create your config: `python xbox_rom_manager.py --dry-run`
3. Open `config.toml` in a text editor:
   - If EmuDeck isn't installed in the default place, fix the `userdata` path. The default is `C:\Emulation\tools\userData`.
   - Paste your SteamGridDB key into `steamgriddb_api_key`. To get one, make an account on steamgriddb.com, then go to Preferences, then API.
   - Optionally list parsers to skip in `exclude_parsers`. For example, `"Emulators - Emulators"` stops it adding each emulator itself as a library entry.
   - Optionally add your own `[[system]]` sections. The file explains each setting.
4. Run `python xbox_rom_manager.py`

## Usage

```
python xbox_rom_manager.py              same as sync
python xbox_rom_manager.py sync         add new ROMs, update changed ones, remove missing ones, fetch art
python xbox_rom_manager.py --dry-run    show what would change without writing anything
python xbox_rom_manager.py list         list the games this tool manages
python xbox_rom_manager.py remove-all   remove every game this tool added (your own stay)
python xbox_rom_manager.py --no-reopen  don't reopen the Xbox app afterwards
```

When it changes your library, it closes the Xbox app, writes the changes, and reopens it. Run it again whenever you add ROMs, or schedule it with Task Scheduler.

## How it works, and what we found out

Nothing below is officially documented. It was worked out by testing against the Xbox app (Insider build 2610.1001.10.0). Corrections welcome.

Games added through "Add games to library" are stored in a plain JSON file:

```
%LOCALAPPDATA%\Packages\Microsoft.GamingApp_8wekyb3d8bbwe\LocalState\ThirdPartyLibraries\CustomLibraryManagement\CustomLibraryManagement.manifest
```

```json
{
  "version": 1,
  "provider": { "version": 1, "enabled": true },
  "gameCache": {
    "version": 1,
    "{a-random-guid}": {
      "id": "{a-random-guid}",
      "addedDate": "1791602916600",
      "title": "New Super Mario Bros. Wii",
      "installLocation": "C:\\Path\\To\\Emulator\\",
      "executableName": "Dolphin.exe",
      "executableCommandArgs": "-b -e \"C:\\Emulation\\roms\\wii\\game.wbfs\"",
      "imagePath": "C:\\Path\\To\\cover.jpg"
    }
  }
}
```

Gotchas:

- **Every entry must have `imagePath`.** If any entry is missing it, the Xbox app silently ignores the entire custom library, including games you added by hand. An empty string is fine.
- **Write it only while the Xbox app is closed.** The app keeps the list in memory and saves over the file.
- **In Xbox mode, Windows restarts the app within about a second of it closing**, so it can load the old file before you've written the new one. Restart it again after writing.
- `addedDate` and `lastPlayedDate` are milliseconds since 1970, stored as strings. The app adds `lastPlayedDate` itself.
- `imagePath` can point anywhere. This tool copies art into the app's own `LocalState\CustomLibraryManagement\Images` folder, the same way the app does.
- `msxbox://library` opens My Library directly.

## Credits

- [Steam ROM Manager](https://github.com/SteamGridDB/steam-rom-manager) and [EmuDeck](https://github.com/dragoonDorise/EmuDeck): this tool reads EmuDeck's SRM parser configs.
- [SteamGridDB](https://www.steamgriddb.com/) for cover art.
- [XboxGridSync](https://github.com/kllakm/XboxGridSync), another project that works with the same Xbox app files.

Not affiliated with Microsoft, Xbox, Valve, EmuDeck or SteamGridDB.

## License

MIT

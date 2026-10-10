"""Xbox ROM Manager: adds emulated games to the Xbox PC app library.

Finds ROMs using EmuDeck's Steam ROM Manager parsers and/or systems listed in
config.toml, and writes one entry per ROM into the Xbox app's custom library
manifest, the same file its "Add games to library" feature uses. Entries this
tool added are tracked in state.json, so re-running only touches its own
entries. It can also fetch cover art, including for games added by hand.

Usage:
    python xbox_rom_manager.py              same as "sync"
    python xbox_rom_manager.py sync         add new ROMs, update changed ones, remove missing ones, fetch art
    python xbox_rom_manager.py sync --dry-run   show what would change, write nothing
    python xbox_rom_manager.py list         list games this tool has added
    python xbox_rom_manager.py remove-all   remove every game this tool has added
"""

import argparse
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import time
import tomllib
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "config.toml"
STATE_PATH = HERE / "state.json"
BACKUP_DIR = HERE / "backups"
BACKUPS_TO_KEEP = 10

LOCAL_STATE = Path(os.environ["LOCALAPPDATA"]) / "Packages" / "Microsoft.GamingApp_8wekyb3d8bbwe" / "LocalState"
MANIFEST_PATH = LOCAL_STATE / "ThirdPartyLibraries" / "CustomLibraryManagement" / "CustomLibraryManagement.manifest"
IMAGES_DIR = LOCAL_STATE / "CustomLibraryManagement" / "Images"

XBOX_PROCESSES = ["XboxPcApp.exe", "XboxPcAppFT.exe"]
XBOX_APP_ID = r"shell:AppsFolder\Microsoft.GamingApp_8wekyb3d8bbwe!Microsoft.Xbox.App"
ART_EXTENSIONS = [".png", ".jpg", ".jpeg"]
TAG_PATTERN = re.compile(r"\s*[\(\[][^\)\]]*[\)\]]")
USER_AGENT = "xbox-rom-manager/1.0"
EMUDECK_PLACEHOLDERS = {"systeminfo.txt", "metadata.txt"}
# EmuDeck parsers that add emulators and frontends rather than games.
LAUNCHER_PARSERS = {"Emulators - Emulators", "ESDE - EmulationStationDE", "Pegasus - Pegasus Frontend"}


# ---------- config and files ----------

def load_config():
    if not CONFIG_PATH.exists():
        example = HERE / "config.example.toml"
        if not example.exists():
            sys.exit(f"Config file not found: {CONFIG_PATH}")
        shutil.copyfile(example, CONFIG_PATH)
        print(f"Created {CONFIG_PATH.name} from the example. Edit it to add a SteamGridDB key or extra systems.")
    with open(CONFIG_PATH, "rb") as f:
        config = tomllib.load(f)
    for s in config.get("system", []):
        for key in ("name", "rom_folder", "extensions", "emulator", "arguments"):
            if key not in s:
                sys.exit(f"System {s.get('name', '(unnamed)')} is missing '{key}'.")
    return config


def load_json(path, default):
    try:
        text = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return default
    return json.loads(text) if text else default


def empty_manifest():
    return {"version": 1, "provider": {"version": 1, "enabled": True}, "gameCache": {"version": 1}}


def load_state():
    state = load_json(STATE_PATH, {})
    state.setdefault("entries", {})  # game id -> rom path
    state.setdefault("art", {})  # game id -> "done" or the art sources already tried
    return state


def clean_title(name):
    title = TAG_PATTERN.sub("", name).strip()
    return title or name


def launch_fields(exe, arguments):
    exe = Path(exe)
    return {
        "installLocation": str(exe.parent) + "\\",
        "executableName": exe.name,
        "executableCommandArgs": arguments,
    }


# ---------- own systems from config.toml ----------

def find_local_art(system, rom_path):
    folder = system.get("art_folder")
    if not folder:
        return None
    for ext in ART_EXTENSIONS:
        candidate = Path(folder) / (rom_path.stem + ext)
        if candidate.exists():
            return candidate
    return None


def scan_systems(systems, found):
    for system in systems:
        folder = Path(system["rom_folder"])
        if not folder.is_dir():
            print(f"Warning: ROM folder for {system['name']} not found: {folder}")
            continue
        extensions = {"." + e.lower().lstrip(".") for e in system["extensions"]}
        files = folder.rglob("*") if system.get("recursive") else folder.iterdir()
        for rom in sorted(files):
            if not rom.is_file() or rom.suffix.lower() not in extensions or str(rom) in found:
                continue
            found[str(rom)] = {
                "system": system["name"],
                "title": clean_title(rom.stem),
                "art": find_local_art(system, rom),
                **launch_fields(system["emulator"], system["arguments"].replace("{rom}", str(rom))),
            }


# ---------- EmuDeck / Steam ROM Manager parsers ----------

def split_top(text, sep):
    """Split on sep, ignoring separators inside ${...} or (...) groups."""
    parts, depth, start, i = [], 0, 0, 0
    while i < len(text):
        if text.startswith("${", i):
            depth += 1
            i += 2
            continue
        c = text[i]
        if c in "({":
            depth += 1
        elif c in ")}":
            depth -= 1
        elif c == sep and depth == 0:
            parts.append(text[start:i])
            start = i + 1
        i += 1
    parts.append(text[start:])
    return parts


def find_close(text, start, open_char, close_char):
    depth = 0
    for i in range(start, len(text)):
        if text[i] == open_char:
            depth += 1
        elif text[i] == close_char:
            depth -= 1
            if depth == 0:
                return i
    raise ValueError(f"Unbalanced '{open_char}' in: {text}")


def expand(template, variables):
    """Expand Steam ROM Manager ${...} variables (the subset EmuDeck uses)."""
    out, i = [], 0
    literals = (("${{}", "{"), ("${\\}}", "}"), ("${/}", "\\"))
    while i < len(template):
        if not template.startswith("${", i):
            out.append(template[i])
            i += 1
            continue
        literal = next((l for l in literals if template.startswith(l[0], i)), None)
        if literal:
            out.append(literal[1])
            i += len(literal[0])
            continue
        depth, j = 0, i
        while j < len(template):
            if template.startswith("${", j):
                depth += 1
                j += 2
                continue
            if template[j] == "}":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        inner = template[i + 2:j]
        if inner.startswith("os:"):
            branches = split_top(inner[3:], "|")
            chosen = branches[1] if branches[0] == "win" else (branches[2] if len(branches) > 2 else "")
            out.append(expand(chosen, variables))
        elif inner.lower() in variables:
            out.append(variables[inner.lower()])
        else:
            raise KeyError(inner)
        i = j + 1
    return "".join(out)


def glob_to_regex(glob, state=None):
    """Convert a Steam ROM Manager glob into a regex over '/'-separated relative paths."""
    state = state if state is not None else {"title": False}
    out, i = [], 0
    while i < len(glob):
        if glob.startswith("${title}", i):
            out.append("[^/]+?" if state["title"] else "(?P<title>[^/]+?)")
            state["title"] = True
            i += len("${title}")
        elif glob.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif glob.startswith("**", i):
            out.append(".*")
            i += 2
        elif glob[i] in "@!" and glob[i + 1:i + 2] == "(":
            close = find_close(glob, i + 1, "(", ")")
            # Empty alternatives (a stray "|)" in some EmuDeck parsers) would match any file.
            alts = "|".join(glob_to_regex(a, state) for a in split_top(glob[i + 2:close], "|") if a)
            out.append(f"(?:{alts})" if glob[i] == "@" else f"(?!(?:{alts})(?:/|$))[^/]*")
            i = close + 1
        elif glob[i] == "{":
            close = find_close(glob, i, "{", "}")
            alts = "|".join(glob_to_regex(a, state) for a in split_top(glob[i + 1:close], ","))
            out.append(f"(?:{alts})")
            i = close + 1
        else:
            out.append({"*": "[^/]*", "?": "[^/]"}.get(glob[i], re.escape(glob[i])))
            i += 1
    return "".join(out)


def emudeck_variables(userdata):
    settings = load_json(userdata / "userSettings.json", {})
    env = settings.get("environmentVariables", {})
    return {
        "romsdirglobal": env.get("romsDirectory", ""),
        "steamdirglobal": env.get("steamDirectory", ""),
        "retroarchpath": env.get("retroarchPath", ""),
        "racores": env.get("raCoresDirectory", ""),
        "localimagesdirglobal": env.get("localImagesDirectory", ""),
    }


def read_param_sfo(path):
    """Read a PS3 PARAM.SFO file into a dict of its fields."""
    data = path.read_bytes()
    if data[:4] != b"\0PSF":
        return {}
    key_start, data_start, count = struct.unpack_from("<III", data, 8)
    fields = {}
    for n in range(count):
        key_offset, fmt, length, _, data_offset = struct.unpack_from("<HHIII", data, 20 + n * 16)
        key_end = data.index(b"\0", key_start + key_offset)
        key = data[key_start + key_offset:key_end].decode("utf-8", "replace")
        raw = data[data_start + data_offset:data_start + data_offset + length]
        fields[key] = struct.unpack("<I", raw[:4])[0] if fmt == 0x0404 else raw.rstrip(b"\0").decode("utf-8", "replace")
    return fields


def find_ps3_installed(rom_dir):
    """Installed PSN games in RPCS3's dev_hdd0/game folder.

    EmuDeck's own parser for this matches every folder starting with N or P and
    titles games by their ID, so disc-game updates and DLC show up and names are
    codes. Instead, read each folder's PARAM.SFO and keep only HDD games (HG).
    """
    for folder in sorted(p for p in rom_dir.iterdir() if p.is_dir()):
        eboot, sfo = folder / "USRDIR" / "EBOOT.BIN", folder / "PARAM.SFO"
        if not (eboot.exists() and sfo.exists()):
            continue
        try:
            info = read_param_sfo(sfo)
        except (OSError, struct.error, ValueError):
            continue
        if info.get("CATEGORY") != "HG":
            continue
        title = str(info.get("TITLE") or folder.name)
        title = " ".join(title.replace("\u2122", "").replace("\u00ae", "").split())
        icon = folder / "ICON0.PNG"
        yield eboot, title, icon if icon.exists() else None


def find_glob_matches(parser, rom_dir, walk_cache):
    pattern = re.compile(glob_to_regex(parser["parserInputs"]["glob"]), re.IGNORECASE)
    if rom_dir not in walk_cache:
        walk_cache[rom_dir] = [Path(root) / f for root, _, files in os.walk(rom_dir) for f in files]
    for rom in sorted(walk_cache[rom_dir]):
        match = pattern.fullmatch(rom.relative_to(rom_dir).as_posix())
        if match and rom.name.lower() not in EMUDECK_PLACEHOLDERS:
            yield rom, match.groupdict().get("title") or rom.stem, None


def is_ps3_installed_parser(parser, rom_dir):
    return parser.get("parserType") == "Glob-regex" and tuple(p.lower() for p in rom_dir.parts[-2:]) == ("dev_hdd0", "game")


def scan_emudeck(emudeck, found):
    userdata = Path(emudeck.get("userdata", r"C:\Emulation\tools\userData"))
    parsers_path = userdata / "userConfigurations.json"
    if not parsers_path.exists():
        print(f"Warning: EmuDeck parsers not found: {parsers_path}")
        return
    excluded = {p.lower() for p in emudeck.get("exclude_parsers", [])}
    global_vars = emudeck_variables(userdata)
    walk_cache = {}
    used = 0

    for parser in load_json(parsers_path, []):
        name = parser.get("configTitle", "(unnamed)")
        if parser.get("disabled") or name.lower() in excluded:
            continue
        # Parsers for emulators that aren't installed point at launcher scripts that don't exist.
        launcher = re.search(r"[A-Za-z]:\\[^'\"]+?\.ps1", parser.get("executableArgs", ""))
        if launcher and not Path(launcher.group(0)).exists():
            continue
        try:
            rom_dir = Path(expand(parser["romDirectory"], global_vars))
            exe = expand(parser["executable"]["path"], global_vars)
            if is_ps3_installed_parser(parser, rom_dir):
                matches = list(find_ps3_installed(rom_dir)) if rom_dir.is_dir() else []
            elif parser.get("parserType") == "Glob":
                matches = list(find_glob_matches(parser, rom_dir, walk_cache)) if rom_dir.is_dir() else []
            else:
                print(f"Skipping parser '{name}': its type ({parser.get('parserType')}) isn't supported yet.")
                continue
        except (KeyError, ValueError) as e:
            print(f"Skipping parser '{name}': couldn't read it ({e}).")
            continue
        used += 1

        for rom, raw_title, fallback_art in matches:
            if str(rom) in found:
                continue
            variables = {
                **global_vars,
                "title": raw_title,
                "fuzzytitle": clean_title(raw_title),
                "finaltitle": clean_title(raw_title),
                "filepath": str(rom),
                "filename": rom.name,
                "filedir": str(rom.parent),
                "exepath": exe,
                "exedir": str(Path(exe).parent),
            }
            try:
                args = expand(parser.get("executableArgs", ""), variables)
                title = expand(parser.get("titleModifier", "${fuzzyTitle}"), variables)
            except KeyError as e:
                print(f"Skipping parser '{name}': unknown variable ${{{e.args[0]}}}.")
                break
            found[str(rom)] = {"system": name, "title": title, "art": None, "fallback_art": fallback_art,
                               **launch_fields(exe, args)}

    print(f"Read {used} EmuDeck parsers for installed emulators.")


# ---------- art ----------

def fetch(url, headers=None):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
    with urllib.request.urlopen(request, timeout=20) as response:
        return response.read()


def normalise(title):
    return re.sub(r"[^a-z0-9]", "", title.lower())


def steam_art(title):
    term = urllib.parse.quote(title)
    results = json.loads(fetch(f"https://store.steampowered.com/api/storesearch/?term={term}&l=english&cc=US"))
    for item in results.get("items", []):
        if normalise(item["name"]) != normalise(title):
            continue
        base = "https://shared.akamai.steamstatic.com/store_item_assets/steam/apps"
        candidates = [f"{base}/{item['id']}/library_600x900_2x.jpg", f"{base}/{item['id']}/library_600x900.jpg"]
        tiny = item.get("tiny_image", "").split("?")[0]
        if tiny:
            candidates.append(tiny.rsplit("/", 1)[0] + "/library_600x900.jpg")
        for url in candidates:
            try:
                return fetch(url), ".jpg"
            except OSError:
                pass
    return None


def steamgriddb_art(title, key, strict):
    """Search SteamGridDB. Strict lookups need an exact title match; otherwise the best match is used."""
    headers = {"Authorization": f"Bearer {key}"}
    api = "https://www.steamgriddb.com/api/v2"
    games = json.loads(fetch(f"{api}/search/autocomplete/{urllib.parse.quote(title)}", headers)).get("data", [])
    exact = [g for g in games if normalise(g["name"]) == normalise(title)]
    game = (exact or ([] if strict else games) or [None])[0]
    if not game:
        return None
    grids = json.loads(fetch(f"{api}/grids/game/{game['id']}?dimensions=600x900", headers)).get("data", [])
    if not grids:
        return None
    url = grids[0]["url"]
    return fetch(url), Path(urllib.parse.urlparse(url).path).suffix.lower() or ".png"


def find_online_art(title, sources, key, strict):
    for source in sources:
        try:
            result = steamgriddb_art(title, key, strict) if source == "steamgriddb" else steam_art(title)
        except (OSError, ValueError, KeyError) as e:
            print(f"Art lookup on {source} failed for {title}: {e}")
            result = None
        time.sleep(0.3)
        if result:
            return result
    return None


def save_art(data, suffix, game_id):
    IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    target = IMAGES_DIR / f"xrm-{game_id.strip('{}')}{suffix}"
    for old in IMAGES_DIR.glob(f"xrm-{game_id.strip('{}')}.*"):
        old.unlink()
    target.write_bytes(data)
    return str(target)


def remove_art(entry, dry_run):
    image = entry.get("imagePath", "")
    if Path(image).name.startswith("xrm-") and not dry_run:
        Path(image).unlink(missing_ok=True)


def update_art(games, state, art_config, fallbacks, rom_ids, dry_run):
    """Fetch cover art once per game: ours always, hand-added ones if enabled.

    fallbacks maps game ids to a local image used when nothing is found online.
    rom_ids are games whose titles come from ROMs, where SteamGridDB's closest match
    is trusted; everything else (hand-added games, emulator launchers) must match exactly.
    """
    if not art_config.get("fetch", True):
        return 0
    key = art_config.get("steamgriddb_api_key", "").strip()
    sources = (["steamgriddb"] if key else []) + ["steam"]
    tried_label = "+".join(sources)
    ours = state["entries"]
    manual = art_config.get("manual_games", True)
    added = 0
    for game_id, entry in games.items():
        if not isinstance(entry, dict) or "title" not in entry:
            continue
        if game_id not in ours and not manual:
            continue
        status = state["art"].get(game_id)
        image = entry.get("imagePath", "")
        has_own_art = Path(image).name.startswith("xrm-") and Path(image).exists()
        if status == "done" and (has_own_art or game_id not in ours):
            continue
        if status == tried_label:
            continue
        if dry_run:
            print(f"Would look up art: {entry['title']}")
            continue
        result = find_online_art(clean_title(entry["title"]), sources, key, strict=game_id not in rom_ids)
        if result:
            entry["imagePath"] = save_art(*result, game_id)
            state["art"][game_id] = "done"
            print(f"Art added: {entry['title']}")
            added += 1
        else:
            state["art"][game_id] = tried_label
            fallback = fallbacks.get(game_id)
            if fallback:
                entry["imagePath"] = save_art(fallback.read_bytes(), fallback.suffix.lower(), game_id)
                print(f"No art found online, using the game's own icon: {entry['title']}")
                added += 1
            else:
                print(f"No art found: {entry['title']}")
    return added


# ---------- Xbox app ----------

def running_xbox_processes():
    out = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True, text=True).stdout.lower()
    return [p for p in XBOX_PROCESSES if f'"{p.lower()}"' in out]


def close_xbox_app():
    running = running_xbox_processes()
    if running:
        print("Closing the Xbox app so it doesn't overwrite the changes.")
        args = ["taskkill", "/F"]
        for p in running:
            args += ["/IM", p]
        subprocess.run(args, capture_output=True)
        time.sleep(2)
    return bool(running)


def start_xbox_app():
    print("Reopening the Xbox app.")
    subprocess.run(["explorer.exe", XBOX_APP_ID])


def backup_manifest():
    if not MANIFEST_PATH.exists():
        return
    BACKUP_DIR.mkdir(exist_ok=True)
    stamp = time.strftime("%Y-%m-%d_%H-%M-%S")
    shutil.copyfile(MANIFEST_PATH, BACKUP_DIR / f"CustomLibraryManagement_{stamp}.manifest")
    for old in sorted(BACKUP_DIR.glob("*.manifest"))[:-BACKUPS_TO_KEEP]:
        old.unlink()


def write_json(path, data, compact):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    text = json.dumps(data, separators=(",", ":"), ensure_ascii=False) if compact else json.dumps(data, indent=2)
    tmp.write_text(text, encoding="utf-8", newline="")
    os.replace(tmp, path)


def save(manifest, state, dry_run, reopen, manifest_changed):
    if dry_run:
        print("Dry run: nothing was written.")
        return
    if manifest_changed:
        was_running = close_xbox_app()
        backup_manifest()
        write_json(MANIFEST_PATH, manifest, compact=True)
        # In Xbox full screen mode Windows relaunches the app within a second of it
        # closing, so it may have already loaded the old library. Close it once more
        # so the relaunch reads the new one.
        if running_xbox_processes():
            print("The Xbox app restarted before the library was written; restarting it again to load the changes.")
            close_xbox_app()
    write_json(STATE_PATH, state, compact=False)
    if manifest_changed and was_running and reopen and not running_xbox_processes():
        start_xbox_app()


# ---------- commands ----------

def sync(args):
    config = load_config()
    manifest = load_json(MANIFEST_PATH, empty_manifest())
    original = json.dumps(manifest)
    games = manifest.setdefault("gameCache", {"version": 1})
    state = load_state()
    ours = state["entries"]

    desired = {}
    emudeck = config.get("emudeck", {})
    if emudeck.get("enabled"):
        scan_emudeck(emudeck, desired)
    scan_systems(config.get("system", []), desired)
    added = updated = removed = 0

    # Forget entries the user deleted in the Xbox app; they get re-added below.
    for game_id in [g for g in ours if g not in games]:
        del ours[game_id]
    for game_id in [g for g in state["art"] if g not in games]:
        del state["art"][game_id]

    rom_to_id = {rom: game_id for game_id, rom in ours.items()}

    for game_id, rom in list(ours.items()):
        if rom not in desired:
            print(f"Removed: {games[game_id].get('title', rom)} (ROM no longer found)")
            remove_art(games[game_id], args.dry_run)
            del games[game_id]
            del ours[game_id]
            state["art"].pop(game_id, None)
            removed += 1

    for rom, want in desired.items():
        game_id = rom_to_id.get(rom)
        if game_id is None:
            # Adopt a matching entry added by hand, so it isn't duplicated.
            game_id = next((g for g, e in games.items() if isinstance(e, dict) and g not in ours
                            and all(str(e.get(k, "")).lower() == want[k].lower()
                                    for k in ("installLocation", "executableName", "executableCommandArgs"))), None)
        is_new = game_id is None
        if is_new:
            game_id = "{" + str(uuid.uuid4()) + "}"
            games[game_id] = {"id": game_id, "addedDate": str(int(time.time() * 1000))}
        entry = games[game_id]
        before = dict(entry)
        for k in ("title", "installLocation", "executableName", "executableCommandArgs"):
            entry[k] = want[k]
        # The Xbox app ignores the WHOLE custom library (hand-added games too) if
        # any entry lacks imagePath, so always include it, empty if there's no art yet.
        entry.setdefault("imagePath", "")
        if want["art"] and not args.dry_run:
            art = want["art"]
            entry["imagePath"] = save_art(art.read_bytes(), art.suffix.lower(), game_id)
            state["art"][game_id] = "done"
        ours[game_id] = rom
        if is_new:
            print(f"Added: {want['title']} ({want['system']})")
            added += 1
        elif entry != before:
            print(f"Updated: {want['title']} ({want['system']})")
            updated += 1

    for entry in games.values():
        if isinstance(entry, dict):
            entry.setdefault("imagePath", "")

    fallbacks = {g: desired[rom]["fallback_art"] for g, rom in ours.items() if desired[rom].get("fallback_art")}
    rom_ids = {g for g, rom in ours.items() if desired[rom]["system"] not in LAUNCHER_PARSERS}
    art_added = update_art(games, state, config.get("art", {}), fallbacks, rom_ids, args.dry_run)

    print(f"Done. {added} added, {updated} updated, {removed} removed, art found for {art_added}. "
          f"{len(ours)} games managed in total.")
    save(manifest, state, args.dry_run, not args.no_reopen, json.dumps(manifest) != original)


def list_games(args):
    manifest = load_json(MANIFEST_PATH, empty_manifest())
    games = manifest.get("gameCache", {})
    ours = load_state()["entries"]
    if not ours:
        print("This tool hasn't added any games yet.")
        return
    for game_id, rom in ours.items():
        title = games.get(game_id, {}).get("title", "(missing from Xbox app)")
        print(f"{title}: {rom}")
    print(f"{len(ours)} games.")


def remove_all(args):
    manifest = load_json(MANIFEST_PATH, empty_manifest())
    games = manifest.get("gameCache", {})
    state = load_state()
    for game_id in state["entries"]:
        if game_id in games:
            print(f"Removed: {games[game_id].get('title', game_id)}")
            remove_art(games[game_id], args.dry_run)
            del games[game_id]
        state["art"].pop(game_id, None)
    print(f"Done. {len(state['entries'])} games removed.")
    changed = bool(state["entries"])
    state["entries"] = {}
    save(manifest, state, args.dry_run, not args.no_reopen, changed)


def main():
    parser = argparse.ArgumentParser(description="Add emulated games to the Xbox PC app library.")
    parser.add_argument("command", nargs="?", default="sync", choices=["sync", "list", "remove-all"])
    parser.add_argument("--dry-run", action="store_true", help="show what would change without writing anything")
    parser.add_argument("--no-reopen", action="store_true", help="don't reopen the Xbox app afterwards")
    args = parser.parse_args()
    {"sync": sync, "list": list_games, "remove-all": remove_all}[args.command](args)


if __name__ == "__main__":
    main()

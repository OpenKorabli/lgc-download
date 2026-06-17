"""CLI entry point for lgc-download."""

import argparse
import fnmatch
import os
import sys
import urllib.request

from .api import USER_AGENT, fetch_showroom, get_manifest, resolve_game
from .remote7z import RemoteFile, decompress_entry, parse_archive_index


def fmt_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    for unit in ("KB", "MB", "GB"):
        n /= 1024
        if n < 1024:
            return f"{n:.1f} {unit}"
    return f"{n:.1f} TB"


def _get_game_and_manifest(args):
    game = resolve_game(args.game)
    manifest = get_manifest(game["api_base"], game["app_id"])
    return game, manifest


# ── Commands ──────────────────────────────────────────────────────────────────

def cmd_games(args):
    games = fetch_showroom()
    print("Available games:\n")
    for g in games:
        print(f"  {g['app_id']:<25} {g['game_name']:<25} {g['region_name']}")


def cmd_list(args):
    game, manifest = _get_game_and_manifest(args)

    if args.files:
        part_name = args.files
        part = manifest["patches"].get(part_name)
        if not part:
            print(f"Error: part '{part_name}' not found.", file=sys.stderr)
            print(f"Available parts: {', '.join(manifest['patches'].keys())}", file=sys.stderr)
            sys.exit(1)

        print(f"Part: {part_name}")
        print(f"Version: {part['versionFrom']} -> {part['versionTo']}")
        print(f"Files ({len(part['files'])}):\n")
        for f in part["files"]:
            print(f"  {f['basename']:<50} {fmt_size(f['size']):>10}  (unpacked: {fmt_size(f['unpackedSize'])})")
        return

    print(f"Game:              {game['app_id']}  ({game['game_name']} — {game['region_name']})")
    print(f"Latest version:    {manifest['latestVersion']}")
    print(f"Metadata version:  {manifest['metadataVersion']}")
    print(f"Chain ID:          {manifest['chainId']}")
    print(f"\nParts ({len(manifest['patches'])}):\n")

    for name, part in manifest["patches"].items():
        total_size = sum(f["size"] for f in part["files"])
        print(f"  {name:<20} {len(part['files']):>3} file(s)   {fmt_size(total_size):>10}   [{part['versionFrom']} -> {part['versionTo']}]")

    if not args.parts:
        print(f"\nUse 'list {game['app_id']} --files <part>' to see individual files.")


def cmd_download(args):
    game, manifest = _get_game_and_manifest(args)

    part_name = args.part
    part = manifest["patches"].get(part_name)
    if not part:
        print(f"Error: part '{part_name}' not found.", file=sys.stderr)
        print(f"Available parts: {', '.join(manifest['patches'].keys())}", file=sys.stderr)
        sys.exit(1)

    if args.all:
        out_dir = args.dir or "."
        os.makedirs(out_dir, exist_ok=True)
        for f in part["files"]:
            target = os.path.join(out_dir, f["basename"])
            _download_file(f, target)
    else:
        if not args.filename:
            print("Error: specify a FILENAME or use --all", file=sys.stderr)
            sys.exit(1)

        match = None
        for f in part["files"]:
            if f["basename"] == args.filename or f["name"] == args.filename:
                match = f
                break
        if not match:
            print(f"Error: file '{args.filename}' not found in part '{part_name}'.", file=sys.stderr)
            print("Available files:", file=sys.stderr)
            for f in part["files"]:
                print(f"  {f['basename']}", file=sys.stderr)
            sys.exit(1)

        target = args.output or match["basename"]
        _download_file(match, target)


def _download_file(file_info: dict, target: str):
    url = file_info["downloadUrl"]
    if not url:
        print(f"  SKIP {file_info['basename']} (no download URL)", file=sys.stderr)
        return

    expected_size = file_info["size"]
    basename = file_info["basename"]

    if os.path.isfile(target):
        actual_size = os.path.getsize(target)
        if actual_size == expected_size:
            print(f"  SKIP {basename} (already downloaded, {fmt_size(expected_size)})")
            return

    print(f"  GET  {basename} ({fmt_size(expected_size)}) ...")

    tmp = target + ".part"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=120) as resp:
            with open(tmp, "wb") as fp:
                downloaded = 0
                while True:
                    chunk = resp.read(1024 * 1024)
                    if not chunk:
                        break
                    fp.write(chunk)
                    downloaded += len(chunk)
                    pct = (downloaded / expected_size * 100) if expected_size else 0
                    print(f"\r  GET  {basename}  {fmt_size(downloaded)} / {fmt_size(expected_size)}  ({pct:.0f}%)", end="", flush=True)
        print()
        os.replace(tmp, target)
        print(f"  OK   {basename} -> {target}")
    except Exception as e:
        print(f"\n  FAIL {basename}: {e}", file=sys.stderr)
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def cmd_extract(args):
    game, manifest = _get_game_and_manifest(args)

    part_name = args.part
    part = manifest["patches"].get(part_name)
    if not part:
        print(f"Error: part '{part_name}' not found.", file=sys.stderr)
        print(f"Available parts: {', '.join(manifest['patches'].keys())}", file=sys.stderr)
        sys.exit(1)

    if not part["files"]:
        print(f"Error: no files in part '{part_name}'.", file=sys.stderr)
        sys.exit(1)
    match = part["files"][0]

    url = match["downloadUrl"]
    if not url:
        print("Error: no download URL for this file.", file=sys.stderr)
        sys.exit(1)

    print(f"Archive: {match['basename']} ({fmt_size(match['size'])})")
    print(f"URL: {url}")
    print(f"Reading archive index via range requests...")

    rf = RemoteFile(url)
    entries = parse_archive_index(rf)
    files_only = [e for e in entries if not e["is_dir"]]

    print(f"Index: {len(files_only)} files ({rf._requests} HTTP requests, ~{rf._requests * 256}KB transferred)\n")

    if args.list:
        for e in files_only:
            print(f"  {e['filename']:<70} {fmt_size(e['uncompressed_size']):>10}  (compressed: {fmt_size(e['compressed_size'])})")
        print(f"\n{len(files_only)} files total")
        return

    targets = args.paths or []
    filt = args.filter

    to_extract = []
    for e in files_only:
        if filt:
            if fnmatch.fnmatch(e["filename"], filt) or fnmatch.fnmatch(e["filename"], f"*/{filt}"):
                to_extract.append(e)
        elif targets:
            for t in targets:
                if e["filename"] == t or e["filename"].endswith("/" + t) or fnmatch.fnmatch(e["filename"], t):
                    to_extract.append(e)
                    break
        else:
            to_extract.append(e)

    if not to_extract:
        print("No files matched. Use --list to see available files.", file=sys.stderr)
        sys.exit(1)

    total_compressed = sum(e["compressed_size"] for e in to_extract)
    total_uncompressed = sum(e["uncompressed_size"] for e in to_extract)
    print(f"Extracting {len(to_extract)} file(s): {fmt_size(total_compressed)} to download, {fmt_size(total_uncompressed)} uncompressed\n")

    out_dir = args.dir or "."
    extracted = 0

    for e in to_extract:
        rel_path = e["filename"]
        dest = os.path.join(out_dir, rel_path.replace("/", os.sep))

        if os.path.isfile(dest) and os.path.getsize(dest) == e["uncompressed_size"]:
            print(f"  SKIP {rel_path}")
            continue

        print(f"  GET  {rel_path} ({fmt_size(e['compressed_size'])}) ...", end="", flush=True)
        compressed = rf.fetch_range(e["offset"], e["compressed_size"])
        decompressed = decompress_entry(compressed, e["filters"], e["uncompressed_size"])

        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "wb") as fp:
            fp.write(decompressed)
        extracted += 1
        print(f" -> {fmt_size(len(decompressed))}")

    print(f"\nDone: {extracted} file(s) extracted to {out_dir}")
    print(f"Total HTTP requests: {rf._requests}")


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        prog="lgc-download",
        description="LGC Download Tool — list versions and download game files from Lesta Game Center API",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # games
    sub.add_parser("games", help="List all available games")

    # list
    p_list = sub.add_parser("list", help="List available versions, parts, and files")
    p_list.add_argument("game", help="Game ID (e.g. WOWS.WW.PRODUCTION, WOT.EU.PRODUCTION)")
    p_list.add_argument("--parts", action="store_true", help="Show all parts (default overview already includes them)")
    p_list.add_argument("--files", metavar="PART", help="Show files in a specific part")

    # download
    p_dl = sub.add_parser("download", help="Download file(s) from a part")
    p_dl.add_argument("game", help="Game ID (e.g. WOWS.WW.PRODUCTION)")
    p_dl.add_argument("part", help="Part name (e.g. hotfix, client, locale)")
    p_dl.add_argument("filename", nargs="?", help="File to download (basename or full name)")
    p_dl.add_argument("-o", "--output", help="Output path (default: basename)")
    p_dl.add_argument("-d", "--dir", help="Output directory when using --all (default: current dir)")
    p_dl.add_argument("--all", action="store_true", help="Download all files in the part")

    # extract
    p_ext = sub.add_parser("extract", help="Extract files from a remote .dspkg archive (no full download needed)")
    p_ext.add_argument("game", help="Game ID (e.g. WOWS.WW.PRODUCTION)")
    p_ext.add_argument("part", help="Part name (e.g. client, locale)")
    p_ext.add_argument("paths", nargs="*", help="Specific file paths to extract (supports globs)")
    p_ext.add_argument("-d", "--dir", default=".", help="Output directory (default: current dir)")
    p_ext.add_argument("--list", action="store_true", help="Only list files in the archive, don't extract")
    p_ext.add_argument("--filter", metavar="GLOB", help="Extract files matching this glob pattern")

    args = parser.parse_args()

    if args.command == "games":
        cmd_games(args)
    elif args.command == "list":
        cmd_list(args)
    elif args.command == "download":
        cmd_download(args)
    elif args.command == "extract":
        cmd_extract(args)

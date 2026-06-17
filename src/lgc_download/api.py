"""LGC API client — metadata, patches chain, and showroom fetching."""

import json
import os
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

CHAIN_BOOTSTRAP = "f00"
META_PROTO = "7.10"
PATCHES_PROTO = "1.11"
LANG_CODE = "RU"
GC_PUBLISHER = "lesta"

USER_AGENT = "lgc-download/0.2.0"

SHOWROOM_URL = (
    "https://lstuscs-ru.lesta.ru/api/v21/content/showroom/"
    "?lang=RU&gameid=LGC.RU.PRODUCTION&format=json"
    "&gc_publisher_id=lesta&country_code=RU"
)


def fetch_url(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()


def fetch_showroom() -> list[dict]:
    """Fetch the showroom catalog. Returns list of game instances."""
    data = json.loads(fetch_url(SHOWROOM_URL))
    games = []
    for entry in data["data"]["showcase"]:
        game_name = entry.get("game_name", "")
        for instance in entry.get("instances", []):
            app_id = instance.get("application_id", "")
            update_url = instance.get("update_service_url", "").rstrip("/")
            region_name = instance.get("name", "")
            if app_id and update_url:
                games.append({
                    "app_id": app_id,
                    "api_base": update_url + "/api/v1",
                    "game_name": game_name,
                    "region_name": region_name,
                })
    return games


def resolve_game(app_id: str) -> dict:
    """Look up a game by exact application_id (e.g. WOT.EU.PRODUCTION).

    Returns dict with app_id, api_base, game_name, region_name.
    Exits with error if not found.
    """
    games = fetch_showroom()
    for g in games:
        if g["app_id"].upper() == app_id.upper():
            return g

    print(f"Error: unknown game '{app_id}'.", file=sys.stderr)
    print("Available games:", file=sys.stderr)
    for g in games:
        print(f"  {g['app_id']:<25} {g['game_name']} — {g['region_name']}", file=sys.stderr)
    sys.exit(1)


def fetch_metadata(api_base: str, guid: str) -> ET.Element:
    url = (
        f"{api_base}/metadata/"
        f"?guid={guid}&chain_id={CHAIN_BOOTSTRAP}&protocol_version={META_PROTO}"
    )
    return ET.fromstring(fetch_url(url))


def fetch_patches_chain(api_base: str, guid: str,
                        metadata_version: str, chain_id: str,
                        client_type: str,
                        part_ids: list[str]) -> ET.Element:
    params = {
        "game_id": guid,
        "protocol_version": PATCHES_PROTO,
        "metadata_version": metadata_version,
        "metadata_protocol_version": META_PROTO,
        "client_type": client_type,
        "lang": LANG_CODE,
        "chain_id": chain_id,
        "game_installation": "false",
        "gc_publisher": GC_PUBLISHER,
    }
    for part_id in part_ids:
        params[f"{part_id}_current_version"] = "0"

    url = f"{api_base}/patches_chain/?{urllib.parse.urlencode(params)}"
    return ET.fromstring(fetch_url(url))


def build_direct_url(torrent_url: str, file_name: str) -> str | None:
    if not torrent_url or not file_name:
        return None
    p = urllib.parse.urlparse(torrent_url)
    segs = [s for s in p.path.split("/") if s]
    if not segs:
        return None
    segs[-1] = os.path.basename(file_name)
    new_path = "/" + "/".join(segs)
    return urllib.parse.urlunparse((p.scheme, p.netloc, new_path, p.params, p.query, p.fragment))


def parse_patches(patches_root: ET.Element) -> dict:
    patches = {}
    latest_version = None

    for patch in patches_root.findall("./patches_chain/patch"):
        part = (patch.findtext("part") or "").strip()
        version_from = (patch.findtext("version_from") or "").strip()
        version_to = (patch.findtext("version_to") or "").strip()

        if version_to and latest_version is None:
            latest_version = version_to

        torrent_urls = [
            u.text.strip()
            for u in patch.findall("./torrent/urls/url")
            if u.text and u.text.strip()
        ]

        files = []
        for f in patch.findall("./files/file"):
            name = (f.findtext("name") or "").strip()
            size = int((f.findtext("size") or "0").strip() or 0)
            unpacked = int((f.findtext("unpacked_size") or "0").strip() or 0)
            files.append({
                "name": name,
                "basename": os.path.basename(name),
                "size": size,
                "unpackedSize": unpacked,
                "downloadUrl": build_direct_url(torrent_urls[0] if torrent_urls else "", name),
            })

        patches[part] = {
            "part": part,
            "versionFrom": version_from,
            "versionTo": version_to,
            "files": files,
        }

    return {"latestVersion": latest_version, "patches": patches}


def get_manifest(api_base: str, guid: str) -> dict:
    """Fetch metadata + patches_chain and return parsed manifest."""
    meta = fetch_metadata(api_base, guid)
    metadata_version = (meta.findtext("version") or "").strip()
    chain_id = (meta.findtext("./predefined_section/chain_id") or "").strip()

    if not metadata_version or not chain_id:
        raise RuntimeError("Could not parse metadata (version or chain_id missing)")

    # Read default client_type and part IDs from metadata
    ct_elem = meta.find("./predefined_section/client_types")
    if ct_elem is None:
        raise RuntimeError("Could not find client_types in metadata")

    client_type = ct_elem.get("default")
    if not client_type:
        raise RuntimeError("No default client_type in metadata")

    part_ids = []
    for ct in ct_elem.findall("client_type"):
        if ct.get("id") == client_type:
            part_ids = [cp.get("id") for cp in ct.findall("client_parts/client_part")]
            break
    if not part_ids:
        raise RuntimeError(f"No client_parts found for client_type '{client_type}'")

    patches_root = fetch_patches_chain(api_base, guid, metadata_version, chain_id, client_type, part_ids)
    manifest = parse_patches(patches_root)
    manifest["metadataVersion"] = metadata_version
    manifest["chainId"] = chain_id
    return manifest

# lgc-download

CLI tool to list, download, and selectively extract game files from the Lesta Game Center (LGC) CDN.

Supports all games available through LGC: **Mir Korabley**, **Mir Tankov**, **Tanks Blitz**, and maybe others in the future.

The key feature is **remote partial extraction** — you can pull individual files out of a 58 GB archive by downloading only the bytes you need, using HTTP range requests against the CDN.

## How it works

Lesta Game Center distributes updates as `.dspkg` archives (7z format) split into *parts*: `client`, `locale`, `sdcontent`, `hotfix`. Each part contains one archive.

This tool:

1. Queries the LGC showroom API to discover all available games and their update servers
2. Fetches version metadata and patch chains for the selected game
3. Parses 7z archive headers remotely via HTTP range requests (~512 KB to index any archive)
4. Fetches only the compressed streams for files you request
5. Decompresses locally (LZMA2, BCJ+LZMA2)

## Install

```
pip install .
```

Or run directly:

```
python lgc.py <command>
```

Requires Python 3.10+ and [py7zr](https://pypi.org/project/py7zr/) (installed automatically).

## Usage

### List available games

```bash
lgc-download games
```

```
Available games:

  MT.RU.PRODUCTION          Мир танков                Мир танков
  MT.PT.PRODUCTION          Мир танков                Мир танков Общий тест
  MK.RU.PRODUCTION          Мир кораблей              Мир кораблей
  MK.RPT.PRODUCTION         Мир кораблей              Мир кораблей Общий тест
  WOTB.RU.PRODUCTION        Tanks Blitz               None
```

### List versions and parts

```bash
lgc-download list MK.RU.PRODUCTION
lgc-download list MT.RU.PRODUCTION
lgc-download list MT.RU.PRODUCTION --files client
```

### Download a full .dspkg

```bash
lgc-download download MT.RU.PRODUCTION locale --all -d downloads/
```

Skips files that are already downloaded with the correct size. Uses atomic writes (`.part` + rename).

### Extract files from a remote archive (no full download)

List and extract individual files from a remote `.dspkg` without downloading the entire archive:

```bash
# List all files inside the client archive
lgc-download extract MT.RU.PRODUCTION client --list

# Extract a single file
lgc-download extract MT.RU.PRODUCTION client Korabli.exe -d out/

# Extract files matching a glob
lgc-download extract MT.RU.PRODUCTION locale --filter "*/res/texts/ru/**" -d out/

# Works with any game
lgc-download extract MT.RU.PRODUCTION client --list
```

Example output:

```
Archive: mk_26.6.1.0.8854215_client.dspkg (33.1 GB)
URL: https://dl-korabli-s3.lesta.ru/cis/patches/mk_26.6.1.0.8854215_ru/mk_26.6.1.0.8854215_client.dspkg
Reading archive index via range requests...
Index: 799 files (2 HTTP requests, ~512KB transferred)

Extracting 1 file(s): 12.9 KB to download, 31.4 KB uncompressed

  GET  bin/8854215/idx/system_data.idx (12.9 KB) ... -> 31.4 KB

Done: 1 file(s) extracted
Total HTTP requests: 3
```

3 HTTP requests to pull a 31 KB file from a 58.7 GB archive.

## How remote extraction works

`.dspkg` files are 7z archives with non-solid compression (each file is independently compressed). This makes selective extraction possible:

1. **HEAD request** — get file size and confirm range request support
2. **Range request #1** — fetch the 7z signature header (32 bytes) to locate the metadata
3. **Range request #2** — fetch the metadata/index from the end of the file (~256 KB)
4. **Parse the index** — py7zr decodes the 7z header to get file names, byte offsets, and compression info
5. **Range request per file** — fetch only the compressed bytes for each requested file
6. **Decompress locally** — LZMA2 or BCJ+LZMA2 depending on content type

## License

Upstream: [MIT](https://github.com/Monstrofil/wgc-download/blob/master/LICENSE)
This project: MIT
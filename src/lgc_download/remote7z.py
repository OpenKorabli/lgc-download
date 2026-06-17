"""Remote 7z archive support — HTTP range-request-backed reading and extraction."""

import io
import lzma
import urllib.request

from .api import USER_AGENT


class RemoteFile(io.RawIOBase):
    """File-like object backed by HTTP range requests with block caching."""

    BLOCK = 256 * 1024  # 256KB per range request

    def __init__(self, url: str):
        super().__init__()
        self.url = url
        self._pos = 0
        req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": USER_AGENT})
        resp = urllib.request.urlopen(req, timeout=15)
        self._size = int(resp.headers["Content-Length"])
        if resp.headers.get("Accept-Ranges") != "bytes":
            raise RuntimeError("Server does not support range requests")
        self._cache = {}
        self._requests = 0

    def seekable(self):
        return True

    def readable(self):
        return True

    def writable(self):
        return False

    def tell(self):
        return self._pos

    def seek(self, offset, whence=0):
        if whence == 0:
            self._pos = offset
        elif whence == 1:
            self._pos += offset
        elif whence == 2:
            self._pos = self._size + offset
        return self._pos

    def readinto(self, b):
        data = self.read(len(b))
        n = len(data)
        b[:n] = data
        return n

    def read(self, n=-1):
        if n is None or n < 0:
            n = self._size - self._pos
        if n <= 0 or self._pos >= self._size:
            return b""
        end = min(self._pos + n - 1, self._size - 1)

        result = bytearray()
        pos = self._pos
        while pos <= end:
            block_num = pos // self.BLOCK
            if block_num not in self._cache:
                bstart = block_num * self.BLOCK
                bend = min(bstart + self.BLOCK - 1, self._size - 1)
                req = urllib.request.Request(self.url, headers={
                    "User-Agent": USER_AGENT,
                    "Range": f"bytes={bstart}-{bend}",
                })
                self._cache[block_num] = urllib.request.urlopen(req, timeout=30).read()
                self._requests += 1

            block_data = self._cache[block_num]
            off_in_block = pos - block_num * self.BLOCK
            take = min(len(block_data) - off_in_block, end - pos + 1)
            result.extend(block_data[off_in_block:off_in_block + take])
            pos += take

        self._pos = pos
        return bytes(result)

    def fetch_range(self, offset: int, size: int) -> bytes:
        """Fetch an exact byte range (bypasses block cache for large reads)."""
        end = offset + size - 1
        req = urllib.request.Request(self.url, headers={
            "User-Agent": USER_AGENT,
            "Range": f"bytes={offset}-{end}",
        })
        self._requests += 1
        return urllib.request.urlopen(req, timeout=120).read()


# ── 7z decompression helpers ─────────────────────────────────────────────────

# Known 7z method IDs
_METHOD_LZMA2 = b"\x21"
_METHOD_BCJ_X86 = b"\x03\x03\x01\x03"
_METHOD_DELTA = b"\x03"
_METHOD_COPY = b"\x00"
_METHOD_BCJ_ARM = b"\x03\x03\x01\x1b"


def _lzma2_dict_size(props_byte: int) -> int:
    """Decode LZMA2 dictionary size from the properties byte."""
    if props_byte == 40:
        return 0xFFFFFFFF
    return (2 | (props_byte & 1)) << (props_byte // 2 + 11)


def _build_lzma_filters(coders: list[dict]) -> list[dict]:
    """Build Python lzma filter chain from 7z folder coders.

    7z coders are in decompression order (LZMA2 first, then BCJ).
    Python's lzma expects compression order (BCJ first, then LZMA2).
    So we reverse the 7z coder list and map each to a Python filter.
    """
    filters = []
    for coder in reversed(coders):
        method = coder["method"]
        props = coder.get("properties") or b""

        if method == _METHOD_LZMA2:
            dict_size = _lzma2_dict_size(props[0]) if props else 1 << 24
            filters.append({"id": lzma.FILTER_LZMA2, "dict_size": dict_size})
        elif method == _METHOD_BCJ_X86:
            filters.append({"id": lzma.FILTER_X86})
        elif method == _METHOD_BCJ_ARM:
            filters.append({"id": lzma.FILTER_ARM})
        elif method == _METHOD_DELTA:
            dist = props[0] + 1 if props else 1
            filters.append({"id": lzma.FILTER_DELTA, "dist": dist})
        elif method == _METHOD_COPY:
            pass
        else:
            raise RuntimeError(f"Unsupported 7z method: {method.hex()}")

    return filters


def decompress_entry(data: bytes, filters: list[dict], max_length: int) -> bytes:
    """Decompress a single 7z stream using its filter chain."""
    if not filters:
        return data
    dec = lzma.LZMADecompressor(format=lzma.FORMAT_RAW, filters=filters)
    return dec.decompress(data, max_length=max_length)


def parse_archive_index(rf: RemoteFile) -> list[dict]:
    """Parse a remote .dspkg (7z) archive index via range requests.

    Returns a list of file entries with: filename, is_dir, offset,
    compressed_size, uncompressed_size, and filters.
    """
    import py7zr

    bf = io.BufferedReader(rf, buffer_size=256 * 1024)
    with py7zr.SevenZipFile(bf, "r") as z:
        si = z.header.main_streams
        files_info = z.header.files_info.files
        folders = si.unpackinfo.folders
        pack_start = si.packinfo.packpos
        pack_positions = si.packinfo.packpositions
        pack_sizes = si.packinfo.packsizes

        entries = []
        folder_idx = 0
        for f in files_info:
            if f.get("emptystream", False):
                entries.append({
                    "filename": f["filename"],
                    "is_dir": True,
                    "offset": 0,
                    "compressed_size": 0,
                    "uncompressed_size": 0,
                    "filters": [],
                })
                continue

            folder = folders[folder_idx]
            abs_offset = 32 + pack_start + pack_positions[folder_idx]
            csize = pack_sizes[folder_idx]
            usize = folder.unpacksizes[0] if folder.unpacksizes else 0

            entries.append({
                "filename": f["filename"],
                "is_dir": False,
                "offset": abs_offset,
                "compressed_size": csize,
                "uncompressed_size": usize,
                "filters": _build_lzma_filters(folder.coders),
            })
            folder_idx += 1

    return entries

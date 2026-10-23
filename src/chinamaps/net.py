"""Network helpers: GDAL env defaults, a shared requests session, an HTTP
range-reading file object (for pyarrow), and a cached downloader.

All remote access uses unsigned https:// URLs. Never use /vsis3/ or signed S3
clients: the AWS_* variables in some environments are placeholders.
"""

from __future__ import annotations

import io
import os
import threading
from pathlib import Path

import requests

REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = Path(os.environ.get("CHINAMAPS_DATA", REPO_ROOT / "data"))
OUT_DIR = Path(os.environ.get("CHINAMAPS_OUT", REPO_ROOT / "out"))

SOURCE_COOP = "https://s3.us-west-2.amazonaws.com/us-west-2.opendata.source.coop"

GDAL_ENV = {
    "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
    "CPL_VSIL_CURL_ALLOWED_EXTENSIONS": ".tif,.TIF",
    "GDAL_HTTP_MULTIRANGE": "YES",
    "GDAL_HTTP_MERGE_CONSECUTIVE_RANGES": "YES",
    "GDAL_HTTP_MAX_RETRY": "5",
    "GDAL_HTTP_RETRY_DELAY": "2",
    "VSI_CACHE": "TRUE",
    "VSI_CACHE_SIZE": str(256 * 1024 * 1024),
    "AWS_NO_SIGN_REQUEST": "YES",
    "PROJ_NETWORK": "OFF",
}


def configure_gdal() -> None:
    """Set GDAL defaults for range reads (existing values win)."""
    for k, v in GDAL_ENV.items():
        os.environ.setdefault(k, v)


configure_gdal()

_local = threading.local()


def session() -> requests.Session:
    """Per-thread requests session (honours HTTPS_PROXY / REQUESTS_CA_BUNDLE)."""
    s = getattr(_local, "session", None)
    if s is None:
        s = requests.Session()
        adapter = requests.adapters.HTTPAdapter(
            max_retries=requests.adapters.Retry(total=5, backoff_factor=1.0, status_forcelist=(429, 500, 502, 503, 504))
        )
        s.mount("https://", adapter)
        _local.session = s
    return s


def vsicurl(url: str) -> str:
    return url if url.startswith("/vsicurl/") else f"/vsicurl/{url}"


def head_size(url: str) -> int:
    r = session().head(url, timeout=60, allow_redirects=True)
    r.raise_for_status()
    return int(r.headers["Content-Length"])


def get_range(url: str, start: int, end: int) -> bytes:
    """Inclusive byte range [start, end]."""
    r = session().get(url, headers={"Range": f"bytes={start}-{end}"}, timeout=120)
    r.raise_for_status()
    return r.content


def get_tail(url: str, nbytes: int) -> bytes:
    r = session().get(url, headers={"Range": f"bytes=-{nbytes}"}, timeout=120)
    r.raise_for_status()
    return r.content


class HTTPRangeFile(io.RawIOBase):
    """Seekable read-only file over HTTP range requests, with a small block cache.

    Good enough for pyarrow.parquet.ParquetFile, which reads the footer and
    then whole column chunks.
    """

    def __init__(self, url: str, size: int | None = None, block: int = 1 << 20):
        self.url = url
        self.size = size if size is not None else head_size(url)
        self.block = block
        self.pos = 0
        self._cache: dict[int, bytes] = {}

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self.pos

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        if whence == io.SEEK_SET:
            self.pos = offset
        elif whence == io.SEEK_CUR:
            self.pos += offset
        else:
            self.pos = self.size + offset
        return self.pos

    def read(self, n: int = -1) -> bytes:
        if n is None or n < 0:
            n = self.size - self.pos
        n = max(0, min(n, self.size - self.pos))
        if n == 0:
            return b""
        start, end = self.pos, self.pos + n
        if n > 4 * self.block:
            data = get_range(self.url, start, end - 1)
        else:
            parts = []
            for b in range(start // self.block, (end - 1) // self.block + 1):
                if b not in self._cache:
                    lo = b * self.block
                    self._cache[b] = get_range(self.url, lo, min(lo + self.block, self.size) - 1)
                parts.append(self._cache[b])
            buf = b"".join(parts)
            off = start - (start // self.block) * self.block
            data = buf[off : off + n]
        self.pos = end
        return data

    def readinto(self, b) -> int:
        data = self.read(len(b))
        b[: len(data)] = data
        return len(data)


def download(url: str, dest: Path, *, chunk: int = 1 << 20) -> Path:
    """Download url to dest unless it already exists (atomic rename)."""
    dest = Path(dest)
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    with session().get(url, stream=True, timeout=300) as r:
        r.raise_for_status()
        with open(tmp, "wb") as f:
            for c in r.iter_content(chunk):
                f.write(c)
    tmp.replace(dest)
    return dest

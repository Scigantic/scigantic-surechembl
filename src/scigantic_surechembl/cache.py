"""Local response cache, ON by default, same reasoning as scigantic-pubchem
and the reverse of scigantic-chembl/scigantic-bindingdb (both default OFF).

Those two read a public S3 mirror with no meaningful rate limit, so a
cache there is a convenience. This package calls SureChEMBL's live REST
API for every lookup, an EMBL-EBI shared service with no published quota
and no rate-limit headers to read (checked 2026-09-08), so being a polite
client is entirely on the caller. A notebook re-fetching the same
compound in a loop is the exact pattern a cache prevents.

Entries expire after ttl_days (30 by default). SureChEMBL republishes its
bulk data every two weeks and the live index moves continuously, so an
entry that never expired would quietly become a stale snapshot inside a
package whose whole argument is that live is more correct than stale.

Keys are opaque JSON documents (see key()), so a function can cache under
whatever identifies its result: a REST path plus params, or a synthetic
tuple for a multi-request operation like a paged structure search.

Writes go to a uniquely-named temp file then an atomic os.replace(), so a
concurrent reader never sees a partial entry and two writers racing on
the same key never collide on a temp path. enable_cache()/disable_cache()
are one-time configuration calls, not synchronized against concurrent
reads, the same way mutating os.environ isn't.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any

_enabled = True
_cache_dir: Path | None = None
_ttl_seconds: float | None = 30 * 86400


def _default_cache_dir() -> Path:
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    elif sys.platform == "darwin":
        base = str(Path.home() / "Library" / "Caches")
    else:
        base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "scigantic-surechembl"


def _resolve_dir() -> Path:
    global _cache_dir
    if _cache_dir is None:
        env = os.environ.get("SCIGANTIC_SURECHEMBL_CACHE")
        _cache_dir = Path(env) if env else _default_cache_dir()
        _cache_dir.mkdir(parents=True, exist_ok=True)
    return _cache_dir


def enable_cache(cache_dir: str | None = None, ttl_days: float | None = 30) -> Path:
    """Turn caching on (it already is, by default), optionally pointing it
    at a directory and/or changing how long an entry stays valid.
    ttl_days=None disables expiry. Returns the resolved directory."""
    global _enabled, _cache_dir, _ttl_seconds
    if cache_dir is not None:
        _cache_dir = Path(cache_dir)
        _cache_dir.mkdir(parents=True, exist_ok=True)
    else:
        _resolve_dir()
    _ttl_seconds = ttl_days * 86400 if ttl_days is not None else None
    _enabled = True
    assert _cache_dir is not None
    return _cache_dir


def disable_cache() -> None:
    """Turn caching off. Every call hits the network fresh until re-enabled."""
    global _enabled
    _enabled = False


def is_cache_enabled() -> bool:
    return _enabled


def cache_dir() -> Path:
    return _resolve_dir()


def key(*parts: Any) -> str:
    """Stable digest of any JSON-serializable identifying parts."""
    raw = json.dumps(list(parts), sort_keys=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()


def get(cache_key: str) -> Any | None:
    if not _enabled:
        return None
    file = _resolve_dir() / f"{cache_key}.json"
    if not file.exists():
        return None
    try:
        entry = json.loads(file.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    if _ttl_seconds is not None and time.time() - entry.get("cached_at", 0) > _ttl_seconds:
        file.unlink(missing_ok=True)
        return None
    return entry["value"]


def put(cache_key: str, value: Any) -> None:
    if not _enabled:
        return
    file = _resolve_dir() / f"{cache_key}.json"
    tmp = file.with_suffix(f".json.{uuid.uuid4().hex}.part")
    tmp.write_text(json.dumps({"cached_at": time.time(), "value": value}))
    os.replace(tmp, file)


def clear() -> int:
    """Delete every cached entry, including any partial write left by a
    crash mid-rename. Returns how many files were removed."""
    d = _resolve_dir()
    n = 0
    for pattern in ("*.json", "*.part"):
        for f in d.glob(pattern):
            f.unlink()
            n += 1
    return n

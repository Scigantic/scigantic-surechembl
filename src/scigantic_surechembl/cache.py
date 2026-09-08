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

The cache can never make a lookup fail. A directory that cannot be
created or written (read-only filesystem, a path that is a file, an
unwritable SCIGANTIC_SURECHEMBL_CACHE) turns caching off for the process
with one warning, and the lookup proceeds live; a corrupt, truncated or
unreadable entry is treated as a miss. Found by the stress test: before
this, a read-only cache directory raised PermissionError from every
single call.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
import uuid
import warnings
from pathlib import Path
from typing import Any

_enabled = True
_cache_dir: Path | None = None
_ttl_seconds: float | None = 30 * 86400
_write_failed = False


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


def _dir_or_disable() -> Path | None:
    """The cache directory, or None (and caching turned off, with one
    warning) if it cannot be created."""
    global _enabled
    try:
        return _resolve_dir()
    except OSError as exc:
        _enabled = False
        warnings.warn(f"scigantic-surechembl cache disabled: cannot use cache directory ({exc})", stacklevel=3)
        return None


def enable_cache(cache_dir: str | None = None, ttl_days: float | None = 30) -> Path:
    """Turn caching on (it already is, by default), optionally pointing it
    at a directory and/or changing how long an entry stays valid.
    ttl_days=None disables expiry. Returns the resolved directory.

    This is the one place an unusable directory raises (OSError) rather
    than quietly disabling the cache: a caller who names a directory
    explicitly wants to know it does not work. The default path taken on
    first use degrades instead (see module docstring)."""
    global _enabled, _cache_dir, _ttl_seconds, _write_failed
    if cache_dir is not None:
        _cache_dir = Path(cache_dir)
        _cache_dir.mkdir(parents=True, exist_ok=True)
    else:
        _resolve_dir()
    _ttl_seconds = ttl_days * 86400 if ttl_days is not None else None
    _enabled = True
    _write_failed = False
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
    directory = _dir_or_disable()
    if directory is None:
        return None
    file = directory / f"{cache_key}.json"
    try:
        entry = json.loads(file.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError, UnicodeDecodeError):
        return None
    if not isinstance(entry, dict) or "value" not in entry:
        return None  # not something this module wrote; treat as a miss
    cached_at = entry.get("cached_at", 0)
    if _ttl_seconds is not None and (
        not isinstance(cached_at, (int, float)) or time.time() - cached_at > _ttl_seconds
    ):
        file.unlink(missing_ok=True)
        return None
    return entry["value"]


def put(cache_key: str, value: Any) -> None:
    global _enabled, _write_failed
    if not _enabled:
        return
    directory = _dir_or_disable()
    if directory is None:
        return
    file = directory / f"{cache_key}.json"
    tmp = file.with_suffix(f".json.{uuid.uuid4().hex}.part")
    try:
        tmp.write_text(json.dumps({"cached_at": time.time(), "value": value}))
        os.replace(tmp, file)
    except OSError as exc:
        tmp.unlink(missing_ok=True)
        if not _write_failed:
            _write_failed = True
            _enabled = False
            warnings.warn(f"scigantic-surechembl cache disabled: cannot write to {directory} ({exc})", stacklevel=3)


def clear() -> int:
    """Delete every cached entry, including any partial write left by a
    crash mid-rename. Returns how many files were removed. Safe to call
    while other threads are reading and writing: a file another thread
    removed or replaced first is simply skipped."""
    d = _dir_or_disable()
    if d is None:
        return 0
    n = 0
    for pattern in ("*.json", "*.part"):
        for f in d.glob(pattern):
            try:
                f.unlink()
                n += 1
            except FileNotFoundError:
                continue
    return n

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Optional

from .ap_bizhelper_config import get_path_setting
from .constants import DEBUG_DOWNLOAD_CACHE_KEY, DOWNLOAD_CACHE_DIR_KEY

ENCODING_UTF8 = "utf-8"
METADATA_SUFFIX = ".json"


def _cache_key(url: str, expected_digest: str, digest_algorithm: str) -> str:
    key_source = f"{url}\n{digest_algorithm}\n{expected_digest}"
    return hashlib.sha256(key_source.encode(ENCODING_UTF8)).hexdigest()


def _cache_paths(cache_dir: Path, cache_key: str) -> tuple[Path, Path]:
    return cache_dir / cache_key, cache_dir / f"{cache_key}{METADATA_SUFFIX}"


def _load_metadata(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        with path.open("r", encoding=ENCODING_UTF8) as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_metadata(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding=ENCODING_UTF8) as handle:
        json.dump(data, handle, indent=2, sort_keys=True)
        handle.write("\n")
    tmp.replace(path)


def _compute_digest(path: Path, algorithm: str) -> str:
    hasher = hashlib.new(algorithm)
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest().lower()


def _resolve_expected_digest(
    expected_digest: str,
    digest_algorithm: str,
    metadata_path: Path,
) -> tuple[str, str]:
    if expected_digest:
        return expected_digest.lower(), digest_algorithm
    metadata = _load_metadata(metadata_path)
    metadata_digest = str(metadata.get("digest") or "")
    metadata_algorithm = str(metadata.get("algorithm") or digest_algorithm)
    if not metadata_digest:
        return "", digest_algorithm
    return metadata_digest.lower(), metadata_algorithm


def maybe_use_download_cache(
    url: str,
    dest: Path,
    settings: dict,
    *,
    expected_hash: Optional[str] = None,
    hash_name: str = "sha256",
) -> bool:
    if not settings.get(DEBUG_DOWNLOAD_CACHE_KEY):
        return False

    cache_dir = get_path_setting(settings, DOWNLOAD_CACHE_DIR_KEY)
    if not cache_dir:
        return False

    normalized_expected = str(expected_hash or "").lower()
    cache_key = _cache_key(url, normalized_expected, hash_name)
    cache_path, metadata_path = _cache_paths(cache_dir, cache_key)
    if not cache_path.is_file():
        return False

    expected_digest, digest_algorithm = _resolve_expected_digest(
        normalized_expected,
        hash_name,
        metadata_path,
    )
    if not expected_digest:
        return False

    try:
        computed = _compute_digest(cache_path, digest_algorithm)
    except Exception:
        return False

    if computed != expected_digest:
        return False

    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(cache_path, dest)
    return True


def store_download_cache(
    url: str,
    source: Path,
    settings: dict,
    *,
    expected_hash: Optional[str] = None,
    hash_name: str = "sha256",
    computed_hash: Optional[str] = None,
    computed_hash_name: Optional[str] = None,
) -> None:
    if not settings.get(DEBUG_DOWNLOAD_CACHE_KEY):
        return

    cache_dir = get_path_setting(settings, DOWNLOAD_CACHE_DIR_KEY)
    if not cache_dir:
        return

    normalized_expected = str(expected_hash or "").lower()
    cache_key = _cache_key(url, normalized_expected, hash_name)
    cache_path, metadata_path = _cache_paths(cache_dir, cache_key)

    digest = normalized_expected
    digest_algorithm = computed_hash_name or hash_name
    if not digest:
        digest = (computed_hash or "").lower()

    if not digest:
        try:
            digest = _compute_digest(source, digest_algorithm)
        except Exception:
            return

    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, cache_path)
        _save_metadata(
            metadata_path,
            {"url": url, "digest": digest.lower(), "algorithm": digest_algorithm},
        )
    except Exception:
        return

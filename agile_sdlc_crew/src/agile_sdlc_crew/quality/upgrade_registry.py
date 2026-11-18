"""Docker Hub'dan yama sürümü ve etiket kanıtı (salt-okunur, genel uç). Okunamazsa None — çağıran elle bırakır."""
from __future__ import annotations

import logging
import re
import time
from pathlib import Path

import yaml

log = logging.getLogger("pipeline")

HUB = "https://hub.docker.com/v2/repositories/library"
IMAGE_YAML = Path(__file__).resolve().parent.parent / "config" / "upgrade_image.yaml"
_NAMES = ("php", "python", "golang")
_TAG_RE = re.compile(r"^[\w][\w.\-]*$")
_MINOR_RE = re.compile(r"^\d+\.\d+$")


def _http_fetch(url: str) -> tuple[int, dict | None]:
    import requests
    r = requests.get(url, timeout=10)
    try:
        return r.status_code, r.json()
    except ValueError:
        return r.status_code, None


class Resolver:
    def __init__(self, fetch=None, patches: dict | None = None, ttl_s: float = 3600):
        self._fetch = fetch or _http_fetch
        self._patches = patches or {}
        self._ttl = ttl_s
        self._cache: dict[tuple, tuple[float, object]] = {}

    def fetch(self, url: str):
        return self._fetch(url)

    def _cached(self, key: tuple):
        hit = self._cache.get(key)
        if hit and time.monotonic() - hit[0] < self._ttl:
            return True, hit[1]
        return False, None

    def _get(self, url: str) -> tuple[int, dict | None] | None:
        try:
            status, data = self._fetch(url)
            return int(status), data if isinstance(data, dict) else None
        except Exception as e:  # noqa: BLE001
            log.warning(f"⚠️ upgrade: Docker Hub okunamadı ({url}): {e}")
            return None

    def latest_patch(self, name: str, minor: str) -> str | None:
        """`("php", "8.5") → "8.5.12"`: önce elle sabitleme, sonra Hub'daki en büyük tam `A.B.N` etiketi."""
        if name not in _NAMES or not isinstance(minor, str) or not _MINOR_RE.match(minor):
            return None
        pinned = ((self._patches or {}).get(name) or {}).get(minor)
        if pinned:
            return str(pinned)
        key = ("patch", name, minor)
        hit, val = self._cached(key)
        if hit:
            return val
        res = self._get(f"{HUB}/{name}/tags?name={minor}.&page_size=100")
        if not res or res[0] != 200 or res[1] is None:
            return None
        pat = re.compile(rf"^{re.escape(minor)}\.(\d+)$")
        nums = [int(m.group(1)) for r in (res[1].get("results") or []) if isinstance(r, dict)
                and isinstance(r.get("name"), str) and (m := pat.match(r["name"]))]
        out = f"{minor}.{max(nums)}" if nums else None
        if out:
            self._cache[key] = (time.monotonic(), out)
        return out

    def tag_exists(self, name: str, tag: str) -> bool | None:
        """200 → True, 404 → False, diğer / hata → None."""
        if name not in _NAMES or not isinstance(tag, str) or not _TAG_RE.match(tag):
            return None
        key = ("tag", name, tag)
        hit, val = self._cached(key)
        if hit:
            return val
        res = self._get(f"{HUB}/{name}/tags/{tag}")
        if not res or res[0] not in (200, 404):
            return None
        out = res[0] == 200
        self._cache[key] = (time.monotonic(), out)
        return out


_DEFAULT: Resolver | None = None


def default() -> Resolver:
    global _DEFAULT
    if _DEFAULT is None:
        try:
            patches = (yaml.safe_load(IMAGE_YAML.read_text(encoding="utf-8")) or {}).get("patch") or {}
        except (OSError, yaml.YAMLError):
            patches = {}
        _DEFAULT = Resolver(patches=patches)
    return _DEFAULT

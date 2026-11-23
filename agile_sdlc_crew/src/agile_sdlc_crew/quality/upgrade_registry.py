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
_NAMES = ("php", "python", "golang", "node")
_TAG_RE = re.compile(r"[\w][\w.\-]*", re.ASCII)
_MINOR_RE = re.compile(r"\d+\.\d+", re.ASCII)
DOWN_S = 60


def _http_fetch(url: str, get=None) -> tuple[int, dict | None]:
    if get is None:
        import requests
        get = requests.get
    r = get(url, timeout=10, headers={"User-Agent": "tempo-upgrade"})
    try:
        return r.status_code, r.json()
    except ValueError:
        return r.status_code, None


class Resolver:
    def __init__(self, fetch=None, patches: dict | None = None, ttl_s: float = 3600, clock=time.monotonic):
        self._fetch = fetch or _http_fetch
        self._patches = ({str(n): {str(k): v for k, v in m.items()} for n, m in patches.items() if isinstance(m, dict)}
                         if isinstance(patches, dict) else {})
        self._ttl = ttl_s
        self._clock = clock
        self._down_until = 0.0     # Hub hata verdi: bu ana kadar çağrı yapılmaz
        self._cache: dict[tuple, tuple[float, object]] = {}

    def fetch(self, url: str):
        return self._fetch(url)

    def _cached(self, key: tuple):
        hit = self._cache.get(key)
        if hit and self._clock() - hit[0] < self._ttl:
            return True, hit[1]
        return False, None

    def _get(self, url: str) -> tuple[int, dict | None] | None:
        if self._clock() < self._down_until:
            return None
        try:
            status, data = self._fetch(url)
            status = int(status)
        except Exception as e:  # noqa: BLE001
            log.warning(f"⚠️ upgrade: Docker Hub okunamadı ({url}): {e}")
            self._down_until = self._clock() + DOWN_S
            return None
        if status not in (200, 404):
            self._down_until = self._clock() + DOWN_S
        return status, data if isinstance(data, dict) else None

    def latest_patch(self, name: str, minor: str) -> str | None:
        """`("php", "8.5") → "8.5.12"`: önce elle sabitleme, sonra Hub'daki en büyük tam `A.B.N` etiketi."""
        if name not in _NAMES or not isinstance(minor, str) or not _MINOR_RE.fullmatch(minor):
            return None
        pinned = str((self._patches.get(name) or {}).get(minor) or "")
        if re.fullmatch(rf"{re.escape(minor)}\.\d+", pinned, re.ASCII):
            return pinned
        key = ("patch", name, minor)
        hit, val = self._cached(key)
        if hit:
            return val
        res = self._get(f"{HUB}/{name}/tags?name={minor}.&page_size=100")
        if not res or res[0] != 200 or res[1] is None:
            return None
        pat = re.compile(rf"^{re.escape(minor)}\.(\d+)$", re.ASCII)
        nums = [int(m.group(1)) for r in (res[1].get("results") or []) if isinstance(r, dict)
                and isinstance(r.get("name"), str) and (m := pat.match(r["name"]))]
        out = f"{minor}.{max(nums)}" if nums else None
        if out:
            self._cache[key] = (self._clock(), out)
        return out

    def latest_in_major(self, name: str, major: int) -> str | None:
        """`("node", 24) → "24.22.3"`: majörün en yeni tam sürümü (Node'da uyumluluk hattı majördür). Önce elle
        sabitleme (`patch: {node: {"24.0": "24.22.3"}}`), sonra Hub'ın en yeni 100 etiketindeki en büyük
        `<majör>.B.C` (çeşit ekli etiketler de sayılır: `24.22.3-alpine`). Okunamazsa None."""
        if name not in _NAMES or isinstance(major, bool) or not isinstance(major, int) or not 0 < major < 1000:
            return None
        pinned = str((self._patches.get(name) or {}).get(f"{major}.0") or "")
        if re.fullmatch(rf"{major}\.\d+\.\d+", pinned, re.ASCII):
            return pinned
        key = ("major", name, major)
        hit, val = self._cached(key)
        if hit:
            return val
        res = self._get(f"{HUB}/{name}/tags?name={major}.&page_size=100")
        if not res or res[0] != 200 or res[1] is None:
            return None
        pat = re.compile(rf"^{major}\.(\d+)\.(\d+)(?:-|$)", re.ASCII)
        vs = [(int(m.group(1)), int(m.group(2))) for r in (res[1].get("results") or []) if isinstance(r, dict)
              and isinstance(r.get("name"), str) and (m := pat.match(r["name"]))]
        out = f"{major}.{max(vs)[0]}.{max(vs)[1]}" if vs else None
        if out:
            self._cache[key] = (self._clock(), out)
        return out

    def tag_exists(self, name: str, tag: str) -> bool | None:
        """200 → True, 404 → False, diğer / hata → None."""
        if name not in _NAMES or not isinstance(tag, str) or not _TAG_RE.fullmatch(tag):
            return None
        key = ("tag", name, tag)
        hit, val = self._cached(key)
        if hit:
            return val
        res = self._get(f"{HUB}/{name}/tags/{tag}")
        if not res or res[0] not in (200, 404):
            return None
        out = res[0] == 200
        self._cache[key] = (self._clock(), out)
        return out


_DEFAULT: Resolver | None = None


def _load_patches(path: Path = IMAGE_YAML) -> dict:
    """yaml `patch:` elle sabitlemeleri; bozuk / beklenmedik biçim → sabitleme yok."""
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return {}
    patches = doc.get("patch") if isinstance(doc, dict) else None
    return patches if isinstance(patches, dict) else {}


def default() -> Resolver:
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = Resolver(patches=_load_patches())
    return _DEFAULT

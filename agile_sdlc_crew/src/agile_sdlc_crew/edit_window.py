"""Büyük dosyada pencereli düzenleme.

Plan bir dosyada `current_code` → `new_code` değişikliği ister; Python önce
birebir/bulanık değiştirmeyi dener (main._try_direct_edit). O tutmazsa eski yol
geliştiriciden "yeni blok" istiyor, blok da oturmazsa onu DOSYANIN SONUNA
ekliyordu — job #229'da 352KB'lık Order.php'nin sonuna geliştiricinin satır
tarifi ("(1) satır 3678: …") eklendi, dosya kırıldı.

Burada: değişikliğin yeri (anchor) bulunur, çevresindeki ±radius satır
numaralı olarak geliştiriciye verilir, geliştirici yalnız o pencerenin yeni
halini döndürür, Python pencereyi yerine koyar. Pencere oturmazsa None —
dosya değiştirilmez; asla sona ekleme yapılmaz.
"""

from __future__ import annotations

import difflib
import re

_FENCE = re.compile(r"```[\w+-]*\n(.*?)```", re.S)
_NUM_PREFIX = re.compile(r"^\s*\d+\| ?")
_FUNC = re.compile(r"function\s+(\w+)")


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def _strip_comment(s: str) -> str:
    return re.sub(r"\s*(//|#).*$", "", s).strip()


def find_anchor(content: str, current_code: str, hint_line: int | None = None) -> tuple[int, int] | None:
    """(başlangıç satırı 1-tabanlı, kapsadığı satır sayısı) ya da None.

    Sıra: mimarın verdiği satır → current_code'un ayırt edici satırının
    birebir (boşluk/yorum duyarsız) geçtiği yer → bulanık en iyi eşleşme
    (≥0.80) → current_code'daki metot adının tanımı."""
    lines = (content or "").splitlines()
    n = len(lines)
    cur = [ln for ln in (current_code or "").splitlines()]
    span = max(1, len([c for c in cur if c.strip()]))
    if hint_line and 1 <= int(hint_line) <= n:
        return int(hint_line), span
    norm_lines = [_norm(_strip_comment(ln)) for ln in lines]
    cands = sorted(((i, _norm(_strip_comment(c))) for i, c in enumerate(cur)),
                   key=lambda t: -len(t[1]))
    cands = [(i, c) for i, c in cands if len(c) >= 12 and c not in ("{", "}", "};")][:3]
    for off, c in cands:
        hits = [k for k, ln in enumerate(norm_lines) if ln == c]
        if hits:
            first_nonblank = next((j for j, x in enumerate(cur) if x.strip()), 0)
            return max(1, hits[0] + 1 - (off - first_nonblank)), span
    for off, c in cands[:1]:
        best, best_k = 0.0, -1
        for k, ln in enumerate(norm_lines):
            sm = difflib.SequenceMatcher(None, c, ln, autojunk=False)
            if sm.real_quick_ratio() < 0.8 or sm.quick_ratio() < 0.8:
                continue
            r = sm.ratio()
            if r > best:
                best, best_k = r, k
        if best >= 0.80:
            return best_k + 1, span
    m = _FUNC.search(current_code or "")
    if m:
        rx = re.compile(r"function\s+" + re.escape(m.group(1)) + r"\b")
        for k, ln in enumerate(lines):
            if rx.search(ln):
                return k + 1, span
    return None


def numbered_window(content: str, start: int, end: int) -> str:
    lines = content.splitlines()
    return "\n".join(f"{i:>5}| {lines[i - 1]}" for i in range(start, end + 1))


def parse_reply(reply: str) -> list[str] | None:
    """Geliştirici yanıtından pencerenin yeni satırları. Kod bloğu varsa o,
    yoksa tüm metin; satır numarası önekleri temizlenir."""
    text = reply or ""
    m = _FENCE.search(text)
    body = m.group(1) if m else text
    out = body.rstrip("\n").splitlines()
    if not out:
        return None
    numbered = sum(1 for ln in out if _NUM_PREFIX.match(ln))
    if numbered >= max(1, len(out) // 2):
        out = [_NUM_PREFIX.sub("", ln, count=1) for ln in out]
    return out


def apply_windowed(content: str, current_code: str, new_code: str, ask, *, radius: int = 50,
                   hint_line: int | None = None, min_keep: float = 0.6) -> tuple[str | None, str]:
    """ask(window_text, start, end) -> str. Döner: (yeni içerik | None, açıklama)."""
    anchor = find_anchor(content, current_code, hint_line)
    if not anchor:
        return None, "değişikliğin yeri dosyada bulunamadı"
    lines = content.splitlines()
    a, span = anchor
    start = max(1, a - radius)
    end = min(len(lines), a + span - 1 + radius)
    old = lines[start - 1:end]
    new = parse_reply(ask(numbered_window(content, start, end), start, end))
    if not new:
        return None, f"satır {start}-{end}: boş yanıt"
    # Pencerenin tamamı dönmeli; çok kısa yanıt satır silmek demektir (model
    # yalnız değişen satırı ya da bir açıklama döndürdü).
    if len(new) < len(old) * min_keep:
        return None, f"satır {start}-{end}: yanıt {len(new)} satır, pencere {len(old)} — reddedildi"
    ratio = difflib.SequenceMatcher(None, "\n".join(old), "\n".join(new), autojunk=False).ratio()
    if ratio < 0.5:
        return None, f"satır {start}-{end}: yanıt pencereye benzemiyor (%{ratio * 100:.0f}) — reddedildi"
    if new == old:
        return None, f"satır {start}-{end}: değişiklik yok"
    trailing = "\n" if content.endswith("\n") else ""
    out = "\n".join(lines[:start - 1] + new + lines[end:]) + trailing
    return out, f"satır {start}-{end} değiştirildi ({len(old)} → {len(new)} satır)"

"""Dil sürümü yükseltme: katalogdan yükseltmeyi etkileyen kuralları süzer."""
from __future__ import annotations

from . import php_modern as pm


def upgrade_rules(rules: list[pm.Rule], current: str, target: str) -> list[pm.Rule]:
    """`breaks` dolu ve current < since ≤ target olan kurallar."""
    cur, tgt = pm._ver(current), pm._ver(target)
    return [r for r in rules if r.breaks and cur < r.since <= tgt]


def blocking(rule: pm.Rule, *, auto: bool) -> bool:
    """Bulgu yükseltmeyi bekletir mi: error/behavior her zaman; deprecation yalnız otomatik düzeltilebiliyorsa."""
    if rule.breaks in ("error", "behavior"):
        return True
    if rule.breaks == "deprecation":
        return bool(auto)
    return False

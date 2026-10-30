"""Kalite işi alımı — ortak veri yapıları (Sonar + New Relic)."""

from __future__ import annotations

from dataclasses import dataclass, field


class QualitySourceError(RuntimeError):
    """Kaynak (Sonar / New Relic / FLODevOps) okunamadı — kısmi veriyle seçim yapılmaz."""


@dataclass
class Finding:
    source: str                     # "sonar" | "newrelic"
    repo: str = ""
    count: int = 1
    # Sonar
    project: str = ""               # "EcomBackend:webservice"
    file: str = ""                  # "app/Controller/Api/Order.php"
    rule: str = ""                  # "php:S2076"
    rule_name: str = ""
    severity: str = ""              # BLOCKER | CRITICAL
    itype: str = ""                 # BUG | VULNERABILITY
    lines: list[int] = field(default_factory=list)
    link: str = ""
    # New Relic
    app: str = ""
    action: str = ""                # "Api/Order::integrationCancel"
    err_class: str = ""
    message: str = ""               # ham mesaj; render maskeler. secrets:* için hep boş
    uris: list[str] = field(default_factory=list)
    first_seen: float = 0.0         # epoch saniye
    last_seen: float = 0.0
    error_rate: float = 0.0         # uygulamanın lookback error rate'i (0-1)


@dataclass
class Group:
    """Tek WI'a dönüşecek iş birimi. Birden çok parmak izi taşıyabilir (paket/aile)."""
    source: str
    repo: str
    title: str                      # repo öneki olmadan kısa başlık
    fingerprints: list[str]
    findings: list[Finding]
    score: float = 0.0
    sp: int = 1
    needs_human: bool = False
    human_reason: str = ""
    recurring_of: int | None = None
    infra: list[str] = field(default_factory=list)   # kod dışı ön koşul (ör. "ext:mbstring") → DevOps

    @property
    def last_seen(self) -> float:
        return max((f.last_seen for f in self.findings), default=0.0)

    @property
    def apps(self) -> list[str]:
        return sorted({f.app for f in self.findings if f.app})

    @property
    def files(self) -> list[str]:
        out: list[str] = []
        for f in self.findings:
            if f.file and f.file not in out:
                out.append(f.file)
        return out

    @property
    def total(self) -> int:
        return sum(f.count for f in self.findings)

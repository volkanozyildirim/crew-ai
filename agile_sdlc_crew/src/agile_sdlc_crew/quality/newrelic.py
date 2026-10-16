"""New Relic toplayıcısı (NerdGraph NRQL, EU region varsayılan).

TransactionError'da stack trace yok: sınıf + mesaj + transactionName + URI.
Prod filtresi çağırandan gelir (ad kalıbı ya da FLODevOps production
eşlemesi), çünkü 'Marketplace API' gibi prod uygulamaların adında [Prod] yok.
"""

from __future__ import annotations

import os
import re
from collections import defaultdict

import requests

from agile_sdlc_crew import pipeline_config
from agile_sdlc_crew.quality.model import Finding, QualitySourceError
from agile_sdlc_crew.quality.rank import action_of

ENDPOINTS = {"eu": "https://api.eu.newrelic.com/graphql", "us": "https://api.newrelic.com/graphql"}
_GQL = "query($a:Int!,$q:Nrql!){actor{account(id:$a){nrql(query:$q,timeout:90){results}}}}"
ERRORS_NRQL = ("SELECT count(*), uniques(request.uri, 3) AS uris, min(timestamp) AS first, "
               "max(timestamp) AS last FROM TransactionError "
               "FACET appName, error.class, error.message, transactionName "
               "SINCE {days} days ago LIMIT MAX")
TX_NRQL = "SELECT count(*) FROM Transaction FACET appName SINCE {days} days ago LIMIT MAX"
_PROD = re.compile(r"^\[(?:prod|production|gcp-prod)\]", re.I)


def is_prod_name(app: str) -> bool:
    return bool(_PROD.match(app or ""))


def nrql(query: str, *, region: str | None = None) -> list[dict]:
    acc = os.environ.get("NEW_RELIC_ACCOUNT_ID", "")
    key = os.environ.get("NEW_RELIC_API_KEY", "")
    if not acc or not key:
        raise QualitySourceError("NEW_RELIC_ACCOUNT_ID / NEW_RELIC_API_KEY tanımlı değil")
    reg = str(region or pipeline_config.get("NEW_RELIC_REGION") or "eu").lower()
    try:
        r = requests.post(ENDPOINTS.get(reg, ENDPOINTS["eu"]),
                          headers={"API-Key": key, "Content-Type": "application/json"},
                          json={"query": _GQL, "variables": {"a": int(acc), "q": query}}, timeout=120)
        r.raise_for_status()
        d = r.json()
    except (requests.RequestException, ValueError) as e:
        raise QualitySourceError(f"New Relic isteği başarısız: {e}") from e
    if d.get("errors"):
        raise QualitySourceError(f"New Relic hatası: {(d['errors'][0] or {}).get('message', '')}")
    acct = (((d.get("data") or {}).get("actor") or {}).get("account") or {})
    return (acct.get("nrql") or {}).get("results") or []


def rows_to_findings(err_rows: list[dict], tx_rows: list[dict], *, is_prod) -> list[Finding]:
    tx: dict[str, int] = {}
    for r in tx_rows or []:
        facet = r.get("facet")
        name = r.get("appName") or (facet[0] if isinstance(facet, list) else facet) or ""
        tx[name] = int(r.get("count") or 0)
    err_total: dict[str, int] = defaultdict(int)
    for r in err_rows or []:
        facet = r.get("facet") or []
        if facet:
            err_total[str(facet[0])] += int(r.get("count") or 0)
    out: list[Finding] = []
    for r in err_rows or []:
        facet = r.get("facet") or []
        if len(facet) < 4:
            continue
        app, cls, msg, txn = (str(x or "") for x in facet[:4])
        if not is_prod(app):
            continue
        rate = err_total[app] / tx[app] if tx.get(app) else 0.0
        out.append(Finding(
            source="newrelic", app=app, err_class=cls, message=msg, action=action_of(txn),
            count=int(r.get("count") or 0), uris=[u for u in (r.get("uris") or []) if u][:3],
            first_seen=float(r.get("first") or 0) / 1000, last_seen=float(r.get("last") or 0) / 1000,
            error_rate=min(rate, 1.0)))
    return out


def fetch(days: int, *, is_prod) -> list[Finding]:
    err = nrql(ERRORS_NRQL.format(days=int(days)))
    tx = nrql(TX_NRQL.format(days=int(days)))
    return rows_to_findings(err, tx, is_prod=is_prod)

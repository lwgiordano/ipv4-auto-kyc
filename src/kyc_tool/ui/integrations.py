"""Integration wiring report for the ops console ("connecting new things").

Introspects the SAME adapter registry the production worker builds
(workers.pipeline_worker.build_adapters), so the console reports what would
actually run — stub vs live, which env vars are missing, recent call stats —
plus optional reachability probes against the real upstreams.
"""

import os
import time

import httpx
from sqlalchemy import text
from sqlalchemy.orm import Session

from kyc_tool.config import Settings
from kyc_tool.configuration import repo as configuration_repo

# Static wiring facts per adapter (kind + config the live client needs).
WIRING = {
    "broker_policy": {
        "kind": "internal gate (legacy/bootstrap broker_entities table)",
        "env": [],
        "todo": "Legacy/bootstrap source; activate versioned configuration for console editing.",
    },
    "email_verification": {
        "kind": "platform event data (no upstream)",
        "env": [],
        "todo": "TODO(integration): platform email-verification fetch API (AUDIT:C4)",
    },
    "companies_house": {
        "kind": "HTTP · api.company-information.service.gov.uk",
        "env": ["CH_API_KEY"],
        "todo": None,
    },
    "gleif": {"kind": "HTTP · api.gleif.org (public)", "env": [], "todo": None},
    "floqer_company_enrichment": {
        "kind": "HTTP · Floqer enrichment",
        "env": [],
        "todo": "TODO(integration): real Floqer API contract unknown (AUDIT:C4)",
    },
    "rir_rdap": {
        "kind": "HTTP · 5 RIR RDAP endpoints",
        "env": ["ARIN_API_KEY (optional, rate limits)"],
        "todo": None,
    },
    "rir_poc": {
        "kind": "RIR POC directory + token email",
        "env": [],
        "todo": "TODO(integration): wire POC lookup to RDAP; email provider (AUDIT:C2/C4)",
    },
    "document_ocr": {
        "kind": "object store + OCR engine",
        "env": [],
        "todo": "TODO(integration): production OCR engine (AUDIT:C4)",
    },
    "website_manual_review": {
        "kind": "human review queue (no crawler in v1)",
        "env": [],
        "todo": None,
    },
}

# Cheap reachability probes (GET, strict timeout). Stub-backed adapters have none.
PROBES = {
    "companies_house": "https://api.company-information.service.gov.uk/search/companies?q=probe&items_per_page=1",
    "gleif": "https://api.gleif.org/api/v1/lei-records?page%5Bsize%5D=1",
    "rir_rdap:arin": "https://rdap.arin.net/registry/entity/PROBE-NONEXISTENT",
    "rir_rdap:ripe": "https://rdap.db.ripe.net/entity/PROBE-NONEXISTENT",
    "rir_rdap:apnic": "https://rdap.apnic.net/entity/PROBE-NONEXISTENT",
    "rir_rdap:lacnic": "https://rdap.lacnic.net/rdap/entity/PROBE-NONEXISTENT",
    "rir_rdap:afrinic": "https://rdap.afrinic.net/rdap/entity/PROBE-NONEXISTENT",
}


def _classify(adapter_id: str, adapter: object) -> tuple[str, str]:
    """→ (status, detail). Status ∈ live | stub | dev | needs-config | manual."""
    if adapter_id == "website_manual_review":
        return "manual", "by design — reviewers complete tasks via the queue"
    if adapter_id == "email_verification":
        return "live", "reads platform-delivered verification state"
    if adapter_id == "companies_house":
        return (
            ("live", "API key present")
            if os.environ.get("CH_API_KEY")
            else ("needs-config", "CH_API_KEY not set — requests will be unauthenticated")
        )
    if adapter_id == "gleif":
        return "live", "public API, no key required"
    if adapter_id == "floqer_company_enrichment":
        client = getattr(adapter, "client", None)
        if type(client).__name__ == "FixtureFloqerClient":
            return "stub", "FixtureFloqerClient — returns canned records only"
        return "live", type(client).__name__
    if adapter_id == "rir_rdap":
        strategies = getattr(adapter, "strategies", {})
        names = {type(s).__name__ for s in strategies.values()}
        if names == {"FixtureRirStrategy"} or not strategies:
            return "stub", "fixture strategies only"
        return "live", f"{len(strategies)} RIR strategies: {', '.join(sorted(strategies))}"
    if adapter_id == "rir_poc":
        directory = getattr(adapter, "directory", None)
        if type(directory).__name__ == "FixturePocDirectory":
            records = getattr(directory, "records", {})
            return "stub", f"FixturePocDirectory ({len(records)} canned records)"
        return "live", type(directory).__name__
    if adapter_id == "document_ocr":
        engine = getattr(adapter, "engine", None)
        if type(engine).__name__ == "JsonScanOcrEngine":
            return "dev", "JsonScanOcrEngine — parses JSON 'scans', not real OCR"
        return "live", type(engine).__name__
    return "live", type(adapter).__name__


def integration_report(settings: Settings, session: Session, adapters: dict) -> dict:
    # `adapters` is the worker registry, built and cached once by the caller.
    # Rebuilding it per request leaked an httpx client pool per adapter; this
    # function only reads their types for classification.
    stats = {
        row.adapter_id: {
            "calls": row.calls,
            "error_rate": float(row.error_rate or 0),
            "p95_ms": float(row.p95_ms or 0),
            "last_call_at": row.last_call_at.isoformat() if row.last_call_at else None,
        }
        for row in session.execute(
            text(
                """
                SELECT adapter_id, count(*) AS calls,
                       avg((status = 'upstream_error')::int) AS error_rate,
                       percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms) AS p95_ms,
                       max(fetched_at) AS last_call_at
                FROM adapter_results GROUP BY adapter_id
                """
            )
        )
    }

    active = configuration_repo.get_active(session)
    broker = (
        session.execute(
            text(
                """
            SELECT count(*) AS total,
                   count(*) FILTER (WHERE cardinality(aliases) + cardinality(domains)
                       + cardinality(email_domains) + cardinality(org_ids)
                       + cardinality(poc_handles) + cardinality(asns) > 0) AS with_identifiers
            FROM broker_entities
            """
            )
        ).one()
        if active is None
        else None
    )
    total = len(active.brokers) if active else broker.total
    with_identifiers = (
        sum(
            any((b.aliases, b.domains, b.email_domains, b.org_ids, b.poc_handles, b.asns))
            for b in active.brokers
        )
        if active
        else broker.with_identifiers
    )

    rows = []
    for adapter_id in WIRING:
        adapter = adapters.get(adapter_id)
        if adapter_id == "broker_policy":
            status, detail = (
                "live",
                (
                    f"{total} entities, {with_identifiers} with identifiers beyond name; "
                    + (
                        f"versioned configuration revision {active.revision}"
                        if active
                        else "legacy/bootstrap source"
                    )
                ),
            )
        elif adapter is None:
            status, detail = "missing", "not in the worker registry"
        else:
            status, detail = _classify(adapter_id, adapter)
        wiring = WIRING[adapter_id]
        rows.append(
            {
                "adapter_id": adapter_id,
                "kind": "internal gate (versioned configuration)"
                if adapter_id == "broker_policy" and active
                else wiring["kind"],
                "status": status,
                "detail": detail,
                "env": [
                    {"name": e.split(" ")[0], "present": bool(os.environ.get(e.split(" ")[0])), "note": e}
                    for e in wiring["env"]
                ],
                "todo": None if adapter_id == "broker_policy" and active else wiring["todo"],
                "probeable": adapter_id in PROBES or adapter_id == "rir_rdap",
                "stats": stats.get(adapter_id),
            }
        )
        if adapter_id == "broker_policy":
            rows[-1].update(
                total=total,
                with_identifiers=with_identifiers,
                configuration_revision=str(active.revision) if active else None,
            )

    callback_configured = bool(
        settings.platform_hmac_secret
        and settings.platform_callback_url
        and "localhost:9999" not in settings.platform_callback_url
    )
    platform = {
        "callback_url_set": bool(settings.platform_callback_url),
        "callback_url_is_default_stub": "localhost:9999" in settings.platform_callback_url,
        "hmac_secret_set": bool(settings.platform_hmac_secret),
        "auth_disabled": settings.auth_disabled,
        "status": "live" if callback_configured else "needs-config",
        "todo": None
        if callback_configured
        else "set KYC_PLATFORM_CALLBACK_URL + KYC_PLATFORM_HMAC_SECRET (AUDIT:C4)",
    }

    return {
        "adapters": rows,
        "platform_callback": platform,
        "object_store": {"kind": settings.object_store, "root": str(settings.object_store_root)},
        "email_sender": {
            "status": "stub",
            "detail": "LoggingEmailSender — POC token emails are logged, not sent",
            "todo": "TODO(integration): outbound email provider (AUDIT:C4)",
        },
    }


def probe(adapter_id: str) -> dict:
    """Reachability check against the real upstream(s). 4xx still proves
    reachability (404 on a nonexistent handle, 401 without a key)."""
    targets = (
        {k.split(":", 1)[1]: v for k, v in PROBES.items() if k.startswith("rir_rdap:")}
        if adapter_id == "rir_rdap"
        else ({adapter_id: PROBES[adapter_id]} if adapter_id in PROBES else {})
    )
    if not targets:
        return {"adapter_id": adapter_id, "probeable": False, "note": "stub/internal — nothing to probe"}

    results = {}
    with httpx.Client(timeout=6.0, follow_redirects=True) as client:
        for name, url in targets.items():
            started = time.monotonic()
            try:
                auth = None
                if adapter_id == "companies_house" and os.environ.get("CH_API_KEY"):
                    auth = (os.environ["CH_API_KEY"], "")
                response = client.get(url, auth=auth)
                results[name] = {
                    "reachable": True,
                    "status_code": response.status_code,
                    "latency_ms": int((time.monotonic() - started) * 1000),
                    "note": "auth required (set the API key)"
                    if response.status_code in (401, 403)
                    else ("reachable" if response.status_code < 500 else "upstream error"),
                }
            except Exception as exc:  # noqa: BLE001 — probes report, never raise
                results[name] = {
                    "reachable": False,
                    "error": f"{type(exc).__name__}: {exc}",
                    "latency_ms": int((time.monotonic() - started) * 1000),
                }
    return {"adapter_id": adapter_id, "probeable": True, "targets": results}

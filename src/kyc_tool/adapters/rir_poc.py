"""rir_poc adapter — approval-grade, automated + user token step.

Confirms the POC handle's association and extracts the RIR-LISTED email (the
token is never sent to a user-submitted address). The RIR directory lookup is
behind an interface: Phase 2 ships the fixture-backed implementation; Phase 3
wires it to the live RDAP clients. Token creation/sending is an orchestration
side effect described in `normalized` — adapters never touch the database.
"""

import json
from typing import Protocol

from kyc_tool.adapters.base import AdapterOutput, hash_inputs
from kyc_tool.domain.models import AdapterStatus
from kyc_tool.validators.normalize import canon_id


class PocDirectory(Protocol):
    def lookup(self, rir: str, poc_handle: str) -> dict:
        """→ {found: bool, associated_org_handles: [..], resources: [..],
        rir_listed_email: str|None} (resources optional)."""
        ...


class FixturePocDirectory:
    """Test/dev directory backed by a dict (or recorded JSON)."""

    def __init__(self, records: dict[str, dict]) -> None:
        self.records = records  # key: f"{rir}:{poc_handle}"

    def lookup(self, rir: str, poc_handle: str) -> dict:
        return self.records.get(
            f"{rir}:{poc_handle}",
            {
                "found": False,
                "associated_org_handles": [],
                "resources": [],
                "rir_listed_email": None,
            },
        )


class RirPocAdapter:
    adapter_id = "rir_poc"

    def __init__(self, directory: PocDirectory) -> None:
        self.directory = directory

    def input_hash(self, case_snapshot: dict, event: dict) -> str:
        poc = case_snapshot.get("poc") or {}
        return hash_inputs(self.adapter_id, poc.get("rir"), poc.get("poc_handle"))

    def run(self, case_snapshot: dict, event: dict) -> AdapterOutput:
        poc = case_snapshot.get("poc") or {}
        if not poc.get("poc_handle"):
            return AdapterOutput(self.adapter_id, AdapterStatus.NOT_APPLICABLE)

        record = self.directory.lookup(poc.get("rir", ""), poc["poc_handle"])

        # Fail-closed association (remediation item 3): the POC must be tied to
        # at least one VERIFIED target — the submitted ORG-ID appearing in the
        # directory's associations, or the submitted resource appearing in its
        # holdings. A submission with neither target, or a directory record
        # confirming neither, is NOT associated (the old rule treated a missing
        # submitted org as associated-by-default).
        submitted_org = poc.get("org_handle")
        submitted_resource = (poc.get("resource") or "").strip()
        directory_orgs = {canon_id(h) for h in record.get("associated_org_handles", [])}
        directory_resources = {
            str(r).strip().lower() for r in record.get("resources", []) if str(r).strip()
        }
        org_associated = bool(canon_id(submitted_org)) and canon_id(submitted_org) in directory_orgs
        resource_associated = (
            bool(submitted_resource) and submitted_resource.lower() in directory_resources
        )
        associated = bool(record.get("found")) and (org_associated or resource_associated)

        normalized = {
            "poc_handle": poc["poc_handle"],
            "rir": poc.get("rir"),
            "org_handle": submitted_org,
            # the identity the minted token is bound to (validator re-checks these
            # against the current snapshot, so a later POC change invalidates it)
            "resource": submitted_resource or None,
            "found": bool(record.get("found")),
            "associated": associated,
            "association_target": {
                "org_handle": submitted_org if org_associated else None,
                "resource": submitted_resource if resource_associated else None,
            },
            "rir_listed_email": record.get("rir_listed_email"),
            # side-effect requests for orchestration:
            "send_token": associated and bool(record.get("rir_listed_email")),
            "email_unavailable": associated and not record.get("rir_listed_email"),
        }
        return AdapterOutput(
            self.adapter_id,
            AdapterStatus.OK,
            raw=json.dumps(record, default=str).encode(),
            normalized=normalized,
        )

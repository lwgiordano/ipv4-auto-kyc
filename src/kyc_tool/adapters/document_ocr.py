"""document_ocr adapter — legal proof, automated.

Trigger: document.uploaded (payload carries the object-storage ref the
platform wrote). The adapter's "upstream" is the object store + OCR engine;
field matching stays in the validator.
"""

import json

from kyc_tool.adapters.base import AdapterOutput, hash_inputs
from kyc_tool.adapters.ocr import OcrEngine
from kyc_tool.domain.models import AdapterStatus
from kyc_tool.storage.object_store import ObjectStore


class DocumentOcrAdapter:
    adapter_id = "document_ocr"

    def __init__(self, store: ObjectStore, engine: OcrEngine) -> None:
        self.store = store
        self.engine = engine

    def _latest_document(self, case_snapshot: dict, event: dict) -> dict | None:
        if event.get("event_type") == "document.uploaded":
            return event.get("payload") or None
        documents = case_snapshot.get("documents") or []
        return documents[-1] if documents else None

    def input_hash(self, case_snapshot: dict, event: dict) -> str:
        doc = self._latest_document(case_snapshot, event)
        return hash_inputs(self.adapter_id, (doc or {}).get("object_ref"))

    def run(self, case_snapshot: dict, event: dict) -> AdapterOutput:
        doc = self._latest_document(case_snapshot, event)
        if not doc:
            return AdapterOutput(self.adapter_id, AdapterStatus.NOT_APPLICABLE)

        data = self.store.get(doc["object_ref"])
        extracted = self.engine.extract(data, doc.get("doc_type", "unknown"))
        return AdapterOutput(
            self.adapter_id,
            AdapterStatus.OK,
            raw=json.dumps({"object_ref": doc["object_ref"], "extracted": extracted}).encode(),
            normalized={
                "extracted": extracted,
                "object_ref": doc["object_ref"],
                "doc_type": doc.get("doc_type"),
            },
        )

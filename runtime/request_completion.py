"""One completion decision for registration, reselection and terminal delivery.

Observations remain in ObservationStore. This module owns no facts, clocks,
OCR, host IO or task queue. A newer serial is not a semantic answer advance.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass


@dataclass(frozen=True)
class RequestCompletion:
    confirmed: frozenset[str]
    missing: frozenset[str]
    facts_ready: bool
    service_ready: bool
    can_wait: bool
    state: str
    progress_key: str

    def public(self, *, body_complete=None, submitted=None, answer_complete=None):
        return {"facts_ready": self.facts_ready, "service_ready": self.service_ready,
                "can_wait_for_evidence": self.can_wait, "state": self.state,
                "confirmed_fields": sorted(self.confirmed), "missing_fields": sorted(self.missing),
                "progress_key": self.progress_key, "body_complete": body_complete,
                "submitted": submitted, "answer_complete": answer_complete,
                "delivered_complete": (self.service_ready and body_complete and submitted and answer_complete is not False)
                                      if body_complete is not None and submitted is not None else None,
                "ack_scope": "plugin_payload_or_sdk_submission_not_host_speech"}


def evaluate_request(plan, visual, *, completion=None):
    """Use ONE selected packet and its assessment, without weakening field gates.

    completion is build_guidance(..., visual=THIS_packet)['completion'], not a
    role/cached assessment borrowed from another observation. Terminal missing
    rules/subject is different from evidence which can still complete a task.
    """
    from .query_coordinator import navigation_applicable, satisfied_fields

    confirmed = satisfied_fields(plan, visual)
    missing = plan.fields - confirmed
    facts_ready = not missing
    assessment = completion or {}
    requested = bool(assessment.get("requested"))
    service_ready = facts_ready and (not requested or assessment.get("evaluation_complete") is True)
    state = ("complete" if service_ready else str(assessment.get("state") or "needs_evidence")
             if requested else "needs_evidence")
    can_wait = bool(plan.screen and plan.fields and not service_ready
                    and navigation_applicable(plan, visual)
                    and (not requested or assessment.get("can_wait_for_evidence") is True))
    # IDs/values/semantic status matter; capture time, OCR geometry, confidence,
    # serial and proof bookkeeping cannot create another speech entitlement.
    facts = plan.relevant_facts((visual.get("screen_facts") or {}).get("facts") or []) if visual.get("fresh") else []
    semantic = {
        "page": visual.get("page_type") if visual.get("fresh") else "unknown",
        "confirmed": sorted(confirmed), "state": state,
        "facts": sorted({json.dumps({k: f.get(k) for k in (
            "kind", "scope", "entity_id", "entity_type", "label", "value")}, sort_keys=True, default=str)
            for f in facts}),
        "characters": sorted({(str(c.get("entity_id") or c.get("name") or ""),
                               str(c.get("identity_scope") or ""))
                              for c in visual.get("characters") or []})
                      if plan.fields & {"attributes", "characters", "controlled_character"} else [],
        "text": [r.get("text") for r in (visual.get("screen_facts") or {}).get("visible_text") or []]
                if "visible_text" in plan.fields else [],
        "subject_ids": sorted(assessment.get("subject_ids") or []),
        "panel_subject_state": assessment.get("panel_subject_state"),
        "evaluation_complete": assessment.get("evaluation_complete"),
    }
    key = hashlib.sha256(json.dumps(semantic, sort_keys=True, ensure_ascii=True, default=str).encode()).hexdigest()
    return RequestCompletion(frozenset(confirmed), frozenset(missing), facts_ready,
                             bool(service_ready), can_wait, state, key)

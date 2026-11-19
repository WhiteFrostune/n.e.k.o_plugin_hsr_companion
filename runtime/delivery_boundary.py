"""Cheap revocation of ONE already-admitted packet; no OCR, IO or new facts.

Proof geometry remains upstream and in the returned source packet. Geometry
is not semantic identity across producer revisions. Never merge equipment or
copy newly discovered values from a newer, independently owned observation.
"""
from __future__ import annotations

import copy
import hashlib
import json
from collections import Counter

FACT_KEYS = ("kind", "scope", "label", "value", "entity_id", "entity_type", "property", "unit",
             "subject_id", "subject_entity_id", "slot")


def semantic_key(row):
    return json.dumps({k: row[k] for k in FACT_KEYS if k in row}, sort_keys=True, ensure_ascii=True)


def fact_packet_digest(value):
    """Bind the review receipt to these exact facts, not only their boxes."""
    facts = (value.get("screen_facts") or {}).get("facts") or []
    return hashlib.sha256(json.dumps(sorted(semantic_key(f) for f in facts)).encode()).hexdigest()


def equipment_key(value):
    facts = (value.get("screen_facts") or {}).get("facts") or []
    return tuple(sorted(semantic_key(f) for f in facts if f.get("scope") == "equipment"))


def fact_field(fact):
    kind = fact.get("kind")
    if kind == "current_location":
        return "location"
    if kind in {"tracked_quest", "quest_objective", "quest_destination"}:
        return "quest"
    return fact.get("scope")


def page_key(value):
    # Public scene has only page semantics, not OCR confidence/box provenance.
    return json.dumps({k: value.get(k) for k in ("page_type", "scene")}, sort_keys=True, ensure_ascii=True)


def represented_fields(value):
    return {"page", "visible_text", "characters", "controlled_character",
            *(fact_field(f) for f in (value.get("screen_facts") or {}).get("facts") or [])} - {None}


def review(snapshot, owner, *, generation=None):
    """Return a detached, possibly partial subset of this packet, with reasons.

    Current raw data is used ONLY to detect replacement/conflict. It cannot
    contribute a new row or extend a capture/verification timestamp.
    """
    from .equipment_view import equipment_card
    from .observation import public_visual_evidence

    selected = snapshot.evidence()
    observer = getattr(owner, "_observer_status", {})
    serial = getattr(owner, "_observation_serial", 0)
    reuse = selected.get("reuse") or {}
    metadata = {"state": "retained", "reason": "compatible", "source_serial": snapshot.serial,
                "current_serial": serial, "withheld_fields": [], "source_epoch": snapshot.epoch,
                "current_epoch": getattr(owner, "_observation_epoch", 0),
                "invalidated": bool(getattr(owner, "_observation_invalidated", False))}

    def reject(reason):
        empty = public_visual_evidence({"invalidated": True, "source": "assistant_local_ocr"}, observer)
        empty["delivery_boundary"] = {**metadata, "state": "rejected", "reason": reason,
                                      "withheld_fields": sorted(snapshot._fields or represented_fields(selected))}
        return empty

    if snapshot.epoch != getattr(owner, "_observation_epoch", 0) or getattr(owner, "_observation_invalidated", False):
        return reject("epoch_changed")
    expected_generation = generation if generation is not None else reuse.get("answer_generation", snapshot._generation)
    if expected_generation is not None and expected_generation != getattr(owner, "_answer_query_generation", 0):
        return reject("new_question")
    failure_revision = reuse.get("capture_failure_revision", snapshot._capture_revision)
    if failure_revision is not None and failure_revision != getattr(owner, "_capture_failure_revision", 0):
        return reject("capture_failed")
    if (getattr(owner, "_observer_stop_requested", False) or not observer.get("enabled")
            or observer.get("state") not in {"observing", "updating"}):
        return reject("observer_unavailable")
    if not selected.get("fresh"):
        return reject("source_unconfirmed")
    pending = getattr(owner, "_observation_pending_reason", "")
    if pending and not (reuse.get("validated_revision") == serial and reuse.get("epoch") == snapshot.epoch):
        return reject("pending_unproved")

    raw = getattr(owner, "_scene_preview", None) or getattr(owner, "_committed_observation", None)
    current = None
    if raw:
        # Shape only, NOT a freshness grant: the selected packet was separately
        # admitted by review_snapshot. A new raw commit cannot mint a proof here.
        from .fact_lifecycle import candidate_evidence
        current = candidate_evidence(raw)
        if page_key(current) != page_key(selected):
            return reject("page_changed")
    elif serial != snapshot.serial:
        return reject("revision_unproved")
    if not snapshot._fields and serial != snapshot.serial:
        return reject("unscoped_revision")

    challenged = set()
    deferred = getattr(owner, "_deferred_verification", None)
    if deferred and deferred[:2] == (snapshot.epoch, serial):
        challenged = set(deferred[2])
        if (snapshot.serial == serial and reuse.get("validated_revision") == serial
                and reuse.get("challenge_revision") == getattr(owner, "_review_challenge_serial", 0)):
            challenged -= {tuple(p) for p in reuse.get("reviewed_coordinates", [])}
    from .delivery_scope import coordinates, identity_key, withhold_regions
    page_dependencies = {coordinates(p) for p in getattr(owner, "_accepted_frame_page_regions", ())}
    admitted_controls = reuse.get("page_control_coordinates")
    if admitted_controls is not None and snapshot.serial == serial and reuse.get("validated_revision") == serial:
        # The live reviewer already classified this page using THIS receipt's
        # admitted control subset. A quarantined redundant tab is not a second
        # page verdict. Never grant unrelated/unreviewed controls permission.
        admitted_controls = {tuple(p) for p in admitted_controls}
        reviewed = {tuple(p) for p in reuse.get("reviewed_coordinates", [])}
        if not admitted_controls or not admitted_controls <= reviewed:
            return reject("page_proof_missing")
        page_dependencies = admitted_controls
    if challenged & page_dependencies:
        return reject("page_control_challenged")

    value = copy.deepcopy(selected)
    if challenged:
        source = raw.get("characters", []) if raw and snapshot.serial == serial else []
        if snapshot.serial == serial:
            value = withhold_regions(value, challenged, identity_proofs=source)
        if current is not None:
            current = withhold_regions(current, challenged, identity_proofs=(raw or {}).get("characters", []))

    original = selected.get("screen_facts") or {}
    screen = value.get("screen_facts") or {}
    withheld = set()
    replaced_equipment = False
    if current is not None:
        if serial != snapshot.serial and snapshot._equipment_key:
            # A producer may fill a missing row without replacing the item.
            # Keep only the OLD admitted subset; never borrow the new row.
            # The baseline includes the whole original item even for main-only
            # queries, so any changed/removed original row still revokes it.
            replaced_equipment = bool(Counter(snapshot._equipment_key) - Counter(equipment_key(current)))
        current_facts = Counter(semantic_key(f) for f in (current.get("screen_facts") or {}).get("facts") or [])
        # A scoped live review happened AFTER this producer revision existed.
        # That same revision's partial/empty parser output cannot veto facts
        # whose own literal regions were just admitted against the new frame.
        # A later revision/challenge still revokes normally; no row is borrowed.
        reviewed_fields = set(reuse.get("reviewed_fields") or [])
        reviewed_regions = {tuple(p) for p in reuse.get("reviewed_coordinates", [])}
        same_reviewed_revision = (snapshot.serial == serial == reuse.get("validated_revision")
                                  and snapshot.epoch == reuse.get("epoch")
                                  and reuse.get("fact_packet_digest") == fact_packet_digest(selected))
        retained = []
        for fact in screen.get("facts") or []:
            field = fact_field(fact)
            key = semantic_key(fact)
            proofs = [p for p in fact.get("evidence") or [] if p.get("validation_scope") != "control_presence"]
            live_reviewed = (same_reviewed_revision and field in reviewed_fields and bool(proofs)
                             and all(p.get("region") and coordinates(p) in reviewed_regions for p in proofs))
            if (replaced_equipment and field == "equipment") or not (current_facts[key] or live_reviewed):
                withheld.add(field)
            else:
                if current_facts[key]:
                    current_facts[key] -= 1
                retained.append(fact)
        screen["facts"] = retained
        # Text is unstructured; equipment replacement invalidates ALL of its
        # old prose rather than leaking old stats outside the equipment card.
        text_counts = Counter(r.get("text") for r in (current.get("screen_facts") or {}).get("visible_text") or [])
        rows = []
        for row in screen.get("visible_text") or []:
            if replaced_equipment or not text_counts[row.get("text")]:
                withheld.add("visible_text")
            else:
                text_counts[row.get("text")] -= 1
                rows.append(row)
        screen["visible_text"] = rows
        actors = {identity_key(a) for a in current.get("characters") or []}
        value["characters"] = [a for a in value.get("characters") or [] if identity_key(a) in actors]
        if identity_key(value.get("controlled_character") or {}) != identity_key(current.get("controlled_character") or {}):
            value["controlled_character"] = None

    old_facts = Counter(semantic_key(f) for f in original.get("facts") or [])
    new_facts = Counter(semantic_key(f) for f in screen.get("facts") or [])
    for fact in original.get("facts") or []:
        if old_facts[semantic_key(fact)] > new_facts[semantic_key(fact)]:
            withheld.add(fact_field(fact))
    if screen.get("visible_text") != original.get("visible_text"):
        withheld.add("visible_text")
    if value.get("characters") != selected.get("characters"):
        withheld.add("characters")
        value.update(team=[], identity_list_complete=False)
    if value.get("controlled_character") != selected.get("controlled_character"):
        withheld.add("controlled_character")
        value["controlled_character_status"] = "selected_slot_unconfirmed"
    value["can_answer_character_identity"] = bool(value.get("characters"))
    screen["unclassified_text"] = []
    if withheld:
        checks = [*screen.get("field_checks", []),
                  *[{"scope": f, "kind": "delivery_boundary", "state": "pending", "reason": "field_revoked"}
                    for f in sorted(withheld - {None})]]
        screen["field_checks"] = checks[-96:]
        value["text_coverage_complete"] = False
    value["screen_facts"] = screen
    screen["equipment_card"] = equipment_card(value)
    value["delivery_boundary"] = {**metadata, "state": "partial" if withheld else "retained",
        "reason": "object_replaced" if replaced_equipment else "fields_revoked" if withheld else "compatible",
        "withheld_fields": sorted(withheld - {None})}
    return value

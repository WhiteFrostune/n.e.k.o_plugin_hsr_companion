"""Project answer dependencies AND their guards together; never authorize extras."""
from __future__ import annotations

import copy
from dataclasses import replace

from ..adapters.text_verification import identity_literal
from .equipment import equipment_card


def coordinates(item):
    region = item.get("region") or {}
    return tuple(region.get(k, -1) for k in ("left", "top", "right", "bottom"))


def project_evidence(evidence, plan):
    if plan is None:
        return copy.deepcopy(evidence)
    value = copy.deepcopy(evidence)
    screen = value.get("screen_facts") or {}
    facts = plan.relevant_facts(screen.get("facts") or []) if plan.topics else []
    value["screen_facts"] = {**screen, "facts": facts,
        "visible_text": screen.get("visible_text", []) if "visible_text" in plan.fields else [],
        "unclassified_text": [], "field_checks": [c for c in screen.get("field_checks", [])
            if c.get("scope") in plan.topics or c.get("kind") in {f.get("kind") for f in facts}]}
    value["screen_facts"]["equipment_card"] = equipment_card(value)
    if not plan.fields & {"characters", "controlled_character"}:
        # A panel owner is a dependency, not an unsolicited roster. Its own
        # identity proof must survive together with the numeric field proof.
        subjects = [a for a in value.get("characters") or [] if a.get("identity_scope") == "detail_subject"] if "attributes" in plan.fields else []
        value.update(characters=subjects, team=[], controlled_character=None, can_answer_character_identity=False,
                     identity_list_complete=False)
    value["text_coverage_complete"] = False
    return value


def identity_key(actor):
    return tuple(actor.get(k) for k in ("entity_id", "name", "identity_scope", "slot"))


def proof_rejected(owner, reason):
    from .live_refresh import trace_refresh
    trace_refresh(owner, "proof_rejected", reason=reason)
    return None


def _identity_proofs(owner, evidence, actors, proof_source):
    """Join sanitized names to INTERNAL geometry from this exact revision.

    Retained cache entries carry their own proofs. Never borrow the current
    actor's coordinates to authorize an older same-name or same-slot packet.
    """
    if not actors:
        return []
    serial = (evidence.get("screen_facts") or {}).get("observation_serial")
    if proof_source is None:
        raw = getattr(owner, "_committed_observation", None) or {}
        if serial != raw.get("observation_serial"):
            return proof_rejected(owner, "identity_revision_mismatch")
        if not getattr(owner, "_accepted_frame_identity_guarded", False):
            return proof_rejected(owner, "identity_not_admitted")
        candidates = raw.get("characters") or []
    else:
        if (proof_source.get("epoch") != getattr(owner, "_observation_epoch", 0)
                or serial != ((proof_source.get("evidence") or {}).get("screen_facts") or {}).get("observation_serial")):
            return proof_rejected(owner, "identity_revision_mismatch")
        candidates = proof_source.get("identity_proofs") or []
    selected = []
    for actor in actors:
        matches = {coordinates(c): c for c in candidates
                   if identity_key(c) == identity_key(actor) and c.get("region")}
        if len(matches) != 1:
            return proof_rejected(owner, "identity_proof_missing" if not matches else "identity_proof_ambiguous")
        actor_proof = next(iter(matches.values()))
        literal = identity_literal(actor_proof)
        selected.append({**actor_proof, **literal} if literal else actor_proof)
    return selected


def scoped_guard(owner, evidence, plan, guards, *, proof_source=None):
    """None means missing coverage, not an empty successful pixel check."""
    projected = project_evidence(evidence, plan)
    controls = list(proof_source.get("page_controls", ()) if proof_source is not None
                    else getattr(owner, "_accepted_frame_page_regions", ()))
    # Controls and public OCR rows round geometry differently. Bind only
    # matching boxes (five-decimal public precision), never nearest text.
    rows_by_box = {}
    for row in (evidence.get("screen_facts") or {}).get("visible_text") or []:
        key = tuple(round(float(v), 5) for v in coordinates(row))
        rows_by_box.setdefault(key, []).append(row)
    enriched = []
    for control in controls:
        matches = rows_by_box.get(tuple(round(float(v), 5) for v in coordinates(control)), [])
        texts = {row.get("text") for row in matches if row.get("text")}
        enriched.append({**control, "text": next(iter(texts)), "control": True} if len(texts) == 1 else control)
    controls = enriched
    facts = (projected.get("screen_facts") or {}).get("facts") or []
    proofs = [p for f in facts for p in f.get("evidence") or [] if p.get("validation_scope") != "control_presence"]
    # A returned value without literal geometry cannot cross a scoped handoff.
    if not controls:
        from .page_control_trace import record_controls
        trace_evidence = {**evidence, "recognition": {"control_trace": (proof_source or {}).get("control_trace") or
            ((getattr(owner, "_committed_observation", None) or {}).get("recognition") or {}).get("control_trace") or {}}}
        record_controls(owner, "delivery_rejected", analysis=trace_evidence, controls=controls,
                        origin="snapshot" if proof_source is not None else "committed")
        return proof_rejected(owner, "page_controls_missing")
    if (any(not any(p.get("validation_scope") != "control_presence" for p in f.get("evidence") or [])
                           for f in facts) or any(not p.get("region") for p in proofs)):
        return proof_rejected(owner, "literal_geometry_missing")
    rows = (projected.get("screen_facts") or {}).get("visible_text") or []
    actors = projected.get("characters") or []
    actors = _identity_proofs(owner, evidence, actors, proof_source)
    if actors is None:
        if plan.fields & {"characters", "controlled_character"}:
            return None
        # Missing ownership proof withholds only ownership, never certifies
        # the declared role and never destroys independently proved numbers.
        projected["characters"] = []
        actors = []
    # Controls are checked separately as controls, but still retain strict
    # native guards. A tiny changed heading must not authorize the old page.
    wanted = [*controls, *proofs, *rows, *actors]
    if any(not item.get("region") for item in wanted):
        return proof_rejected(owner, "literal_geometry_missing")
    pairs = {}
    for guard in guards:
        if guard:
            pairs.update({patch[0]: (item, patch) for item, patch in zip(*guard)})
    selected = list({coordinates(item): item for item in wanted}.values())
    if not selected or any(coordinates(item) not in pairs for item in [*controls, *selected]):
        return proof_rejected(owner, "guard_coverage_missing")
    return (projected, ([item for item in selected], tuple(pairs[coordinates(item)][1] for item in selected)))


def select_scoped_guard(owner, evidence, plan, guards, *, proof_source=None, allow_partial=False):
    """Strict checks per dependency; failed roles cannot erase a proven page.

    Public data and pixel guard are ALWAYS projected together. This fallback
    never authorizes an unproven field and never skips shared page controls.
    """
    selected = scoped_guard(owner, evidence, plan, guards, proof_source=proof_source)
    if selected is not None or not allow_partial:
        return selected
    admitted = set()
    for field in plan.fields:
        part = replace(plan, fields=frozenset({field}), topics=plan.topics & {field})
        if scoped_guard(owner, evidence, part, guards, proof_source=proof_source) is not None:
            admitted.add(field)
    if not admitted or admitted == set(plan.fields):
        return None
    projected_plan = replace(plan, fields=frozenset(admitted), topics=plan.topics & admitted)
    result = scoped_guard(owner, evidence, projected_plan, guards, proof_source=proof_source)
    if result is not None:
        from .live_refresh import trace_refresh
        trace_refresh(owner, "delivery_partial", missing_fields=sorted(plan.fields - admitted))
    return result


def update_checked_patches(owner, patches, *, selected_snapshot=None):
    """Update ONLY checked coordinates. Never advance time or whole-frame proof."""
    mapped = {p[0]: p for p in patches}

    def update(guard):
        return (guard[0], tuple(mapped.get(p[0], p) for p in guard[1])) if guard else guard

    selected_serial = (((selected_snapshot or {}).get("evidence") or {}).get("screen_facts") or {}).get("observation_serial")
    owns_current = (selected_snapshot is None or selected_snapshot.get("epoch") == getattr(owner, "_observation_epoch", 0)
                    and selected_serial == getattr(owner, "_observation_serial", 0))
    if owns_current:
        owner._accepted_frame_delivery_guard = update(getattr(owner, "_accepted_frame_delivery_guard", None))
        owner._accepted_frame_query_guard = update(getattr(owner, "_accepted_frame_query_guard", None))
        owner._accepted_frame_full_query_guard = update(getattr(owner, "_accepted_frame_full_query_guard", None))
    for snapshot in getattr(owner, "_reusable_observations", {}).values():
        serial = ((snapshot.get("evidence") or {}).get("screen_facts") or {}).get("observation_serial")
        if snapshot is selected_snapshot or (owns_current and snapshot.get("epoch") == getattr(owner, "_observation_epoch", 0)
                and serial == getattr(owner, "_observation_serial", 0)):
            snapshot["guard"] = update(snapshot.get("guard"))


def withhold_regions(value, regions, *, identity_proofs=()):
    """Read-side quarantine, not deletion of the committed/displayed observation."""
    value = copy.deepcopy(value)
    withheld_keys = {identity_key(p) for p in identity_proofs if coordinates(p) in regions}
    value["characters"] = [a for a in value.get("characters") or [] if identity_key(a) not in withheld_keys]
    controlled = value.get("controlled_character")
    if controlled and identity_key(controlled) in withheld_keys:
        value["controlled_character"] = None
        value["controlled_character_status"] = "selected_slot_unconfirmed"
    value["can_answer_character_identity"] = bool(value["characters"])
    if withheld_keys:
        value["team"] = []
        value["identity_list_complete"] = False
    screen = value.get("screen_facts") or {}
    affected = [f for f in screen.get("facts") or [] if any(coordinates(p) in regions
        for p in f.get("evidence") or [] if p.get("validation_scope") != "control_presence")]
    value["screen_facts"] = {**screen, "facts": [f for f in screen.get("facts") or [] if f not in affected],
        "visible_text": [r for r in screen.get("visible_text") or [] if coordinates(r) not in regions],
        "unclassified_text": [], "field_checks": [*screen.get("field_checks", []),
            *[{"kind": f.get("kind"), "scope": f.get("scope"), "state": "pending", "reason": "verification_pending"}
              for f in affected]][-96:]}
    value["text_coverage_complete"] = False
    value["screen_facts"]["equipment_card"] = equipment_card(value)
    return value

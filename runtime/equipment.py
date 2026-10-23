"""Frame-local answer cards and sourced, bounded equipment assessments.

No OCR, profile writes, damage simulator or generated scoring thresholds.
The exact observed row and the recommended property are separate fields.
"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata

PARTS = {"头部", "手部", "躯干", "脚部", "位面球", "连结绳"}
NUMBER = re.compile(r"\+?\d[\d,]*(?:\.\d+)?%?")


def property_name(label, value):
    name = str(label).split("·")[-1].strip()
    if name in {"攻击力", "防御力", "生命值"} and "%" in unicodedata.normalize("NFKC", str(value)):
        return name + "百分比"
    return name


def _hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def equipment_digest(card):
    """Digest the actual rows, not the caller's possibly stale digest field."""
    return _hash({key: card.get(key) for key in (
        "name", "slot", "level", "sets", "main_stat", "sub_stats", "state", "complete", "pending", "sub_stats_complete")})


def equipment_exact_text(card):
    """Deterministic rendering; useful to audit text vs structured rows too."""
    fragments = [str(card[s]) for s in ("name", "slot", "level") if card.get(s)]
    main, subs = card.get("main_stat"), card.get("sub_stats") or []
    if main:
        fragments.append("主词条：" + main["label"] + main["value"])
    if subs:
        fragments.append("已读副词条：" + "、".join(s["label"] + s["value"] for s in subs))
    if card.get("state") != "confirmed":
        fragments.append("字段冲突，不能转述成完整装备" if card.get("state") == "conflict" else "未读全，不补齐缺失词条")
    return "；".join(fragments)


def _rows(facts, kind):
    rows = []
    for fact in facts:
        if fact.get("kind") != kind:
            continue
        label, value = str(fact.get("label") or "").split("·")[-1], str(fact.get("value") or "")
        if not label or not NUMBER.fullmatch(unicodedata.normalize("NFKC", value).strip()):
            continue
        row = {"label": label, "value": value, "property": property_name(label, value)}
        if row not in rows:
            rows.append(row)
    return rows


def _pending_stats(screen):
    pending = {c.get("kind") for c in screen.get("field_checks") or []
               if c.get("scope") == "equipment" and c.get("state") == "pending"}
    unreadable = set()
    for fact in screen.get("facts") or []:
        if fact.get("scope") != "equipment":
            continue
        if fact.get("kind") == "equipment_binding_missing":
            return {"main_stat", "sub_stat"}
        if fact.get("kind") == "unreadable_stat":
            label = str(fact.get("label") or "")
            unreadable.update({"main_stat"} if label.startswith("主词条·") else
                              {"sub_stat"} if label.startswith("副词条·") else {"main_stat", "sub_stat"})
    # Object evidence is shared; its uncertainty still blocks BOTH kinds.
    # An unreadable, explicitly labelled sub-row is not object uncertainty.
    if pending - {"main_stat", "sub_stat", "unreadable_stat"}:
        return {"main_stat", "sub_stat"}
    if "unreadable_stat" in pending and not unreadable:
        return {"main_stat", "sub_stat"}
    return (pending & {"main_stat", "sub_stat"}) | unreadable


def equipment_card(visual):
    """One immutable-by-convention projection of ONE selected observation.

    Always recompute from selected facts: never trust a cached embedded card
    after scoped withdrawal, or merge a previous item's missing rows.
    """
    screen = visual.get("screen_facts") or {}
    facts = [f for f in screen.get("facts") or [] if f.get("scope") == "equipment"] if visual.get("fresh") else []

    def values(kind):
        return list(dict.fromkeys(str(f.get("value") or "") for f in facts if f.get("kind") == kind and f.get("value")))

    names, slots, levels = values("equipment_name"), values("equipment_slot"), values("equipment_level")
    mains, subs = _rows(facts, "main_stat"), _rows(facts, "sub_stat")
    sets = [{"id": f["entity_id"], "label": str(f.get("value") or "")} for f in facts
            if f.get("kind") == "entity_label" and f.get("entity_type") == "relic_set" and f.get("entity_id")]
    sets = list({s["id"]: s for s in sets}.values())
    conflicts = len(names) > 1 or len(slots) > 1 or len(levels) > 1 or len(mains) > 1 or len(subs) > 4
    conflicts |= len({s["property"] for s in subs}) != len(subs)
    conflicts |= bool(mains and any(s["property"] == mains[0]["property"] for s in subs))
    pending = any(c.get("scope") == "equipment" and c.get("state") == "pending" for c in screen.get("field_checks") or [])
    unreadable = any(f.get("kind") in {"unreadable_stat", "equipment_binding_missing"} for f in facts)
    # +15 is the five-star maximum; other levels/rarities need no invented row
    # count. Four observed rows are complete only absent pending/conflicts.
    expected = 4 if levels == ["+15"] else None
    pending_stats = _pending_stats(screen)
    sub_stats_complete = bool(len(subs) == 4 and not conflicts and "sub_stat" not in pending_stats)
    complete = bool(len(mains) == 1 and sub_stats_complete and "main_stat" not in pending_stats)
    main = mains[0] if len(mains) == 1 and not conflicts else None
    state = "conflict" if conflicts else "confirmed" if complete and len(slots) == 1 else "partial" if facts else "unconfirmed"
    payload = {"name": names[0] if len(names) == 1 else None,
               "slot": slots[0] if len(slots) == 1 else None,
               "level": levels[0] if len(levels) == 1 else None,
               "sets": sets, "main_stat": main, "sub_stats": subs if not conflicts else [],
               "state": state, "complete": complete, "pending": pending or unreadable,
               "sub_stats_complete": sub_stats_complete}
    digest = _hash(payload)
    serial, stamp = screen.get("observation_serial", 0), visual.get("observed_at") or screen.get("observed_at") or ""
    return {"schema_version": 1, **payload, "observed_at": stamp, "observation_serial": serial,
            "snapshot_id": f"{serial}@{stamp}:{digest[:16]}", "content_digest": digest,
            "expected_sub_stat_count": expected, "observed_sub_stat_count": len(payload["sub_stats"]),
            "exact_text": equipment_exact_text(payload),
            "instruction": "本卡只描述这个快照。转述词条只用main_stat/sub_stats的原label/value，不用攻略推荐填空，"
                           "不混用此前装备、未读副词条或角色面板。完整性看complete；未知不等于没有。"}


def equipment_readiness(visual, required_kinds):
    """One contract for parsed rows, usable partials and requested completeness.

    A main-only question needs no substat count. Without a proven total, fewer
    than four subs remain useful partial evidence, not an invalid relic or a
    complete inventory. Pending a substat does not revoke an independent main.
    """
    card = equipment_card(visual)
    screen = visual.get("screen_facts") or {}
    pending = _pending_stats(screen) if visual.get("fresh") else set()
    observed = set()
    if card["state"] != "conflict":
        if card["main_stat"]:
            observed.add("main_stat")
        if card["sub_stats"]:
            observed.add("sub_stat")
    confirmed = set()
    if "main_stat" in observed and "main_stat" not in pending:
        confirmed.add("main_stat")
    if "sub_stat" in observed and card["sub_stats_complete"]:
        confirmed.add("sub_stat")
    return {"confirmed": sorted(required_kinds & confirmed), "observed": sorted(required_kinds & observed),
            "missing": sorted(required_kinds - confirmed),
            "row_coverage": {"sub_stat_count": len(card["sub_stats"]),
                             "expected_sub_stat_count": card["expected_sub_stat_count"],
                             "complete": card["complete"], "sub_stats_complete": card["sub_stats_complete"],
                             "pending": card["pending"], "state": card["state"]}}


def equipment_object(visual, card=None):
    """Current relic identity only; a prior name/guide is not a binding.

    Missing numeric rows do not erase an independently confirmed new name.
    Shared object uncertainty does prevent naming or evaluating an old item.
    """
    card = equipment_card(visual) if card is None else card
    screen = visual.get("screen_facts") or {}
    shared_pending = any(c.get("scope") == "equipment" and c.get("state") == "pending"
                         and c.get("kind") not in {"main_stat", "sub_stat", "unreadable_stat"}
                         for c in screen.get("field_checks") or [])
    confirmed = bool(visual.get("fresh") and card["state"] not in {"conflict", "unconfirmed"}
                     and card.get("name") and card.get("slot") and not shared_pending)
    return {"confirmed": confirmed, "name": card["name"] if confirmed else None,
            "slot": card["slot"] if confirmed else None, "snapshot_id": card["snapshot_id"],
            "source": "current_snapshot_only", "previous_object_allowed": False,
            "instruction": "只用本对象名称与部位；未确认时不沿用上一件的名称、套装、词条或评价。"
                           "角色讨论可延续，但不表示装备仍是上一件；参考配装不是眼前对象。"}


def handoff_receipt(card, *, channel):
    """Safe bounded audit, not a transcript or a host playback receipt."""
    return {"channel": channel, "observation_serial": card.get("observation_serial", 0),
            "observed_at": card.get("observed_at"), "content_digest": card.get("content_digest"),
            "state": card.get("state"), "main_stat_present": bool(card.get("main_stat")),
            "sub_stat_count": len(card.get("sub_stats") or []), "complete": bool(card.get("complete")),
            "ack_scope": "plugin_payload_only_not_host_speech"}


def record_handoff(owner, card, *, channel):
    receipt = handoff_receipt(card, channel=channel)
    owner._last_equipment_handoff = receipt
    owner._equipment_handoffs = [*getattr(owner, "_equipment_handoffs", []), receipt][-8:]
    return receipt


def _stat_note(role, prop):
    notes = role.get("stat_notes", {})
    note = notes.get(prop) or notes.get(prop.replace("百分比", ""))
    return dict(note) if note else {"meaning": "本项保留实际读数；尚未收录此角色的专门收益解释。",
                                   "check": "未列优先不等于无用，不据此推荐丢弃、替换或判毕业。"}


def assess_relic(card, role, sources):
    result = {"kind": "equipment", "state": "unconfirmed", "available": False,
              "snapshot_id": card["snapshot_id"], "content_digest": card["content_digest"],
              "complete": False, "quality_score_available": False, "replacement_decided": False,
              "main_stat": None, "sub_stats": [], "sources": sources,
              "limitations": ["词条方向匹配不是强化档次评分，不证明全身循环、最优配装或必须替换"]}
    if not sources or not role or card["state"] in {"unconfirmed", "conflict"} or card["slot"] not in PARTS:
        return result
    expected = role.get("main_stats", {}).get(card["slot"], [])
    if card["slot"] in {"头部", "手部"}:
        expected = ["生命值" if card["slot"] == "头部" else "攻击力"]
    main = card["main_stat"]
    if main and expected:
        result["main_stat"] = {**main, **_stat_note(role, main["property"]), "candidates": list(expected),
                               "verdict": "matches_reviewed_candidate" if main["property"] in expected else "outside_reviewed_candidates"}
    priority = set(role.get("substats") or [])
    result["sub_stats"] = [{**row, **_stat_note(role, row["property"]), "verdict": "priority" if row["property"] in priority else "not_listed"}
                           for row in card["sub_stats"]]
    result["fixed_main_stat"] = card["slot"] in {"头部", "手部"}
    result["fit_summary"] = {"observed_substat_count": len(card["sub_stats"]),
                             "priority_substat_count": sum(r["verdict"] == "priority" for r in result["sub_stats"]),
                             "priority_properties": sorted(priority), "substats_complete": card["sub_stats_complete"],
                             "not_listed_is_not_useless": True, "replacement_comparison_available": False}
    result["available"] = bool(result["main_stat"] or result["sub_stats"])
    result["state"] = "assessed" if result["available"] else "unconfirmed"
    result["complete"] = bool(card["complete"] and result["main_stat"])
    match = result["main_stat"] and result["main_stat"]["verdict"] == "matches_reviewed_candidate"
    result["summary"] = ("主词条在已复核候选内" if match else "主词条不在已复核候选内" if result["main_stat"] else "尚未确认主词条适配")
    result["summary"] += "；已读副词条逐项标注方向，不据此宣称极品、毕业、强化得好或整件非常适合"
    result["conditions"] = list(role.get("principles") or [])
    result["next_steps"] = []
    if result["main_stat"] and not match:
        result["next_steps"].append({"kind": "main_stat_review",
            "action": "先比较这个部位的已复核主词条候选：" + "、".join(expected) + "；未比较替代装备前不宣布必须换。"})
    if not card["complete"]:
        result["next_steps"].append({"kind": "missing_rows", "action": "先补齐这件遗器尚未确认的词条，不从上一件或角色面板补数。"})
    result["next_steps"].append({"kind": "team_conditions", "action": "方向匹配之后，再结合全身属性和队友核对循环；本结果不评价强化档次。"})
    return result


def assess_panel(visual, role, sources):
    facts = (visual.get("screen_facts") or {}).get("facts") or [] if visual.get("fresh") else []
    rows = _rows([f for f in facts if f.get("scope") == "attributes"], "stat")
    # Panel totals are not relic flat/percentage substats. Collapse priorities
    # to their panel property, retaining raw labels/values for the answer.
    priorities = list(dict.fromkeys([s.replace("百分比", "") for s in role.get("substats") or []] + [
        s.replace("百分比", "") for choices in role.get("main_stats", {}).values() for s in choices]))
    labels = {r["label"] for r in rows}
    conflict = len(labels) != len(rows)
    result = {"kind": "attributes", "available": bool(rows and sources and not conflict),
              "state": "conflict" if conflict else "partial" if rows else "unconfirmed",
              "rows": rows if not conflict else [], "priority_properties": priorities,
              "missing_priorities": [p for p in priorities if p not in labels], "sources": sources,
              "complete": False, "graduation_threshold_available": False, "quality_score_available": False,
              "replacement_decided": False,
              "conditions": list(role.get("principles") or []),
              "instruction": "引用属性时保留实际label/value；普通评价按reply_plan只讲重点，明确要求完整/逐项时列全已确认项。"
                             "结合来源解释作用；没有角色/光锥/队友/战斗条件下的阈值，不能判毕业、超标或不够。"
                             "priority_properties只是关注方向，不是每一项都必须达到的毕业清单。"}
    for row in result["rows"]:
        row["verdict"] = "reviewed_priority" if row["label"] in priorities else "observed_only"
        row.update(_stat_note(role, row["label"]))
    result["focus_checks"] = [dict(row) for row in result["rows"] if row["label"] in priorities][:4] if result["available"] else []
    result["next_steps"] = [{"kind": "reviewed_condition", "action": item} for item in (role.get("principles") or [])[:2]] if result["available"] else []
    if result["missing_priorities"] and result["available"]:
        result["next_steps"].append({"kind": "missing_context",
            "action": "未显示的关注属性不等于没有：" + "、".join(result["missing_priorities"]) + "；补齐后再评价对应方向。"})
    return result


def assess_cone(visual, role, sources, catalog):
    facts = (visual.get("screen_facts") or {}).get("facts") or [] if visual.get("fresh") else []
    ids = {f.get("entity_id") for f in facts if f.get("kind") == "entity_label" and f.get("entity_type") == "light_cone"}
    result = {"kind": "light_cone", "available": False, "state": "unconfirmed", "sources": sources,
              "replacement_decided": False, "quality_score_available": False, "complete": False}
    if len(ids) != 1 or not sources:
        return result
    entity_id = next(iter(ids))
    record = catalog.get(entity_id)
    if not record or not record.get("sources") or record.get("path") != role.get("path"):
        return result
    candidate = next((c for c in role.get("alternative_cones") or [] if c["id"] == entity_id), None)
    signature = entity_id == role.get("signature_id")
    result.update(available=True, state="assessed", entity_id=entity_id, name=record["name"],
                  verdict="signature_reference" if signature else "conditional_candidate" if candidate else "not_in_reviewed_candidates",
                  reason=candidate["reason"] if candidate else "已复核的专属参考" if signature else "不在已复核候选内，不据此判不可用",
                  identity_scope="visible_label_not_selection_or_wearer",
                  conditions=[candidate["condition"]] if candidate else list(role.get("principles") or []))
    return result

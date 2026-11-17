"""Deterministic equipment fact views shared across layers.

This is the contracts-side projection of screen_facts into an equipment card.
It never reads OCR, files or player profiles, and it never evaluates fit; the
L4 assess_* functions consume this view.
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

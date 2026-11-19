"""Sourced, bounded equipment assessments (L4 analysis).

No OCR, profile writes, damage simulator or generated scoring thresholds.
The exact observed row and the recommended property are separate fields.
"""
from __future__ import annotations

from .equipment_view import PARTS, _rows, handoff_receipt


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

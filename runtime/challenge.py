"""Bounded two-team constraints over saved builds, never pixels or a simulator.

Templates describe reviewed mechanisms. Selection establishes roster feasibility,
not damage, star clears, optimality, or ownership of an unrecorded character.
"""
from __future__ import annotations

import re
from copy import deepcopy
from datetime import datetime
from itertools import product


def _time(value):
    result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("challenge dates require timezone")
    return result


def challenge_scope(text):
    """Read scope markers, not enemy guesses or a default ordinary mode."""
    floor_match = re.search(r"(?<!\d)(1[0-2]|[1-9])\s*层", text)
    floor = int(floor_match[1]) if floor_match else 12 if re.search(r"(?:十二层|其十二)", text) else 11 if "十一层" in text else 0
    ordinary = bool(re.search(r"普通|常规", text)) or all(re.search(r"剩余\s*" + value + r"\s*轮以上", text) for value in ("10", "20"))
    star = ("星启" in text and not re.search(r"(?:不是|不打|不要)星启|星启.*打不过", text)
            or all(re.search(r"剩余\s*" + value + r"\s*轮以上", text) for value in ("15", "30")))
    return floor, "normal" if ordinary and not star else "star" if star and not ordinary else ""


def assign_teams(candidates):
    """Joint selection avoids greedily consuming the other half's only support."""
    if len(candidates) != 2:
        return []
    choices = []
    for half in candidates:
        eligible = []
        for team in half[:64]:
            members = team.get("members", [])
            identities = [row.get("identity_id") for row in members]
            if (team.get("ready") and len(members) == 4 and all(identities)
                    and len(set(identities)) == 4 and not team.get("missing_slots")):
                eligible.append(team)
        choices.append(eligible)
    pairs = [(upper, lower) for upper, lower in product(*choices)
             if not ({r["identity_id"] for r in upper["members"]}
                     & {r["identity_id"] for r in lower["members"]})]
    if not pairs:
        return []
    # This stable tie-break is not a damage score. All selected teams remain
    # conditional, and no numeric benchmark is presented to the player.
    best = min(pairs, key=lambda pair: (sum(len(t.get("condition_gaps", [])) for t in pair),
                                       tuple(t["id"] for t in pair)))
    return deepcopy(list(best))


def _roster(profile, plan, catalog):
    saved, conflicts = {}, set()
    for row in (profile.get("imported_builds") or {}).get("characters", [])[:200]:
        form_id = row.get("form_id", "")
        form = catalog.get(form_id) or {}
        if (not form.get("sources") or form.get("entity_type") != "character_form"
                or row.get("identity_id") != form.get("identity_id")
                or row.get("name") in plan.excluded_names):
            continue
        if form_id in saved and saved[form_id] != row:
            conflicts.add(form_id)
        saved[form_id] = deepcopy(row)
    for form_id in conflicts:
        saved.pop(form_id, None)
    if plan.supplied_owned:
        saved = {key: row for key, row in saved.items() if row["name"] in plan.supplied_owned}
    # Names without a confirmed form/build are useful questions, not fabricated
    # snapshots with implicit E0/S1/Lv80 values.
    return saved


def _member(row):
    return {key: deepcopy(row.get(key)) for key in (
        "form_id", "identity_id", "name", "level", "eidolon", "light_cone", "sets", "relic_sets",
        "panel", "fetched_at", "manual_updated_at", "field_sources", "source", "freshness")}


def _options(template, saved, *, book, catalog, side):
    slots = template.get("slots", [])
    refs = template.get("source_refs", [])
    if (not template.get("id") or not template.get("label") or not template.get("reason")
            or len(slots) != 4 or not refs or any(ref not in book.sources for ref in refs)
            or any(not isinstance(slot, list) or not 1 <= len(slot) <= 4 for slot in slots)):
        return []
    for gate in template.get("eidolon_requirements", []):
        minimum = gate.get("minimum")
        if (not isinstance(minimum, int) or isinstance(minimum, bool) or not 0 <= minimum <= 6
                or gate.get("form_id") not in {form for slot in slots for form in slot}):
            return []
    choices, missing = [], []
    for index, slot in enumerate(slots):
        known = [saved[form] for form in slot if form in saved]
        choices.append(known or [None])
        if not known:
            missing.append({"slot": index + 1, "choices": [
                {"form_id": form_id, "name": form["name"]} for form_id in slot
                if (form := catalog.get(form_id)) and form.get("sources")], "reason": "not_in_saved_roster"})
    output = []
    for number, selected in enumerate(product(*choices)):
        if number >= 64:
            break
        members = [_member(row) for row in selected if row]
        gaps, warnings = [], []
        for gate in template.get("eidolon_requirements", []):
            row = next((r for r in members if r["form_id"] == gate["form_id"]), None)
            if row is None:
                continue
            rank = row.get("eidolon") if row else None
            if not isinstance(rank, int) or isinstance(rank, bool) or rank < gate["minimum"]:
                gaps.append({"form_id": gate["form_id"], "required_eidolon": gate["minimum"],
                             "saved_eidolon": rank, "reason": "reference_condition_not_met"})
        for row in members:
            cone = row.get("light_cone") or {}
            if not cone or cone.get("superimposition") is None:
                gaps.append({"form_id": row["form_id"], "reason": "cone_or_superimposition_unknown"})
            cone_record = catalog.get(cone.get("id", "")) or {}
            form = catalog.get(row["form_id"]) or {}
            if cone_record.get("path") and form.get("path_id") != cone_record["path"]:
                gap = {"form_id": row["form_id"], "reason": "cone_path_mismatch", "severity": "warning"}
                gaps.append(gap)
                name = cone_record.get("name") or cone.get("name") or cone.get("id", "未命名光锥")
                warnings.append({**gap, "character_name": row["name"], "light_cone_id": cone.get("id"),
                                 "light_cone_name": name, "light_cone_ability_active": False,
                                 "message": f"{row['name']}的「{name}」命途不匹配，仅提供基础属性，光锥技能不生效。"})
        # A gear drawback is not a roster prohibition. Keep the real cone and
        # warn about the inactive ability; hard template prerequisites remain.
        invalid = any(g["reason"] == "reference_condition_not_met" for g in gaps)
        output.append({"id": template["id"] + ":" + str(number), "template_id": template["id"],
            "side": side, "label": template["label"], "members": members, "missing_slots": deepcopy(missing),
            "ready": len(members) == 4 and not missing and not invalid,
            "condition_gaps": gaps, "warnings": warnings, "conditions": deepcopy(template.get("conditions", [])),
            "reason": template["reason"], "basis": template.get("basis", "reviewed_mechanism_candidate"),
            "sources": book.provenance(refs), "first_slot": members[0]["name"] if selected[0] else None,
            "panel_simulated": False, "damage_simulated": False, "guaranteed_clear": False})
    return output


def build_challenge_plan(plan, *, book, catalog, profile, now):
    result = {"requested": plan.kind == "challenge" and plan.mode == "moc", "state": "not_requested",
              "assignments": [], "candidates": [], "missing_information": [], "warnings": [], "question": ""}
    if not result["requested"]:
        return result
    result.update(floor=plan.challenge_floor or None, difficulty=plan.challenge_difficulty or None,
                  focus_side=plan.challenge_side or None,
                  roster_complete=False, scope="saved_build_constraints_not_damage_or_clear_prediction")
    if not plan.challenge_floor or not plan.challenge_difficulty:
        question = ("这层是普通还是星启模式？" if plan.challenge_floor else "要讨论混沌哪一层？"
                    if plan.challenge_difficulty else "要讨论混沌哪一层，是普通还是星启模式？")
        result.update(state="needs_scope", question=question)
        return result
    if plan.challenge_floor != 12 or plan.challenge_difficulty != "normal":
        result.update(state="out_of_scope", question="目前这批只核对普通混沌12层；其他层或星启还没有针对性方案。")
        return result
    periods = [p for p in book.periods if p["mode"] == "moc"
               and (not plan.period_ids or p["id"] in plan.period_ids)
               and _time(p["starts_at"]) <= now < _time(p["ends_at"])]
    stages = [s for s in book.data.get("challenge_stages", [])
              if s.get("period_id") in {p["id"] for p in periods}
              and s.get("floor") == 12 and s.get("difficulty") == "normal"]
    if len(stages) != 1 or len(periods) != 1:
        result.update(state="coverage_unavailable", question="本期普通12层资料还没有核对，不能沿用旧期次敌人配队。")
        return result
    stage, period = stages[0], periods[0]
    halves = stage.get("halves", [])
    if (len(halves) != 2 or [h.get("side") for h in halves] != ["upper", "lower"]
            or not stage.get("source_refs")
            or any(ref not in book.sources for ref in stage.get("source_refs", []))):
        result.update(state="coverage_unavailable", question="本期节点资料不完整，暂不生成上下半配队。")
        return result
    saved = _roster(profile, plan, catalog)
    team_rows = book.data.get("challenge_teams", [])[:64]
    templates = {row.get("id"): row for row in team_rows if row.get("id")}
    if (len(templates) != len(team_rows)
            or any(not half.get("team_refs") or any(ref not in templates for ref in half["team_refs"]) for half in halves)):
        result.update(state="coverage_unavailable", question="本期配队资料缺失或冲突，暂不生成上下半配队。")
        return result
    candidates = [[option for ref in half.get("team_refs", []) if ref in templates
                   for option in _options(templates[ref], saved, book=book, catalog=catalog, side=half["side"])]
                  for half in halves]
    selected = assign_teams(candidates)
    result.update(state="ready" if selected and stage.get("enemy_names_verified") else "partial" if selected else "needs_input",
                  period={key: deepcopy(period[key]) for key in ("id", "name", "starts_at", "ends_at", "rules_summary")},
                  stage=deepcopy(stage), sources=book.provenance(stage["source_refs"]),
                  saved_character_count=len(saved), assignments=selected, candidates=candidates,
                  enemy_specific=bool(stage.get("enemy_names_verified")),
                  coverage_limits=deepcopy((stage.get("evidence") or {}).get("missing", [])),
                  selection_basis="joint_nonoverlap_constraints_not_damage_ranking",
                  instruction="这是按已存配装选择的条件队伍，不是当前画面或最优解。先说上下半各四人和各一个理由；"
                              "需要时再解释星魂/光锥/套装或面板。缺项不猜，不把没存的角色说成没有，不默认专武或毕业。"
                              "未知面板不要求重填全账号，不把已有非战斗面板再次叠加装备。"
                              "没有完整两队就说已存可用部分及一个最关键缺口，不自行补人；说完交还话轮。")
    if plan.challenge_side:
        result["instruction"] += "本轮只展开focus_side对应半场的原因或缺项，另一半只用于角色占用检查，不重复整段讲解。"
    if not stage.get("enemy_names_verified"):
        result["missing_information"].append("已核对普通12层和推荐属性；完整敌人名单尚待详情核对，不能称逐敌机制已验证")
    if not selected:
        # Show one nearest, nonoverlapping partial pair. Do not overwhelm the
        # player with every alternative or silently fill unrecorded slots.
        pairs = [pair for pair in product(*candidates)
                 if len(ids := [r["identity_id"] for t in pair for r in t["members"]]) == len(set(ids))]
        closest = min(pairs, key=lambda pair: (sum(len(t["missing_slots"]) for t in pair),
                      sum(len(t["condition_gaps"]) for t in pair), tuple(t["id"] for t in pair))) if pairs else ()
        result["provisional_halves"] = deepcopy(list(closest))
        missing = next((slot["choices"] for t in closest if not plan.challenge_side or t["side"] == plan.challenge_side
                        for slot in t["missing_slots"] if slot["choices"]), [])
        result["missing_information"].append("已有存档尚不能组成两支互不抢人的已核对参考队，不能随意拼队")
        names = "／".join(row["name"] for row in missing)
        result["question"] = ("先确认一个缺口：" + names + ("已练好可用吗？" if len(missing) == 1 else "中有已练好可用的吗？")
                              + "存档没记录不代表你没有。"
                              if missing else "现有队伍存在角色占用或配置条件缺口，你愿意优先保留哪一队？")
    elif any((r.get("panel") or {}).get("state") != "provided" for t in selected for r in t["members"]):
        result["missing_information"].append("部分角色未保存完整面板；阵容可作条件建议，尚未评估输出、循环或满星练度")
    result["warnings"] = [{**deepcopy(warning), "side": team["side"]}
                          for team in selected or result.get("provisional_halves", [])
                          for warning in team.get("warnings", [])]
    if result["warnings"]:
        result["instruction"] += ("warnings是配装风险，不是缺角色或禁止组队；回答必须一并提醒本轮所谈半场的风险。"
                                  "跨命途光锥只计基础属性，不能引用其光锥技能作为配队收益或已生效增益；"
                                  "保留实际装备，不替换成推荐光锥，不重复给已存最终面板加算基础属性。"
                                  "ready只表示条件阵容可交付，不表示光锥匹配、伤害/循环已验证或保证满星。")
    return result

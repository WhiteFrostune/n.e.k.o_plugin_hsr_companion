"""Small, sourced mechanism views, not role guessing or a damage simulator."""

from __future__ import annotations

import copy
import re
from decimal import Decimal, InvalidOperation

_TOKEN = re.compile(r"#(\d+)\[(i|f[0-6])\](%)?")


def reviewed_stat_advice(role, prop, *, catalog, stated_value=""):
    """Read a pinned trace rule, not a guessed target or damage simulator."""
    result = {"available": False, "property": prop, "recommended_target": None,
              "graduation_threshold_available": False, "comparison": None,
              "kind": "mechanic_cap_not_build_target", "sources": []}
    rule = ((role or {}).get("stat_rules") or {}).get(prop) or {}
    if rule.get("rule") != "pearl_defense_to_elation_v1" or prop != "防御力" or (role or {}).get("identity_id") != "character.pearl":
        return result
    trace = catalog.get(rule.get("trace_id")) or {}
    sources = trace.get("sources") or []
    if (trace.get("id") != rule.get("trace_id") or trace.get("entity_type") != "character_trace" or trace.get("owner_ids") != [role["identity_id"]]
            or not any(s.get("revision") == rule.get("revision") and s.get("source_id") == "starrailres_structured_cn" for s in sources)):
        return result
    rows = trace.get("params") or []
    expected = rule.get("reviewed_params") or []
    try:
        if (not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], list)
                or len(rows[0]) != 6 or len(expected) != 6 or any(isinstance(v, bool) for v in rows[0])):
            return result
        params = [Decimal(str(v)) for v in rows[0]]
        if any(not v.is_finite() for v in params) or params != [Decimal(str(v)) for v in expected]:
            return result  # Parameter/revision changes require another review.
        activation, base, step, increment, excess, healing_ratio = params
        if activation <= 0 or step <= 0 or excess < 0 or excess % step or not 0 < base < 1 or not 0 < increment < 1:
            return result
        cap = activation + excess
        effect = (base + excess / step * increment) * 100
        result.update(available=True, trace_id=trace["id"], trace_name=trace.get("name"),
                      activation_value=int(activation), cap_value=int(cap),
                      max_self_elation_percent=float(effect), self_not_team=True,
                      requirements=["已解锁对应额外能力", "比较的是该能力生效时的实际防御力，不自动等同非战斗面板"],
                      sources=copy.deepcopy(sources), revision=rule["revision"])
        if stated_value and not str(stated_value).endswith("%"):
            value = Decimal(str(stated_value))
            if value.is_finite() and value >= 0:
                result["comparison"] = {"value": str(stated_value), "origin": "player_statement",
                    "assumes_trace_active_and_same_basis": True,
                    "relation": "below_activation" if value < activation else "at_or_above_cap" if value >= cap else "above_activation_below_cap",
                    "proves_graduation": False}
    except (InvalidOperation, TypeError, ValueError, ZeroDivisionError):
        return {**result, "available": False, "sources": [], "comparison": None}
    return result


def skill_view(skill: dict, *, level: int | None = None, sources=()) -> dict:
    """Expand only the requested upstream parameter row; never assume max level."""
    simple = str(skill.get("simple_desc") or "")
    template = str(skill.get("desc") or "")
    missing = []
    rendered = ""
    if level is None:
        missing.append("未确认技能等级，暂不展开倍率")
    elif isinstance(level, bool) or not isinstance(level, int) or level < 1:
        missing.append("技能等级无效")
    else:
        rows = skill.get("params") or []
        if level > len(rows) or not isinstance(rows[level - 1], list):
            missing.append("资料没有该等级的技能参数")
        else:
            params = rows[level - 1]

            def replace(match):
                index = int(match[1]) - 1
                try:
                    if index < 0 or isinstance(params[index], bool):
                        raise ValueError("invalid parameter")
                    value = Decimal(str(params[index]))
                    if not value.is_finite():
                        raise ValueError("non-finite parameter")
                    if match[3]:
                        value *= 100
                    precision = 0 if match[2] == "i" else int(match[2][1:])
                    return format(value, f".{precision}f") + ("%" if match[3] else "")
                except (IndexError, InvalidOperation, ValueError, TypeError):
                    missing.append("技能参数缺失或格式不支持")
                    return match[0]

            rendered = _TOKEN.sub(replace, template)
            if "#" in rendered or not rendered:
                missing.append("技能模板未完整展开")
            if missing:
                rendered = ""  # Never expose a half-filled formula as an explanation.
    return {
        "id": str(skill.get("id") or ""), "name": str(skill.get("name") or ""),
        "type": str(skill.get("type_text") or ""), "effect": str(skill.get("effect_text") or ""),
        "summary": simple, "level": level, "description": rendered,
        "description_verified": bool(rendered), "missing_information": list(dict.fromkeys(missing)),
        "sources": list(sources), "kind": "versioned_knowledge_not_observation",
    }


def character_mechanics(record: dict, *, catalog) -> dict:
    forms = []
    for form_id in (record.get("form_ids") or [])[:12]:
        form = catalog.get(form_id)
        if not form or not form.get("sources"):
            continue
        path = catalog.get(f"path.{form.get('path_id')}") or {}
        element = catalog.get(f"element.{form.get('element_id')}") or {}
        skills = [skill_view(s, sources=form["sources"]) for s in (form.get("skills") or [])[:12]]
        if not any(s["summary"] for s in skills):
            continue
        forms.append({
            "id": form["id"], "name": form["name"],
            "path": path.get("name") or "", "element": element.get("name") or "",
            "skills": skills, "sources": form["sources"],
        })
    return {
        "id": record["id"], "name": record["name"], "forms": forms,
        "needs_form_selection": len(forms) > 1,
        "missing_information": (["有多个形态，尚未确认当前使用的形态"] if len(forms) > 1 else
                                ["资料缺少可解释的角色机制"] if not forms else []),
    }

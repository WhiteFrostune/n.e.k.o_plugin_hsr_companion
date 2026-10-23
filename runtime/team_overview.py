"""Sourced member mechanisms, without inferring team defects from missing data."""

from __future__ import annotations

from .mechanics import character_mechanics


def team_overview(members, *, catalog, goal=""):
    names = list(dict.fromkeys(str(name).strip() for name in members if str(name).strip()))
    records = [catalog.resolve_character(name) for name in names]
    mechanics = [character_mechanics(record, catalog=catalog) for record in records if record]
    covered = [item for item in mechanics if item["forms"]]
    return {
        "members": names,
        "goal": str(goal or ""),
        "recognized_characters": [record["name"] for record in records if record],
        "unknown_characters": [name for name, record in zip(names, records) if not record],
        "strengths": [],
        "issues": [],
        "recommended_next_steps": [],
        "evidence": [{"record_id": record["id"], "name": record["name"], "sources": record.get("sources") or []} for record in records if record],
        "member_mechanics": mechanics,
        "analysis_scope": "sourced_member_mechanics" if covered else "identity_only",
        "analysis_available": bool(covered),
        "team_optimization_available": False,
        "missing_information": ["尚未确认形态、装备、行迹等级、行动配置及具体敌人条件"],
        "disclaimer": "可依据 member_mechanics 解释有来源的成员机制；未实现完整队伍模拟或最优配队。不能把未收录机制说成缺输出、缺生存或阵容缺陷；多形态必须先确认，不能默认任取。",
    }

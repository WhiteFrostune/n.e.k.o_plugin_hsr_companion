"""A typed answer view, not a route planner or quest-completion detector."""

from __future__ import annotations


def navigation_answer(visual: dict, *, requested: bool) -> dict:
    facts = ((visual.get("screen_facts") or {}).get("facts") or []) if visual.get("fresh") else []
    fields = {}
    for name, kind in (
        ("current_location", "current_location"), ("destination", "quest_destination"),
        ("objective", "quest_objective"), ("quest_title", "tracked_quest"),
    ):
        matches = [f for f in facts if f.get("kind") == kind]
        values = {str(f["value"]) for f in matches if f.get("value")}
        known = len(values) == 1
        fields[name] = {"known": known, "value": next(iter(values)) if known else "",
                        "ambiguous": len(values) > 1, "evidence": matches if known else []}
    parts = []
    if fields["current_location"]["known"]:
        parts.append("当前位置：" + fields["current_location"]["value"])
    if fields["destination"]["known"]:
        parts.append("导航目的地：" + fields["destination"]["value"])
    if fields["objective"]["known"]:
        parts.append("任务目标：" + fields["objective"]["value"])
    missing = [name for name in ("current_location", "destination", "objective") if not fields[name]["known"]]
    return {
        "requested": requested, "observed_at": visual.get("observed_at") or "",
        **fields, "confirmed_parts": parts, "missing_fields": missing,
        "game_task_completion": {"status": "unknown", "verified": False},
        "instruction": (
            "只依据 known=true 的字段回答。当前位置回答在哪，destination 只回答去哪，objective 才回答做什么。"
            "目的地不能冒充所在地或具体目标；缺项分别说明，不从未分类文字、旧对话或资料补齐。"
            "这不是路线规划；不得编造最快路线、传送步骤、任务完成或新目标。"
        ),
    }

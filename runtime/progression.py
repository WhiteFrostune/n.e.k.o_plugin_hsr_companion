"""Exact external-table consumption, never guessed inventory or farming advice."""

from __future__ import annotations

from collections import Counter


def plan_progression(plan: dict, *, catalog) -> dict:
    result = {
        "available": False, "scope": "ascension_or_single_trace_materials",
        "materials": [], "missing_information": [], "sources": [],
        "exclusions": ["不含角色经验与升级信用点", "不含其他行迹或装备", "不读取账号库存", "不是培养优先级或最优刷取方案"],
    }
    try:
        if not isinstance(plan, dict):
            raise ValueError("培养计划格式无效")
        # Only explicit machine IDs and endpoints, not natural-language level guesses.
        kind = plan.get("kind")
        current, target = plan.get("current"), plan.get("target")
        if any(isinstance(n, bool) or not isinstance(n, int) for n in (current, target)):
            raise ValueError("需要明确的起始和目标阶段或等级")
        record = catalog.get(str(plan.get("entity_id") or ""))
        if not record or not record.get("sources"):
            raise ValueError("未找到带来源的培养对象")
        if kind == "ascension" and record.get("entity_type") == "character_form":
            rows = (record.get("promotion") or {}).get("materials") or []
            if not 0 <= current <= target <= min(6, len(rows)):
                raise ValueError("突破阶段须为 0–6；不能把角色等级当突破阶段")
            selected = rows[current:target]
            for row in selected:
                if not isinstance(row, list) or not row:
                    raise ValueError("资料缺少某个突破阶段的消耗，不能当作零消耗")
        elif kind == "trace" and record.get("entity_type") == "character_trace":
            rows = record.get("levels") or []
            if not 0 <= current <= target <= min(int(record.get("max_level") or 0), len(rows)):
                raise ValueError("行迹等级超出资料范围")
            # Upstream levels[0] describes unlocking trace level 1.
            selected = []
            for row in rows[current:target]:
                if not isinstance(row, dict) or not isinstance(row.get("materials"), list):
                    raise ValueError("资料缺少某级行迹消耗")
                selected.append(row["materials"])
        else:
            raise ValueError("需要明确角色形态的突破计划，或单个行迹的升级计划")
        total = Counter()
        for row in selected:
            for item in row:
                if not isinstance(item, dict) or not str(item.get("id") or "").isdigit():
                    raise ValueError("材料记录无效")
                number = item.get("num")
                if isinstance(number, bool) or not isinstance(number, int) or number < 0:
                    raise ValueError("材料数量无效")
                total[str(item["id"])] += number
        materials = []
        for key, count in sorted(total.items()):
            item = catalog.get(f"item.{key}") or {}
            materials.append({
                "entity_id": f"item.{key}", "name": item.get("name") or "材料名称未收录",
                "required": count, "owned": None, "shortfall": None,
                "sources": item.get("sources") or record["sources"],
            })
        result.update(available=True, kind=kind, entity_id=record["id"], name=record["name"],
                      current=current, target=target, materials=materials, sources=record["sources"])
    except (ValueError, TypeError, OverflowError) as exc:
        result["missing_information"].append(str(exc))
    return result

"""Saved equipment is reference evidence, never a substitute current frame.

Read the archive supplied by this request. Retain no equipment values in the
discussion and never infer an expired team, ownership or optimality from it.
"""
from __future__ import annotations

import time


def build_saved_cone_guidance(plan, *, catalog, profile):
    identities = plan.character_ids
    answer = {"evidence_source": "saved_profile", "state": "needs_subject",
              "team_assessment": "not_revalidated", "visually_confirmed": False,
              "light_cone": None, "sources": []}
    conclusion, question = "先确认要查哪位角色保存的光锥。", "你指哪位角色？"
    if plan.source_state == "needs_source":
        answer.update(evidence_source="unresolved", state="needs_source")
        conclusion = "尚未确定你指存档配队里的光锥，还是眼前这把，未启动读屏。"
        question = "你指刚才存档里的光锥，还是现在画面这把？"
    elif len(identities) == 1:
        rows = [row for row in (profile.get("imported_builds") or {}).get("characters") or []
                if row.get("identity_id") == identities[0]]
        answer["state"] = "needs_saved_build"
        conclusion, question = "没有唯一可用的该角色存档配装，不能拿旧讨论或眼前装备填入。", "请确认要查的角色形态和已保存配装。"
        if len(rows) == 1:
            row = rows[0]
            form = catalog.get(row.get("form_id", "")) or {}
            cone = row.get("light_cone") or {}
            record = catalog.get(cone.get("id", "")) or {}
            path = form.get("path_id") or form.get("path")
            if (form.get("identity_id") == identities[0] and path and form.get("sources")
                    and record.get("entity_type") == "light_cone" and record.get("sources") and record.get("path")):
                active = path == record["path"]
                name = form.get("name") or (catalog.get(identities[0]) or {}).get("name", "该角色")
                answer.update(state="ready", character={"identity_id": identities[0], "form_id": form["id"],
                              "name": name}, archive_revision=row.get("edit_revision") or row.get("content_digest"),
                              light_cone={"id": record["id"], "name": record["name"],
                                          "superimposition": cone.get("superimposition"), "passive_active": active},
                              sources=[*form["sources"], *record["sources"]])
                conclusion = (f"按本次读取的存档，{name}带的是「{record['name']}」。" +
                              ("命途一致，可触发光锥技能，但这不证明整队强度或循环达标。" if active else
                               "命途不符，只提供基础属性，光锥技能不生效；这项风险本身不要求拆散原队。"))
                question = ""
            else:
                answer["state"] = "needs_cone_data"
                conclusion, question = "存档光锥或角色形态缺少可核对资料，不能推定光锥技能生效。", "请确认该角色保存的光锥名称。"
    ready = answer["state"] == "ready"
    instruction = ("只按本次saved_cone_answer回答，注明存档而非当前画面。命途不符只说明技能不生效，"
                   "不能说角色不能上场、整队最优、保证通关或通关靠运气。未重新核对整队，"
                   "不能把过期讨论的名单或配置当成本次已验证队伍；必要时只澄清队伍，不要求玩家切光锥页。")
    result = {"requested": True, "kind": "build", "state": "ready" if ready else "needs_input",
              "evidence_source": answer["evidence_source"], "saved_cone_answer": answer,
              "source_state": plan.source_state,
              "assessment": {"requested": False},
              "completion": {"requested": not ready, "state": answer["state"],
                             "evaluation_complete": ready, "can_wait_for_evidence": False, "instruction": instruction},
              "reply_plan": {"enabled": True, "mode": "brief", "evaluation_available": ready,
                             "conclusion": conclusion, "next_step": "优先换同命途光锥，再检查实战循环。" if ready and
                             answer["light_cone"]["passive_active"] is False else "",
                             "question": question, "max_questions": int(bool(question)), "instruction": instruction},
              "guides": [], "team_candidates": [], "sources": answer["sources"], "instruction": instruction}
    if ready or plan.source_state == "needs_source" and len(identities) == 1:
        result["context"] = {"at": time.monotonic(), "kind": "build", "character_ids": list(identities),
                             "request_focus": "equipment", "equipment_kinds": [],
                             "consultation_action": "equipment_guide" if ready else "clarify",
                             "execution_intent": "equipment_guide" if ready else "assess_cone",
                             "assessment_scope": "saved_cone" if ready else "",
                             "evidence_source": answer["evidence_source"], "subject_state": plan.subject_state}
    return result

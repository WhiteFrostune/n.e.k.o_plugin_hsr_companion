"""One short-lived missing-row reply, separate from current-screen evidence.

No OCR, capture, profile writes, source-value replacement or fresh visual receipt.
A supplied value refers to the last discussed snapshot, never to the screen now.
"""
from __future__ import annotations

import copy
import re
import time
from dataclasses import replace

from .consultation import STAT_PROPERTIES
from .equipment import assess_relic, equipment_card, equipment_digest, equipment_object, property_name
from .observation import public_visual_evidence
from .query_coordinator import answer_status
from .query_plan import build_query_plan
from .service_contract import mark_service_request, record_answer_delivery, response_contract

REPLY_SECONDS = 120.0


def offer_equipment_reply(owner, visual, *, character_ids=(), assess=False):
    """Offer only ONE missing substat of an already bound +15 relic."""
    card = equipment_card(visual)
    result = {"state": "not_needed"}
    if (not equipment_object(visual, card)["confirmed"] or card["state"] != "partial"
            or card["expected_sub_stat_count"] != 4 or len(card["sub_stats"]) != 3
            or not card["main_stat"]):
        owner._equipment_reply_pending = None
        return result
    labels = {str(f.get("label", "")).split("·")[-1]
              for f in (visual.get("screen_facts") or {}).get("facts", [])
              if f.get("kind") == "unreadable_stat" and str(f.get("label", "")).startswith("副词条·")}
    prop = next(iter(labels)) if len(labels) == 1 and labels <= set(STAT_PROPERTIES) else ""
    owner._equipment_reply_pending = {
        "at": time.monotonic(), "epoch": getattr(owner, "_observation_epoch", 0),
        "pid": getattr(owner, "_analysis_game_pid", 0), "card": copy.deepcopy(card),
        "property": prop, "character_ids": tuple(character_ids), "assess": assess,
    }
    return {"state": "awaiting_player_value", "kind": "sub_stat", "property": prop,
            "snapshot_id": card["snapshot_id"], "expires_in_seconds": REPLY_SECONDS,
            "player_text_is_sufficient": True,
            "instruction": "若询问缺项，可请玩家提供该项；有明确词条名时补数即可，否则询问词条名和数值。"
                           "玩家补充走player_reply，不重述成看屏幕确认；不是固定问答，也不保证后台读全。"}


def _valid(owner, pending):
    observer = owner._observer_status
    if (not 0 <= time.monotonic() - pending["at"] <= REPLY_SECONDS
            or pending["epoch"] != getattr(owner, "_observation_epoch", 0)
            or pending["pid"] != getattr(owner, "_analysis_game_pid", 0)
            or not observer.get("enabled") or observer.get("state") in {"paused", "stopped", "waiting_for_game"}
            or getattr(owner, "_observer_stop_requested", False)
            or getattr(owner, "_observation_invalidated", False) or getattr(owner, "_scene_preview", None)):
        return False
    # Detect KNOWN contradictions without another capture. Absence is not a
    # fresh-screen proof: the result below always remains historical discussion.
    raw = getattr(owner, "_committed_observation", None) or {}
    page = (raw.get("page") or {}).get("page_type") or raw.get("page_type")
    if page not in {None, "unknown", "relic_detail"}:
        return False
    card = pending["card"]
    known = {("equipment_name", ""): card["name"], ("equipment_slot", ""): card["slot"],
             ("equipment_level", ""): card["level"],
             ("main_stat", card["main_stat"]["property"]): card["main_stat"]["value"],
             **{("sub_stat", r["property"]): r["value"] for r in card["sub_stats"]}}
    for fact in (raw.get("screen_facts") or {}).get("facts", []):
        kind, value = fact.get("kind"), str(fact.get("value", ""))
        prop = property_name(fact.get("label"), value) if kind in {"main_stat", "sub_stat"} else ""
        if kind == "main_stat" and prop != card["main_stat"]["property"]:
            return False
        if (kind, prop) in known and known[kind, prop] != value:
            return False
    return True


def _parse(text, expected):
    if not isinstance(text, str) or len(text) > 80:
        return None
    text = re.sub(r"\s+", "", text).rstrip("。！!")
    properties = "|".join(map(re.escape, STAT_PROPERTIES))
    match = re.fullmatch(r"(?:(?:那|这)(?:项|条)(?:副词条)?|数值)?(?:是|为)?"
                         r"(?P<property>" + properties + r")?(?:是|为|[:：+])?"
                         r"(?P<value>\d{1,8}(?:\.\d{1,3})?%?)", text)
    if not match or float(match["value"].rstrip("%")) <= 0:
        return None
    prop = match["property"] or expected
    if not prop or expected and prop != expected:
        return None
    return {"label": prop, "value": match["value"], "property": property_name(prop, match["value"]),
            "origin": "player_statement", "visually_confirmed": False}


async def try_equipment_reply(owner, query, *, player_reply=None, generation):
    """A typed answer is not a request to independently prove its pixels.

    An explicit original reply can accompany a model-rephrased query. It never
    promotes the supplied value to OCR truth or overrides a real screen request.
    """
    pending = getattr(owner, "_equipment_reply_pending", None)
    if pending is None and player_reply is None:
        return None
    text = str(query or "").strip()
    screen_request = any(w in text for w in (
        "重新识别", "核对画面", "屏幕", "现在在哪", "什么页面", "什么界面", "这个呢", "换了", "另一件", "换一件"))
    candidate = player_reply is not None or len(text) <= 40 and bool(re.search(r"\d", text)) and not re.search(r"[？?吗么]", text)
    if screen_request or not candidate:
        owner._equipment_reply_pending = None
        return None
    state, row = "needs_context", None
    alive = pending is not None and _valid(owner, pending)
    if alive:
        row = _parse(player_reply if player_reply is not None else text, pending["property"])
        card = pending["card"]
        if row and row["property"] in {card["main_stat"]["property"], *(r["property"] for r in card["sub_stats"])}:
            row = None
        state = "accepted" if row else "needs_clarification"
    historical = ([{"kind": "main_stat", **pending["card"]["main_stat"]},
                   *[{"kind": "sub_stat", **r} for r in pending["card"]["sub_stats"]]] if alive else [])
    statement = {"state": state, "origin": "player_statement", "visually_confirmed": False,
                 "describes_current_screen": False, "row": row,
                 "discussion_object": {k: pending["card"][k] for k in ("name", "slot", "snapshot_id")} if alive else None,
                 "prior_observed_rows": copy.deepcopy(historical),
                 "visual_complete": False, "discussion_rows_complete": bool(row),
                 "instruction": "补充值来自玩家，不是刚看见的数值；先承接补充，再完成原问题并交还话轮。"
                                "之前读到的行仅属于该讨论快照，不代表当前画面，不写视觉或永久档案。"}
    evaluation = {"available": False}
    if row and pending["assess"]:
        book = getattr(owner, "_guidance_book", None)
        role = next((r for r in book.characters if pending["character_ids"] == (r["identity_id"],)), None) if book else None
        if role:
            discussion = copy.deepcopy(pending["card"])
            discussion["sub_stats"].append(row)
            discussion["content_digest"] = equipment_digest(discussion)
            discussion["snapshot_id"] = "player-discussion:" + discussion["content_digest"][:16]
            evaluation = assess_relic(discussion, role, book.provenance(role["source_refs"]))
            evaluation.update(origin="player_supplemented_discussion", visually_confirmed=False,
                              discussion_rows_complete=True)
            # Missing VISUAL proof is not a new debt once the player supplied
            # the requested discussion value. Keep complete=False for vision.
            evaluation["next_steps"] = [step for step in evaluation.get("next_steps", [])
                                        if step.get("kind") != "missing_rows"]
    plan = replace(build_query_plan(query), screen=False, identity=False, content=False, scene_only=False,
                   navigation=False, controlled=False, topics=frozenset(), fields=frozenset())
    visual = public_visual_evidence({}, owner._observer_status)
    next_step = next((r.get("check", "") for r in evaluation.get("sub_stats", [])
                      if row and r["property"] == row["property"]), "")
    reply_plan = {"enabled": True, "mode": "brief", "goal": "acknowledge_player_equipment_value",
                  "after_answer": "yield_to_player", "evidence_rows": [row] if row else [],
                  "next_step": next_step, "evaluation_available": evaluation["available"],
                  "question": None if row else {"purpose": "identify_property_and_value" if alive else "identify_discussion_object"},
                  "instruction": "先简短确认玩家补充的词条和值；原问题有评价需求时，只讲一个有来源的作用或下一步。"
                                 "不用再次等待OCR，不说看清/刷新好了，不重新背整套配装；说到位就结束。"
                                 "无法绑定或信息有歧义时，只澄清一项，不猜数值、单位或装备。"}
    payload = {"response_contract": response_contract(owner, generation, channel="direct"),
               "query_plan": plan.public(), "query_answer": answer_status(plan, visual, owner._observer_status),
               "player_equipment_statement": statement, "equipment_answer": {"state": "not_requested"},
               "current_visual_evidence": visual, "query_timing": {"refresh_wait_seconds": 0, "refresh": {"outcome": "not_requested", "stages": []}},
               "followup": {"pending": False, "reason": "player_statement", "instruction": "本轮不新增画面等待或补答。"},
               "guidance_answer": {"requested": True, "kind": "equipment_reply", "state": "ready" if row else "needs_input",
                                   "assessment": {"evaluation": evaluation, "evaluation_available": evaluation["available"]},
                                   "reply_plan": reply_plan, "instruction": statement["instruction"] + reply_plan["instruction"]},
               "authoritative_context": statement["instruction"] + reply_plan["instruction"]}
    await owner._record_tool_use("hsr_companion_prepare", mark_query=False, query=text[:160],
                                query_plan=plan.public(), refresh_diagnostics=payload["query_timing"]["refresh"],
                                player_equipment_reply_state=state)
    if (generation != owner._answer_query_generation or alive and (
            getattr(owner, "_equipment_reply_pending", None) is not pending or not _valid(owner, pending))):
        statement.update(state="needs_context", row=None, discussion_object=None, prior_observed_rows=[], discussion_rows_complete=False)
        payload["guidance_answer"] = {"requested": True, "state": "superseded", "instruction": "讨论对象或问题已变化，不沿用旧装备补数。"}
        if getattr(owner, "_equipment_reply_pending", None) is pending:
            owner._equipment_reply_pending = None
    elif row or not alive:
        if getattr(owner, "_equipment_reply_pending", None) is pending:
            owner._equipment_reply_pending = None
    mark_service_request(owner, generation, "completed")
    payload["delivery_receipt"] = record_answer_delivery(owner, generation=generation, channel="direct", payload=payload,
                                                        visual=visual, answer=payload["query_answer"])
    return payload

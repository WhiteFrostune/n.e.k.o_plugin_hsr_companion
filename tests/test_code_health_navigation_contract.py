"""Page identity and selected source advice have different content contracts.

Actual local adapter/prepare on synthetic OCR and a test catalog only. No live
game, network, model or host notification; original safety checks stay active.
"""
import asyncio
import copy
from types import SimpleNamespace

import pytest
from plugin.plugins.hsr_companion.adapters.local_vision import LocalVisionAdapter
from plugin.plugins.hsr_companion.adapters.text_verification import proof_key
from plugin.plugins.hsr_companion.runtime.query_coordinator import satisfied_fields
from plugin.plugins.hsr_companion.runtime.query_plan import build_query_plan
from plugin.plugins.hsr_companion.runtime.query_reuse import try_reuse
from plugin.plugins.hsr_companion.tests.test_chain_audit_20261017 import audit_owner, commit_raw, navigation_raw
from plugin.plugins.hsr_companion.tests.test_current_observation import plugin_fixture
from plugin.plugins.hsr_companion.tests.test_delivery_fix_20261012 import frame, picture
from plugin.plugins.hsr_companion.tests.test_experience_fixes import box
from plugin.plugins.hsr_companion.tests.test_navigation_v1 import interior, png


@pytest.mark.parametrize("heading", ["missing", "conflicting"])
def test_navigation_delivers_quest_without_inventing_location(tmp_path, heading):
    owner, _, _ = audit_owner(tmp_path)
    boxes = interior()[1:] if heading == "missing" else [*interior(), box("地点丙", 210, 10)]
    adapter = LocalVisionAdapter(owner._catalog)
    adapter._get_backend = lambda: SimpleNamespace(extract_text_with_boxes=lambda image: ("", boxes))
    raw = adapter.analyze_navigation(png())
    assert raw["page"]["page_type"] == "exploration_hud"

    async def run():
        await commit_raw(owner, raw)
        await commit_raw(owner, raw)
        result = await owner.hsr_companion_prepare(query="现在在哪，接下来这个任务要做什么？")
        assert result["query_answer"]["state"] == "partial"
        assert result["query_answer"]["confirmed_fields"] == ["quest"]
        assert result["query_answer"]["missing_fields"] == ["location"]
        navigation = result["screen_context_answer"]["navigation"]
        assert not navigation["current_location"]["known"]
        assert navigation["objective"]["value"] == "寻找线索甲"
        assert navigation["destination"]["value"] == "区域乙"
        assert not any(f["kind"] == "current_location"
                       for f in result["current_visual_evidence"]["screen_facts"]["facts"])

    asyncio.run(run())


def test_degraded_old_navigation_does_not_hide_current_partial_quest(tmp_path):
    from io import BytesIO

    from PIL import Image, ImageDraw

    owner, _, _ = audit_owner(tmp_path)
    with Image.open(BytesIO(picture())) as opened:
        image = opened.convert("RGB")
    ImageDraw.Draw(image).rectangle((280, 58, 550, 122), fill="white")
    output = BytesIO()
    image.save(output, format="PNG")
    new_pixels = output.getvalue()

    async def run():
        previous = navigation_raw()
        previous["recognition"]["navigation_only_query"] = True
        await commit_raw(owner, previous)
        await commit_raw(owner, previous)
        current = copy.deepcopy(previous)
        current["screen_facts"]["facts"] = current["screen_facts"]["facts"][1:]
        for fact in current["screen_facts"]["facts"]:
            fact["value"] = "新" + fact["value"]
            for proof in fact["evidence"]:
                proof["text"] = fact["label"] + fact["value"]
        await commit_raw(owner, current, new_pixels)
        owner._assistant_client.fetch_frame = lambda seq: frame(new_pixels)
        literals = {p["text"] for f in current["screen_facts"]["facts"] for p in f["evidence"]}
        owner._vision.verify_text_regions = lambda data, proofs, **kw: {
            proof_key(p) for p in proofs if p.get("text") in literals}
        assert "full" not in owner._reusable_observations
        assert "navigation_previous" in owner._reusable_observations
        plan = build_query_plan("现在在哪，接下来任务做什么？")
        selected = await try_reuse(owner, navigation=True, plan=plan)
        assert selected is not None
        assert satisfied_fields(plan, selected) == {"quest"}
        assert {f["value"] for f in selected["screen_facts"]["facts"]} == {"新任务甲", "新目标甲"}
        assert len(owner._reusable_observations) <= 4  # current scene/nav plus ONE retained per scope

    asyncio.run(run())


@pytest.mark.parametrize("heading_changed", [False, True])
def test_missing_new_location_keeps_old_location_only_with_its_own_live_proof(tmp_path, heading_changed):
    owner, _, _ = audit_owner(tmp_path)

    async def run():
        previous = navigation_raw()
        previous["recognition"]["navigation_only_query"] = True
        await commit_raw(owner, previous)
        await commit_raw(owner, previous)
        current = copy.deepcopy(previous)
        current["screen_facts"]["facts"] = current["screen_facts"]["facts"][1:]
        for fact in current["screen_facts"]["facts"]:
            fact["value"] = "新" + fact["value"]
            for proof in fact["evidence"]:
                proof["text"] = fact["label"] + fact["value"]
        await commit_raw(owner, current)
        if heading_changed:
            from io import BytesIO

            from PIL import Image, ImageDraw

            with Image.open(BytesIO(picture())) as opened:
                changed = opened.convert("RGB")
            ImageDraw.Draw(changed).rectangle((290, 60, 540, 76), fill="white")
            data = BytesIO()
            changed.save(data, format="PNG")
            owner._assistant_client.fetch_frame = lambda seq: frame(data.getvalue())
            owner._vision.verify_text_regions = lambda *args, **kwargs: set()
        plan = build_query_plan("现在在哪？")
        selected = await try_reuse(owner, navigation=False, plan=plan)
        if heading_changed:
            assert selected is None or not satisfied_fields(plan, selected)
            assert not any(f["kind"] == "current_location" for f in (selected or {}).get("screen_facts", {}).get("facts", []))
            return
        assert selected is not None and satisfied_fields(plan, selected) == {"location"}
        assert [f["value"] for f in selected["screen_facts"]["facts"]] == ["地点甲"]
        assert not any(f["kind"] in {"tracked_quest", "quest_objective"} for f in selected["screen_facts"]["facts"])

    asyncio.run(run())


@pytest.mark.parametrize("revocation", ["stale_capture", "changed_control", "different_process", "epoch"])
def test_partial_navigation_still_requires_current_frame_and_page(tmp_path, revocation):
    owner, _, _ = audit_owner(tmp_path)

    async def run():
        raw = navigation_raw()
        raw["recognition"]["navigation_only_query"] = True
        raw["screen_facts"]["facts"] = raw["screen_facts"]["facts"][1:]
        await commit_raw(owner, raw)
        await commit_raw(owner, raw)
        plan = build_query_plan("现在在哪，接下来任务做什么？")
        valid = await try_reuse(owner, navigation=True, plan=plan)
        assert valid is not None and satisfied_fields(plan, valid) == {"quest"}
        capture = frame(picture("page" if revocation == "changed_control" else "none"),
                        stale=revocation == "stale_capture")
        if revocation == "different_process":
            capture.game["pid"] = 2
        elif revocation == "epoch":
            owner._observation_epoch = getattr(owner, "_observation_epoch", 0) + 1
        owner._assistant_client.fetch_frame = lambda seq: capture
        owner._vision.verify_text_regions = lambda *args, **kw: set()
        selected = await try_reuse(owner, navigation=True, plan=plan)
        assert selected is None

    asyncio.run(run())


def test_navigation_pass_without_readable_values_can_still_answer_only_page(tmp_path):
    owner, _, _ = audit_owner(tmp_path)

    async def run():
        raw = navigation_raw()
        raw["recognition"]["navigation_only_query"] = True
        raw["screen_facts"]["facts"] = []
        await commit_raw(owner, raw)
        await commit_raw(owner, raw)
        plan = build_query_plan("当前页面是什么？", "read_screen", screen_fields=["page"])
        selected = await try_reuse(owner, navigation=False, plan=plan)
        assert selected is not None and satisfied_fields(plan, selected) == {"page"}
        assert not selected["screen_facts"]["facts"]

    asyncio.run(run())


@pytest.mark.parametrize("query,topics", [
    ("绯英遗器怎么配？", {"relic"}),
    ("绯英光锥怎么配？", {"cone"}),
    ("绯英遗器和光锥怎么配？", {"relic", "cone"}),
    ("不用讲光锥，只看绯英遗器怎么配。", {"relic"}),
])
def test_selected_knowledge_body_keeps_requested_topics_and_sources(tmp_path, query, topics):
    owner, values, _ = plugin_fixture(tmp_path)
    owner._observer_status.update(enabled=False, state="paused")
    profile = copy.deepcopy(values["player_context"])

    async def forbidden(*args, **kwargs):
        raise AssertionError("Reference advice must not wait for current screen evidence")

    owner._current_visual_evidence = owner._read_game_state = owner._wait_for_data_pack = forbidden

    async def run():
        result = await owner.hsr_companion_prepare(query=query)
        assert not result.get("is_error")
        assert not result["query_plan"]["screen"]
        assert result["query_timing"]["refresh_wait_seconds"] == 0
        guidance = result["guidance_answer"]
        assert guidance["assessment"]["subject_ids"] == ["character.evanescia"]
        assert not guidance["assessment"]["visual_subject_confirmed"]
        reply = guidance["reply_plan"]
        assert reply["enabled"] and set(reply["guide_topics"]) == topics
        options = reply["discussion_options"]
        assert len(options) == 1 and options[0]["character"] == "绯英"
        assert bool(options[0].get("main_stats")) is ("relic" in topics)
        assert "light_cone_candidates" in options[0]
        if "cone" not in topics:
            assert not options[0]["light_cone_candidates"]
        assert options[0]["sources"] and guidance["sources"]
        assert not {"guides", "team_candidates", "context"} & guidance.keys()
        assert values["player_context"] == profile

    asyncio.run(run())

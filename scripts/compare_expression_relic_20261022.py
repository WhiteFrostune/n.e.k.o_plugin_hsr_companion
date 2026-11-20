"""Frozen-source comparison, synthetic cache/guard inputs, real text formatter.

Does not connect to the helper, game, model or message bus. Only generated
reports/fixtures under this plugin's dist are written. No live OCR speed claim.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import importlib.util
import json
import statistics
import sys
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parents[2]))
BEFORE = ROOT / "dist/expression-relic-before-20261022-1805"


def frozen_module(name):
    key = "plugin.plugins.hsr_companion.runtime._frozen_compare_" + name
    spec = importlib.util.spec_from_file_location(key, BEFORE / "runtime" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[key] = module
    spec.loader.exec_module(module)
    return module


def timing(operation, repeats):
    samples = []
    result = None
    for _ in range(repeats):
        start = time.perf_counter()
        result = operation()
        samples.append((time.perf_counter() - start) * 1000)
    return {"samples": repeats, "mean_ms": round(statistics.mean(samples), 4),
            "p95_ms": round(sorted(samples)[int((repeats - 1) * .95)], 4),
            "max_ms": round(max(samples), 4)}, result


async def cache_case(module, *, rows, unchanged=False):
    from plugin.plugins.hsr_companion.runtime.equipment import equipment_card
    from plugin.plugins.hsr_companion.runtime.query_plan import build_query_plan
    from plugin.plugins.hsr_companion.tests.test_current_observation import plugin_fixture
    from plugin.plugins.hsr_companion.tests.test_delivery_fix_20261012 import frame, picture
    from plugin.plugins.hsr_companion.tests.test_delivery_races import complete_commit
    from plugin.plugins.hsr_companion.tests.test_expression_relic_closeout_20261022 import changed_digits

    owner, _, _ = plugin_fixture(ROOT / "dist/expression-compare-fixture-unused")
    owner._answer_query_generation = owner._query_refresh_generation = 9
    await complete_commit(owner, picture(), "10")
    previous = copy.deepcopy(owner._reusable_observations["full"])
    payload = changed_digits()
    await complete_commit(owner, payload, "20")
    raw = copy.deepcopy(owner._committed_observation)
    raw["screen_facts"]["facts"] = raw["screen_facts"]["facts"][:rows]
    await owner._apply_local_analysis(raw, image_fingerprint="comparison-partial", source="assistant_local_ocr")
    owner._reusable_observations["full_previous"] = previous
    committed = copy.deepcopy(owner._committed_observation)
    calls = {"frame_reads": 0, "literal_reviews": 0}

    def verify(*a, **kw):
        calls["literal_reviews"] += 1
        return set()

    def fetch(_):
        calls["frame_reads"] += 1
        return frame(picture() if unchanged else payload)

    owner._vision = SimpleNamespace(verify_text_regions=verify)
    owner._assistant_client = SimpleNamespace(fetch_frame=fetch)
    plan = build_query_plan("这件呢？给她合适吗", "assess_relic", screen_fields=["equipment", "page"])
    result, samples = [], []
    for _ in range(2):
        start = time.perf_counter()
        selected = await module.try_reuse(owner, navigation=False, full=True, plan=plan)
        samples.append(round((time.perf_counter() - start) * 1000, 3))
        card = equipment_card(selected or {})
        result.append({"main": (card.get("main_stat") or {}).get("value"),
                       "sub_count": len(card["sub_stats"]), "complete": card["complete"]})
    return {"rows": rows, "old_pixels_match": unchanged, "result_twice": result,
            "elapsed_ms": samples, "calls": calls, "committed_unchanged": owner._committed_observation == committed}


def main():
    from plugin.plugins.hsr_companion.runtime import answer_message, guidance, query_reuse
    from plugin.plugins.hsr_companion.runtime.consultation import for_followup
    from plugin.plugins.hsr_companion.runtime.query_plan import build_query_plan
    from plugin.plugins.hsr_companion.tests import test_guidance as fixtures
    from plugin.plugins.hsr_companion.tests.test_d1_closeout import panel
    from plugin.plugins.hsr_companion.tests.test_stage2_assessment import assess, relic

    old_reply, old_message, old_cache = (frozen_module(n) for n in ("consultation", "answer_message", "query_reuse"))
    book = fixtures.book.__wrapped__()
    catalog = fixtures.catalog.__wrapped__(ROOT / "dist/expression-compare-catalog-unused")
    current = panel()
    current["characters"] = [{"entity_id": "character.pearl", "name": "真珠", "identity_scope": "detail_subject"}]
    cases = [("panel", "真珠这面板怎么样，接下来先养哪儿？", current),
             ("relic_fit", "这件遗器适合绯英吗？", relic()),
             ("relic_full", "这件遗器主副词条是什么？适合绯英吗？", relic())]
    # Warm the same read-only host tokenizer for BOTH sides. Cold timing is
    # reported separately, never mistaken for ongoing OCR or model latency.
    cold, _ = timing(lambda: answer_message.bridge_preview("格式器预热"), 1)
    report = {"kind": "offline_frozen_source_comparison", "no_install_or_live_capture": True,
              "not_end_to_end_model_or_OCR_timing": True, "cold_formatter": cold, "expression": []}
    for key, query, visual in cases:
        selection = fixtures.plan(query, book, catalog)
        observed = selection.observation_plan(build_query_plan(query))
        result = assess(query, visual, book, catalog)
        args = (selection, result["assessment"], next(c for c in book.characters
                if c["identity_id"] == selection.character_ids[0]))
        kwargs = {"query": query, "guides": result["guides"], "teams": result["team_candidates"]}
        old_t, before = timing(lambda: old_reply.build_reply_plan(*args, **kwargs), 500)
        new_t, after = timing(lambda: guidance.build_reply_plan(*args, **kwargs), 500)
        packet = {"guidance_answer": copy.deepcopy(result), "screen_facts": visual["screen_facts"],
                  "equipment_answer": result["assessment"].get("equipment_answer") or {}}
        packet["guidance_answer"]["reply_plan"] = before
        old_render, old_body = timing(lambda: old_message.render_followup(packet, plan=observed, ready=True), 100)
        packet["guidance_answer"]["reply_plan"] = after
        new_render, new_body = timing(lambda: answer_message.render_followup(packet, plan=observed, ready=True), 100)
        row = {"case": key, "plan_before": old_t, "plan_after": new_t,
               "formatter_before": old_render, "formatter_after": new_render,
               "text_chars_before": len(old_body[0]), "text_chars_after": len(new_body[0]),
               "body_complete_before": old_body[1]["complete"], "body_complete_after": new_body[1]["complete"],
               "before_text": old_body[0], "after_text": new_body[0]}
        if key == "relic_full":
            card = packet["equipment_answer"]
            assert all(r["label"] + r["value"] in new_body[0] for r in [card["main_stat"], *card["sub_stats"]]), (observed, new_body, old_body)
        assert new_body[1]["complete"]
        report["expression"].append(row)
    before = assess(cases[1][1], relic(subs=[]), book, catalog)["reply_plan"]
    meta = copy.deepcopy(before)
    meta["current_object"] = {"name": "only metadata changed", "slot": "位面球"}
    report["metadata_only_silent"] = not for_followup(meta, previous=before)["answer_increment"]
    report["cache"] = {}
    for label, module in (("before", old_cache), ("after", query_reuse)):
        report["cache"][label] = asyncio.run(asyncio.wait_for(_cache_batch(module), timeout=20))
    assert [r["main"] for r in report["cache"]["before"][0]["result_twice"]] == [None, None]
    assert [r["main"] for r in report["cache"]["after"][0]["result_twice"]] == ["20", "20"]
    assert all(r["main"] is None for r in report["cache"]["after"][1]["result_twice"])
    assert all(r["main"] == "10" and r["complete"] for r in report["cache"]["after"][2]["result_twice"])
    installed = Path("C:/Users/cai/AppData/Local/N.E.K.O/.neko-plugin-installations/plugins/hsr_companion")
    frozen = json.loads((ROOT / "dist/stage3-candidate-20261022.json").read_bytes())["source_hashes"]
    report["baseline_hash_matches_frozen"] = all(hashlib.sha256((BEFORE / "runtime" / (n + ".py")).read_bytes()).hexdigest()
        == frozen["runtime/" + n + ".py"] for n in ("consultation", "answer_message", "query_reuse"))
    assert report["baseline_hash_matches_frozen"]
    report["installed_hash_mismatches"] = [n for n, h in frozen.items()
        if not (installed / n).is_file() or hashlib.sha256((installed / n).read_bytes()).hexdigest() != h]
    report["changed_source_files"] = [n for n, h in frozen.items()
        if hashlib.sha256((ROOT / n).read_bytes()).hexdigest() != h]
    destination = ROOT / "dist/expression-relic-comparison-final-20261022.json"
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"report": str(destination), "metadata_only_silent": report["metadata_only_silent"],
                      "installed_mismatches": report["installed_hash_mismatches"],
                      "expression": [{k: v for k, v in r.items() if not k.endswith("text")} for r in report["expression"]]}, ensure_ascii=False))


async def _cache_batch(module):
    return [await cache_case(module, rows=1), await cache_case(module, rows=0),
            await cache_case(module, rows=1, unchanged=True)]


if __name__ == "__main__":
    main()

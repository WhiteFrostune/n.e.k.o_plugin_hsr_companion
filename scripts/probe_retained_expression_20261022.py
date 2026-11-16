"""Diagnose one unchanged two-second regression with bounded detached traces."""
import asyncio
import copy
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parents[2]))


def main():
    import pytest

    from plugin.plugins.hsr_companion.runtime import followup, query_reuse
    from plugin.plugins.hsr_companion.runtime.equipment import equipment_card
    from plugin.plugins.hsr_companion.runtime.observation_store import ObservationSnapshot
    from plugin.plugins.hsr_companion.scripts.compare_expression_relic_20261022 import frozen_module
    from plugin.plugins.hsr_companion.tests import test_stage2a_contract as case

    report = {}
    for label, module in (("candidate", followup), ("frozen_followup_and_cache", frozen_module("followup"))):
        events = []
        completion = module.request_completion
        reuse = query_reuse.try_reuse if label == "candidate" else frozen_module("query_reuse").try_reuse
        matches = ObservationSnapshot.matches

        def state(owner, visual, request, **kw):
            result = completion(owner, visual, request, **kw)
            events.append({"event": "completion", "state": result[0].public(),
                           "main": (equipment_card(visual).get("main_stat") or {}).get("value"),
                           "serial": (visual.get("screen_facts") or {}).get("observation_serial")})
            return result

        async def recover(*a, **kw):
            result = await reuse(*a, **kw)
            events.append({"event": "cache", "selected": result is not None,
                           "main": (equipment_card(result or {}).get("main_stat") or {}).get("value")})
            return result

        def valid(self, owner):
            result = matches(self, owner)
            events.append({"event": "matches", "value": result, "boundary": self.delivery(owner).get("delivery_boundary")})
            return result

        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(module, "request_completion", state)
            patch.setattr(query_reuse, "try_reuse", recover)
            patch.setattr(ObservationSnapshot, "matches", valid)
            patch.setattr(case, "followup", module)
            try:
                case.test_followup_can_deliver_verified_retained_packet_not_partial_current(
                    ROOT / "dist" / ("retained-expression-probe-" + label), patch)
                outcome = "passed"
            except (Exception, asyncio.CancelledError) as error:
                outcome = type(error).__name__ + ": " + str(error)
        report[label] = {"outcome": outcome, "events": copy.deepcopy(events[:20])}
    path = ROOT / "dist/retained-expression-after-repair-20261022.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()

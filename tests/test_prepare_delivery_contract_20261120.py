"""0.9.7: an incomplete equipment read must carry an explicit delivery notice."""
from __future__ import annotations

import asyncio

from plugin.plugins.hsr_companion.tests import test_guidance as guidance_cases
from plugin.plugins.hsr_companion.tests.test_d1_subject_contract_20261020 import formal_fixture
from plugin.plugins.hsr_companion.tests.test_stage2_assessment import relic

catalog = guidance_cases.catalog


def test_prepare_marks_incomplete_relic_substats(tmp_path, monkeypatch, catalog):
    current = relic(subs=[("暴击率", "5.5%"), ("暴击伤害", "6.4%"), ("速度", "7")])
    owner, _, _, _ = formal_fixture(tmp_path, monkeypatch, catalog, current)

    async def run():
        result = await owner.hsr_companion_prepare(
            query="这个遗器属性是啥", intent="read_screen", screen_fields=["equipment"])
        assert not result.get("is_error"), result
        card = result["equipment_answer"]
        assert card["complete"] is False and card["sub_stats_complete"] is False
        assert card["observed_sub_stat_count"] == 3
        assert "只读到 3/4 条" in result["delivery_notice"]
        assert "缺项未读全" in result["delivery_notice"]
        assert "只读到 3/4 条" in result["query_answer"]["instruction"]

    asyncio.run(run())

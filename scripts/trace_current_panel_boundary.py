"""One unchanged-helper resident cross-page query, with real saved Pearl data.

The read-only SQLite rows are copied into the isolated owner's in-memory store.
No archive/host chat writes, OCR overrides, request-budget changes or installs.
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import sqlite3
from pathlib import Path
from unittest.mock import patch

from . import trace_character_handoff as handoff
from .trace_reader_budget import ReaderBudgetProbe


def pearl_archive():
    import ormsgpack

    database = Path('C:/Users/cai/AppData/Local/N.E.K.O/data-packs/hsr_companion/player-state/store.db')
    with sqlite3.connect(database.as_uri() + '?mode=ro', uri=True) as db:
        values = {key: ormsgpack.unpackb(data) for key, data in db.execute(
            'SELECT key,value FROM kv_store WHERE key IN (?,?)', ('manual_player_builds', 'uid_import_state'))}
    for value in values.values():
        value['characters'] = {key: row for key, row in value.get('characters', {}).items()
                               if row.get('identity_id') == 'character.pearl'}
    return values


async def run(args):
    from plugin.plugins.hsr_companion.tests import test_current_observation as fixtures

    saved = pearl_archive()
    original_fixture = fixtures.plugin_fixture
    original_collector = handoff.collect_probe_report

    def fixture(*values, **kwargs):
        owner, store, sent = original_fixture(*values, **kwargs)
        store.update(copy.deepcopy(saved))
        return owner, store, sent

    def collect(result, probes, setup_checks):
        original_collector(result, probes, setup_checks)
        result['test_scope'] = 'resident_exploration_to_current_panel_with_saved_archive'
        result['archive_storage_changed'] = False
        result['archive_source'] = 'read_only_copy_of_player_store_pearl_only'
        result['current_panel_source_checks'] = []
        for packet in result.get('formal_results', []):
            guidance = packet.get('guidance_answer') or {}
            archived = (guidance.get('imported_builds') or {}).get('characters', [])
            result['current_panel_source_checks'].append({
                'answer_state': (packet.get('query_answer') or {}).get('state'),
                'archive_panel_totals_exposed': any((row.get('panel') or {}).get('values')
                    or (row.get('panel') or {}).get('stats') for row in archived),
                'current_attribute_rows': [
                    {'label': f.get('label'), 'value': f.get('value')}
                    for f in ((packet.get('current_visual_evidence') or {}).get('screen_facts') or {}).get('facts', [])
                    if f.get('scope') == 'attributes' and f.get('kind') == 'stat'],
            })

    args.question = '真珠这面板怎么样，接下来先养哪儿？'
    args.on_page_discovery = False
    args.on_control_change = True
    args.preload_backend = True
    with patch.object(fixtures, 'plugin_fixture', fixture), \
            patch.object(handoff, 'collect_probe_report', collect), \
            patch.object(handoff, 'ReviewProbe', ReaderBudgetProbe):
        await handoff.run(args)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report', required=True)
    parser.add_argument('--signals', required=True)
    parser.add_argument('--port', type=int, default=8463)
    asyncio.run(run(parser.parse_args()))

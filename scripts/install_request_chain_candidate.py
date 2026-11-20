"""Freeze/package the R4-accepted R3 source, then use the existing HSR transaction.

No production fixes, host/helper updates, external services or blind retries.
Only explicit --install performs the user-authorized single-plugin install.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

from . import install_character_page_proof_candidate as transaction
from .install_page_activity_candidate import digest, request, tree
from .verify_request_chain_r4 import BUILD, frozen_sources

ROOT = Path(__file__).resolve().parents[1]
INSTALLED = transaction.INSTALLED
LIVE = ROOT / 'dist/request-chain-r4-live-20261021-2.json'
LIVE_SHA256 = '88e45e4dc03785e8a10be387f7602d30c96e66b09dd92d7c94fb9f77e8481d9b'
AB = ROOT / 'dist/roundtrip-content-ab-final3-replay-20261020.json'
AB_SHA256 = '0d62f55734d8bc608ea376f21b619fab6fee221705ceb8aee90d04ef63ba9c6d'
LAYOUT_SHA256 = 'c77770f7f7b9bb362e454adb7a0cf81b3e52c43eb014e627ae6c5a2fd0f910a7'
DOCS = {'DESIGN.md', 'docs/TEST_RUN_REQUEST_CHAIN_R0_R1_20261021.md',
        'docs/TEST_RUN_REQUEST_HANDOFF_R2_20261021.md', 'docs/TEST_RUN_PAGE_BODY_R3_20261021.md',
        'docs/TEST_RUN_REQUEST_CHAIN_R4_20261021.md'}


def save(path, value):
    with path.open('x', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)


def check_evidence():
    expected = frozen_sources()
    assert digest(AB) == AB_SHA256, 'Original A+B evidence changed'
    prior = json.loads(AB.read_text(encoding='utf-8'))
    assert prior['additional_candidate_hashes_without_recorded_baseline']['detectors/layout.py'] == LAYOUT_SHA256
    assert digest(ROOT / 'detectors/layout.py') == LAYOUT_SHA256
    expected = {**expected, 'detectors/layout.py': LAYOUT_SHA256}
    assert digest(LIVE) == LIVE_SHA256, 'Original R4 input changed'
    live = json.loads(LIVE.read_text(encoding='utf-8'))
    assert live['build_revision'] == BUILD and live['live_core_accepted'] and not live.get('failure')
    assert live['source_hashes_unchanged'] and live['frozen_r3_sources_unchanged']
    assert not any(live.get(key) for key in ('installed_changed', 'helper_pointer_changed', 'helper_executable_changed',
                                           'probe_errors', 'probe_dropped'))
    assert [r['phase'] for r in live['roundtrip']] == ['attributes', 'challenge']
    assert all(r['verdict']['accepted'] for r in live['roundtrip'])
    assert all(digest(ROOT / name) == value for name, value in live['source_hashes'].items())
    for report, count in [('page-body-r3-final2-20261021.xml', 568),
                          ('request-chain-r4-offline-20261021.xml', 87),
                          ('request-chain-r4-harness2-20261021.xml', 28)]:
        suites = ET.parse(ROOT / 'dist' / report).getroot().findall('testsuite')
        assert sum(int(s.get('tests', 0)) for s in suites) == count, report
        assert not any(int(s.get(key, 0)) for s in suites for key in ('failures', 'errors', 'skipped')), report
    return expected, live


def allowed_change(name, accepted=()):
    if name in {'detectors/exploration.py', 'detectors/layout.py'}:
        return name in accepted
    return name == '__init__.py' or name == 'DESIGN.md' or name.startswith(('runtime/', 'docs/', 'tests/', 'scripts/'))


def payload_names(baseline, evidence):
    names = set(baseline) | DOCS
    names.update(p.relative_to(ROOT).as_posix() for p in (ROOT / 'runtime').glob('*.py'))
    names.update(name for name in evidence if not name.startswith(('tests/', 'scripts/', 'dist/')))
    assert not any(name.startswith('dist/') or '..' in Path(name).parts for name in names)
    return names


def prepare():
    from plugin.neko_plugin_cli.commands.validate_cmd import validate_plugin_dir
    from plugin.neko_plugin_cli.core import build_plugin, inspect_package

    expected, live = check_evidence()
    baseline = tree(INSTALLED)
    assert baseline and '__init__.py' in baseline
    run = ROOT / 'dist' / ('request-chain-candidate-' + datetime.now().strftime('%Y%m%d-%H%M%S'))
    run.mkdir()
    stage = run / 'hsr_companion'
    names = payload_names(baseline, {**live['source_hashes'], **expected})
    changed = []
    for name in sorted(names):
        source, target = (ROOT / name).resolve(strict=True), (stage / name).resolve()
        assert source.is_relative_to(ROOT) and target.is_relative_to(stage)
        if name in baseline and digest(source) != baseline[name]:
            assert allowed_change(name, {**live['source_hashes'], **expected}), 'Unexpected out-of-scope change: ' + name
            changed.append(name)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    issues = validate_plugin_dir(stage)
    assert not any(level == 'error' for level, _ in issues), issues
    package = run / ('hsr_companion-0.9.6-' + BUILD + '.neko-plugin')
    build_plugin(stage, package)
    inspected = inspect_package(package)
    assert inspected.package_id == 'hsr_companion' and inspected.package_type == 'plugin' and inspected.payload_hash_verified
    assert all(digest(ROOT / name) == value for name, value in expected.items())
    candidate = {'build_revision': BUILD, 'plugin_version': '0.9.6', 'package': str(package),
        'package_sha256': digest(package), 'source_hashes': tree(stage), 'baseline_hashes': baseline,
        'changed_baseline_files': changed, 'new_files': sorted(names - set(baseline)),
        'check': issues, 'installed': False, 'regression_evidence': [568, 87, 28],
        'live_evidence': str(LIVE), 'live_sha256': LIVE_SHA256,
        'prior_page_evidence': str(AB), 'prior_page_sha256': AB_SHA256,
        'frozen_source_hashes': expected, 'real_cat_validation': 'pending',
        'remaining_issues_not_fixed': ['unknown_exploration_controls', 'changing_control_probe_cost',
                                     'optional_mechanic_fragments', 'cold_formatter_timing']}
    save(run / 'candidate.json', candidate)
    print(json.dumps({'candidate': str(run / 'candidate.json'), 'files': len(candidate['source_hashes']),
                      'changed': changed, 'added': candidate['new_files'], 'check': issues}), flush=True)


def stable_player_records():
    database = transaction.APP / 'data-packs/hsr_companion/player-state/store.db'
    with sqlite3.connect(database.as_uri() + '?mode=ro', uri=True) as db:
        rows = db.execute('SELECT key,value FROM kv_store WHERE key IN (?,?,?,?,?) ORDER BY key',
            ('manual_player_builds', 'uid_import_state', 'player_context', 'character_corrections', 'pending_profile_proposal')).fetchall()
    return hashlib.sha256(repr(rows).encode()).hexdigest()


def install(candidate):
    candidate = candidate.resolve(strict=True)
    assert candidate.is_relative_to(ROOT / 'dist')
    check_evidence()
    before = stable_player_records()
    context = request('/plugin/hsr_companion/hosted-ui/context?kind=panel&id=demo', timeout=20).get('state') or {}
    save(candidate.parent / 'runtime-before-install.json', {
        'observer': context.get('observer'), 'candidate_revision': BUILD,
        'stable_player_records_sha256': before, 'real_cat_validation': 'pending'})
    # Reuse the established standard host single-plugin transaction and backup,
    # changing its candidate metadata only, never its behavior or file targets.
    transaction.REVISION = BUILD
    transaction.install(candidate)
    after = stable_player_records()
    save(candidate.parent / 'stable-player-records-after.json', {'before': before, 'after': after, 'unchanged': before == after})
    assert before == after, 'Player roster/profile/corrections changed'


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--install', type=Path, help='Install exactly this already checked candidate, with backup')
    args = parser.parse_args()
    install(args.install) if args.install else prepare()

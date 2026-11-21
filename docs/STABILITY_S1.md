# Stability S1: current character reads without a full-frame dependency

Base: S0 a8bd562b2f0c81997140e7b934e83e02a0d89fb1 (draft PR #17).
This is a behavioral stability fix, not another move-only layering change.
The installed P8, host and capture assistant are unchanged. S2 manual-refresh
completion and S3 chat-activity retries are explicitly outside this commit.

## Root cause and scope

The frozen expired-role samples in tests/fixtures/stability/p8_baseline.json
remain historical failures (old 7.156 s, P8 7.141 s). The existing regional reader
required a committed region plan; character-detail identity had none. A cold or
expired question consequently joined full-screen OCR, despite needing one name.
Warm cache success alone did not cover this path.

## Execution changes

- L2a detectors/character_regions.py: classify the CURRENT capture with the
  existing thumbnail page classifier, then read native character-name and, only
  when requested, attribute regions. Geometry comes from current labels with
  bounded layout fallbacks; neither a crop nor a database name grants identity.
- Factor the existing native/enlarged exact-name gate into contracts, unchanged.
  A thumbnail name, alias or uncorroborated name cannot pass this gate. Ambiguous
  subjects remain unknown. No new OCR backend or multi-frame numeric assembly.
- L2b current_regions.py: keep source-frame time and PID; recheck current page
  controls before publishing even a page preview. Run the same scoped reviewer
  and final ObservationSnapshot boundary as other queries. A valid region is
  cached separately from the raw producer result, with detached ownership.
- The existing single-worker scheduler yields a full pass at its next native
  checkpoint while a region consumer is waiting. A cancelled waiter retains its
  still-running native worker; subsequent admission drains it before inference.
  No native call is forcibly stopped. An uninterruptible backend remains a risk.
- Same-revision raw identity absence cannot erase a separately reviewed identity
  packet. The receipt binds its content and reviewed identity coordinates; newer
  revisions, challenges, page/object changes, stop and new question still revoke.
- Detached review does NOT refresh raw fact leases or global pixel guards. A
  page-only preview may get only its independently admitted control guard, so
  background animation alone does not force a no-control coarse guard loop.
- A supported partial regional read returns explicit missing fields rather than
  spending the remaining budget on an unrelated full pass. Existing follow-up
  completion decides whether more evidence is needed. It is not a complete-role
  success and does not change chat-activity retry behavior.

Unchanged: cache age caps, fresh-frame bounds, direct answer/manual/follow-up
budgets, relic completeness, item identity protection, stored roster and teams.
No public build/version promotion is made in this source-only stage.

## Evidence and validation

New tests test_current_character_regions.py use synthetic frames/native work:
current-frame identity and attributes without a previous commit; actual parsers
and name reread gate; original capture timestamp; formal prepare tool; lifecycle
and PID/pixel changes; receipt tampering; missing name; subsequent role revision;
thread cancellation/draining; checkpoint preemption with nonzero simulated cost;
no renewal of old fact leases or global guards. They do not prove real OCR speed.

The first valid red baseline was 6 failed / 3 passed. Subsequent failing local
reports remain in the untracked external logs directory, including an identity
receipt geometry mismatch and a helper-name collision fixed during development.
Existing test assertions are unchanged. Three ignored historical materials are
copied locally for regression coverage and are not part of Git or the PR.

## Live acceptance after explicit installation approval

Do NOT replace the installed plugin automatically. Before any change:
1. Copy the entire currently installed plugin to a new timestamped backup and
   record a SHA-256 manifest, source commit, version and player-data locations.
2. Build this branch with a distinct trial stamp and source SHA. Preserve the
   previous installation by rename outside plugin discovery; never mix files.
3. Stop only the verified plugin-worker PID if needed. Do not stop unrelated
   plugins, host or game, or replace the original capture assistant.
4. Verify the new startup version/SHA before counting any live result.

Minimum live cases, arranged one step at a time:
- Cold/expired current role: keep the same character detail page. Developer
  confirms cache is absent/older than 60 s from actual logs, then player asks
  the original role question once. Record region/full/cache path, real capture,
  waiting time, timeout and delivered identities. Do not infer expiry from waiting
  on the clock if the resident observer has already refreshed the cache.
- Warm role: within 60 s, ask the same question once on the same page. Confirm
  current guarded cache reuse and identical role; do not count a model answer
  without a formal tool call as cache-path acceptance.
- Change protection: while a role question waits, switch to another character
  detail page (or exploration). Old identity must not be delivered as current;
  the new role may be returned after its own proof, or explicitly remain unknown.

Read the current attribute values only if the query selected attributes; no
extra full relic or saved-team live rerun is required for S1. S2 manual refresh
and S3 USER_CONTEXT_GET failures stay separately recorded, not declared fixed.
A source tool success is not proof the host model spoke correctly.

## Rollback procedure (only after an approved trial installation)

Stop only the re-verified S1 plugin-worker PID; rename the S1 trial directory to
a preserved location outside discovery; restore the entire P8 installation from
the new pre-S1 backup (or rename back its preserved directory). Restart/reload
only the plugin and verify the previous version/SHA. Keep trial logs and all
backup directories. Do not clear player data or change host/helper configuration.
Existing original backup: E:/code/neko2/backups/hsr_companion-before-p8-20261121-200958.
That is a 0.9.7 backup, NOT a substitute for backing up the current installed P8.

## Final offline gates

- Full pytest (single foreground run): 2527 passed, 10 skipped, zero failed/errors; 201.966 s.
- New S1 cases: 25 passed; existing assertions unchanged.
- Ruff E4/E7/E9/F/I, line length 120: passed.
- Import-layer gate: 5 passed; zero violations; baseline unchanged.
- Thirteen existing warnings (settings deprecation / JUnit record_property compatibility).
- No new real-game or host-model acceptance is claimed.

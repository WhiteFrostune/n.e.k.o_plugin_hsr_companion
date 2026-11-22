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

- L2a detectors/character_regions.py: assemble CURRENT native control/name
  crops into one compact atlas, then use the existing page classifier and exact
  native/enlarged name verifier. No full-frame thumbnail precedes this path.
  Prior same-size coordinates are acquisition hints only; cached labels, IDs and
  values are never passed. Invalid/resized hints use bounded layout acquisition.
  Attribute regions are read only when requested. Neither a crop nor a database
  name grants identity.
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

## S1 follow-up: pre-native admission and compact acquisition (2026-11-22)

Approved changes only: central native-call preemption and character-detail
acquisition. Installed S1 efa451a and the original helper remain unchanged.
No answer/manual/follow-up deadline, cache-age cap, score threshold or native /
enlarged agreement predicate changes. No additional model or host changes.

The single isolated request exposed a pre-native checkpoint gap: a region waiter
already existed when the background producer started an 11.230 s native call;
the region consumer waited 7.054 s and timed out before name/page/review stages.
The saved previous chat has a DIFFERENT timing (admission at +0.219 s); this is
not a claim that its post-admission failure has now been explained.

Changes:
- Put the preemption check in the existing read_native_text backend-call helper,
  covering the initial scene probe and name confirmation as well as tiles. Remove
  the immediately redundant unresolved-atlas check. A call already in progress is
  retained until its actual return; no async cancellation is sold as thread exit.
- Replace the full-frame thumbnail + separate native name read with current
  control/name crops packed at scale 1 into a bounded native atlas. Reuse the
  existing page classifier; remap only boxes wholly contained in a single chip.
  Do not accept seam-spanning boxes or suppress alternative subjects in the name
  header by a narrow old-name crop. All accepted identities still need the
  unchanged native/enlarged confirmation and final current-frame snapshot review.
- Pass only coordinates and original dimensions from prior character layout.
  Current native controls/name determine results. No old identity/value import,
  no two-frame fact merging, and no new query keyword/page-classification rules.
- Update one existing test fixture to locate glyph markers in actual crops rather
  than returning boxes for the old call order. Its identity, score-method and
  numeric-result assertions remain; expected OCR count changes from four to three
  for attributes because control/name acquisition is combined. This assertion
  change was explained before editing, and does not relax a correctness gate.

New counterexamples cover waiting before the first probe, demand arriving while
preparing that probe, demand before name confirmation, actual in-flight native
work, absent/wrong controls, weak/disagreeing/ambiguous names, atlas seams,
resized/malformed prior geometry, cancellation, and geometry without cached text.
Initial new-contract red run: 8 failed / 5 passed, including unavailable new API
arguments. Later broader tests retain all first failures locally.

Work comparison uses the SAME synthetic marker frame and a backend substitute,
NOT a real-model benchmark: character-only OCR calls 3 -> 2; sampled pixels
494082 -> 428088 (cold layout), or 347136 (prior-layout acquisition). Real OCR
latency and the original chat's post-admission gap remain unverified here.

The related legacy relic timing suite failed three cases in a targeted run.
An isolated countercheck using the actual pre-change read_native_text function
also failed its unchanged selected case under the same environment. This is
retained as a pre-existing timing sensitivity, NOT waived or marked passed.
No timeout constants, skip markers or old relic assertions were altered.
The final full-suite gate is reported separately with its actual outcome.

Known limits: a required native call may itself exceed the direct-answer budget;
compact geometry alone does not promise latency. Unknown layouts may remain
unknown. Chat-activity follow-up and manual-refresh completion stay outside this
patch. Installation and further live checks require separate confirmation.

### Follow-up gate outcome: BLOCKED, not committed

First pre-commit full run: 2546 passed / 3 failed / 10 skipped (432.654 s).
One failure was introduced by retaining the outer tile check after centralizing
pre-native admission. The duplicate was removed; the unchanged original
between-tiles assertion now passes. Corrected-code targeted run: 34 passed /
1 failed (a 1-second cancellation fixture expired before its second read).
The 22 new S1 follow-up cases pass. No old priority/cancellation/relic assertions
or test durations were relaxed.

Remaining two failure families have baseline counterexamples:
- Original read_native_text, unchanged late-relic timing case: timeout again.
- Entire original efa451a source, unchanged empty-advice/late-slot case: timeout
  again (one read before the 1-second fixture deadline).
These counterexamples show the failures are not newly introduced by the two
approved changes; they do not make the tests passed or prove the product stable.

Corrected-code Ruff passes. Layer gate: 5 passed, 0 violations; baseline unchanged.
The full gate is NOT cleared; there was no second full-suite run to seek green.
No commit/push/install is made. The installed payload still matches efa451a.
A separate decision is needed before changing legacy test-clock/scheduling
fixtures; product deadlines and correctness assertions must remain unchanged.

### Subsequent test-only closeout

The blocked run above is historical evidence and remains preserved. The
separately approved clock/scheduling step reproduced cancellation on unchanged
efa451a before changing fixtures. With no further product changes, the combined
candidate now passes all gates. See STABILITY_S1_TEST_CLOCK.md for actual
counts, deadlines, assertion preservation and evidence limits. The two steps
are committed separately; this is not permission to replace the installed build.

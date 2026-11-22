# S1 test clock and scheduling follow-up

Scope: test fixtures and test support only. The previous S1 follow-up remains
separate. Product source, deadlines, identity confirmation, layering baseline,
installed plugin, host and capture assistant are unchanged by this step.

## Baseline first, no retries to seek green

The exact unchanged `efa451a905eed50b8d427dacbd2bc9f6f869906e` source archive was
used before editing tests. `test_empty_advice_cannot_send_or_complete_without_valid_evidence[cancel]`
failed once: expected `user_cancelled`, got `no_new_verified_facts`. The fixture's
one-second deadline was exhausted before its second read could cancel. Both its
test file and followup_delivery.py were compared to git-show content (also
native_text.py); they match. This is a baseline scheduling counterexample, not
a newly introduced S1 cancellation regression. It does not prove all real
cancellation behavior is stable.

Local retained evidence (not published):
- s1-cancel-baseline-once-20261122.xml
- s1-test-clock-source-verification-20261122.json
- Previous full-suite failures and baseline relic/follow-up failures remain.

## Test changes, not product fixes

`tests/controlled_time.py` supplies a module-local monotonic clock and scheduling
proxy. It never patches stdlib time or asyncio globally. Scenario sleeps and
explicit backend costs advance logical time. Native execution and the normal
parsing, verification, snapshot and delivery code are retained. Waiting for
thread completion does not consume modeled OCR time a second time.

- Follow-up fixtures retain deadlines 1, .06, .35 and .035 seconds. Idle polling
  advances the clock, so the second packet/cancellation happens at a specified
  scheduling turn. The visual-event fixture explicitly schedules its idle
  timeout. Formatting/CPU cost on this machine does not decide the scenario.
- Relic replay retains a 13.75-second late join, 8-second query deadline,
  5-second modeled relic-bottom and unrelated-tile costs. The first native
  call is released ONLY after the actual query-joined-worker trace. No result
  or validation gate is stubbed to force success. Exact color markers are
  located with Pillow masks instead of millions of Python pixel iterations.
- The first-tile readiness check waits for its event instead of testing whether
  the machine happened to reach it in two wall seconds. A separate 30-second
  deadlock guard fails a stuck test. That is test infrastructure, not an
  enlarged product/query deadline or a relaxed latency assertion.
- Every original domain assertion is preserved (15 follow-up / 16 relic
  assertions verified by AST); an extra worker-release guard is added. Complete
  main/four substats, character ownership, forbidden unrelated work, cancellation,
  no fabricated send and the original less-than-8-seconds assertion stay intact.
- Scheduler tests independently reject work at/after the unchanged 7.15-second
  effective budget and check cancellation/draining and local patch isolation.

These deterministic tests cover ordering and deadline semantics; they are not
real OCR latency benchmarks. Native in-flight latency and live chat remain
installation-acceptance items. No new live test is requested here.

## Gates and delivery

Results are appended after the single pre-commit full-suite run. Commits/push
are allowed only after pytest, Ruff and layering all pass. S1 behavior and this
test-only follow-up are committed separately. Installation requires approval.

Final results (one full-suite run, no rerun to seek green):
- Baseline cancellation: 0 passed / 1 failed (original unmodified assertion).
- Related protection + scheduler boundary tests: 68 passed / 0 failed.
- Full pytest: 2554 passed / 0 failed / 10 skipped (412.468 s).
- Ruff: passed (`--select E4,E7,E9,F,I --line-length 120`).
- Independent layer gate: 5 passed / 0 violations; baseline unchanged.
- The existing 13 warnings concern deprecated host settings and JUnit
  record_property reporting. None was suppressed or promoted into a skip.
- Installed S1 efa451a payload: 129/129 hashes unchanged. No install/live test.

Historical red/full-run XML and local timing reports were retained; the green
result does not erase them. Two separate commits follow these gates: first the
previous S1 preemption/compact-region changes, then this test-clock-only step.

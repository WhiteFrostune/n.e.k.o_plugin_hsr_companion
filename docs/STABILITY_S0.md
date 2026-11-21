# Stability S0: evidence and timings only

Base: P8 `00a7be4c731992d4012c0994b11e0f1366ad5677` (draft PR #16).
This stage does not fix recognition or change readiness, freshness, deadlines,
cancellation, retries, or installation. S1 and S2 remain separate work.

## Frozen baseline

`tests/fixtures/stability/p8_baseline.json` contains seven sanitized observations:

| Sample | Actual observation | Boundary |
| --- | --- | --- |
| 0.9.7 expired | 7.156 s timeout, zero deliverable characters, joined full worker | Valid expired tool sample |
| P8 expired | 7.141 s timeout, zero deliverable characters, started full analysis | Valid expired tool sample |
| 0.9.7 unexpired | No new candidate in the 240 s preparation window | No tool invocation; never count as a pass |
| P8 unexpired | Approximately 19 s old cache revalidated in 1.110 s, one character | Old pending status belongs to the preceding request |
| 0.9.7 manual | Approximately 20 s timeout | Historical, not a new live test |
| P8 manual | Approximately 20 s timeout | Historical, not a new live test |
| P8 coarse revocations | Repeated no-control revocations before the role query | Animation as cause is not established |

The controlled role cases retain identical supplied tool arguments; they do not
recover unknown model-generated arguments from the earlier natural chat. Source
SHA-256 references permit local comparison without publishing original files or
paths. Request IDs are replaced. Only a generic reproduction prompt remains: no
player names, UID, equipment values, chat logs, images, tokens or raw errors.
Three existing ignored regression materials remain local and are never staged.

## Existing channel, additional receipts

- Extend existing `vision_trace.measure`: context-local IDs propagate into the
  native worker. Return values and original exceptions/cancellation are preserved.
- Measure query coordination, lock/settings/wait, regions, cache review, producer
  and manual refresh. Add no transport wait, background task, retry, deadline or file IO.
- Mirror existing refresh events into bounded observer diagnostics. Keep the
  per-query stage order; count parsed, verified and actually committed outputs.
- A producer started before a question need not carry that question's ID.
  `query_joined_worker` links its real job/frame rather than rewriting its past.
  Source frames and verification frames have separate IDs; serial is not frame ID.
- Manual refresh uses a diagnostic ID derived from its existing request timestamp.
  Its final state, baseline serial, elapsed time and error class are recorded. This
  ID is never an observation, readiness, or scheduling token.
- Missing regional data returns normally with its existing reason; internal errors
  retain their error class. Neither is reported as recognition success.
- Export retains allowlisted IDs/counts/stages/error classes only. Buffers stay at
  48 events and may truncate old events; this is not durable full history or proof
  the host spoke. Recording failure cannot replace the original result/exception.
- Business decisions never consume the additional diagnostic metadata.

## Acceptance

Correlation across concurrent requests and threads; no context leak. Original
return, exception and cancellation preserved. Distinct missing-region and internal
error receipts. Joinable parse/verify/commit counts without values. Export privacy
and 48-event bound. Historical failures remain failures and missing preconditions
remain untested. Run full pytest, Ruff and import-layer gate before committing.

S0 makes no new real-game claim and leaves the installed plugin/host/helper alone.
First new game checks belong to the isolated S1 candidate, with minimal user input.

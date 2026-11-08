# Engineering log

Dates below use UTC unless an older entry explicitly says otherwise. Group dates identify the documented phase; they do not invent exact times for undated events. Earlier entries are retained below, including superseded CI conditions.

## 2026-11-02 — registration

- **Registration:** the owner froze 17 cells and three paired seeds on November 2, 2026. Frozen formulas, guardrails, S0 per-job receipt extras and S2 trailing-window recovery supersede illustrative defaults. No thresholds were selected after seeing official results.

## 2026-11-03 — interrupted capture

- **Capture setback:** the original batch stopped during the November 3 shutdown without an exit code. Slots 1–29 survived. Slot 30 held a zero-byte reservation with no receipts; the lost execution could not be reconstructed.

## 2026-11-05/06 — recovery, analysis and reporting

- **Recovery decision:** the owner approved counting that reservation as ABANDONED attempt 1, retaining it and the 29 valid files unchanged, and allowing only two remaining slot-30 attempts. An additive sidecar binds both runner identities. [The decision](recovery-decision-20261105.md) discloses the absence of a historical capture-time hash anchor.

- **Health failure:** slot 46 S3/C2 round 3 attempt 1 exceeded the frozen health limits. It remains INVALID, with its raw evidence intact. Attempt 2 is VALID. Reducing sample counts, dropping slow observations or raising limits was not permitted.

- **Completed evidence:** 51 VALID results, one INVALID attempt and one ABANDONED attempt; 53 attempts and 55 immutable artifacts. The independently audited COMPLETE manifest was merged as G8. Invalid and abandoned attempts never enter valid statistics.

- **G9 analysis:** read-only reconciliation precedes every report generation/check. Metrics come from logical records and authoritative provider receipts; measured health percentiles are recomputed from raw samples. Round pairing, nearest-rank percentiles, null recovery and zero-baseline decisions are explicit. Request pooling and significance claims are excluded.

- **Reporting decisions:** report all registered C0 and stepwise comparisons, even when HARMFUL or NO MEASURABLE EFFECT. The S2 timeline selects each configuration's median primary-metric run and shows the full three-run per-bin range. This descriptive illustration does not replace paired decisions. Work executions are a cost proxy, without invented currency pricing.

- **Reproducibility:** use existing locked Matplotlib, fixed SVG IDs and no date metadata. The original CI workflow is hash-locked, so a separate minimal workflow runs report freshness with full Git history. No dependency or frozen-source changes are needed.

- **Rendering setback:** the staged whitespace check caught trailing spaces in Matplotlib SVG path lines. The generator normalizes only line-ending whitespace; chart values and visual geometry are unchanged. Rendering determinism and freshness are rechecked after this formatting correction.

- **CI setback:** GitHub rejected the new report workflow before creating jobs. Local YAML validation identified the unquoted install command's colon; quoting that command fixes the parse error. The unnecessary job-level cache expression was also removed because the generator already selects a project-local cache. Freshness must pass on the corrected PR head; the frozen original workflow remains unchanged.

- **Fresh-checkout integrity setback:** CI passed all 549 tests but its existing teardown guard detected access-time changes from ordinary reads. G9 now uses owner-only no-atime reads and frozen in-memory validators, and its tests copy official inputs through that reader. A fresh-copy regression checks every metadata field using the existing integrity helper. No timestamps are restored or edited, and the original guard and capture runner are unchanged.

- **Scope clarification:** earlier work stopped because the full brief was absent from the VM. The owner then supplied the authoritative G9 requirements and explicitly removed that file requirement; no substitute brief was invented.

## 2026-11-07 UTC — supplemental validation history

- **Supplemental CI validation (execution #47):** on PR #9 head `c862dcaefe72dd4f56847a1062de54c00570bca2`, both attempts of original CI run [42181246988](https://github.com/shaikh-muhammad/ai-provider-outage-test/actions/runs/42181246988) were CANCELLED after exceeding the frozen five-minute job limit. GitHub annotations identify that timeout; setup and dependency checks passed, pytest was interrupted without a reported test failure, and the prohibited-call check was skipped. The separate `ci-extended.yml` pull-request workflow uses ten minutes with the same pinned actions, Python, dependencies, full pytest selection and prohibited-call check, retaining `contents: read`. The original workflow and its registered hash remain unchanged. Supplemental success is not a passing result for the original workflow; accepting it in place of original-workflow success still requires a formal acceptance exception. No tests, assertions, experiment settings or official evidence were changed, and no local suite or experiment was rerun for this addition.

## Continuing limitations

- **Residual limits:** synthetic loopback traffic on one shared machine, three runs per cell, limited profiles, seed and scheduling variation, arrival-dependent token-bucket faults, and simulator-specific idempotency. These observations do not guarantee real-provider performance, prices, availability or exactly-once behavior.

## 2026-11-07 — final main and audit handoff

PR #9 merged at 2026-11-07T04:20:21Z as 4ab102d5bed20be524e1018e1b0e5d40db356a53. On its exact head 98707b234215e6e35b3347e11599bd2812076e70, original CI 42337014597 passed 552 tests in 195.74 s, supplemental CI 42337014635 passed 552 in 271.31 s, and report freshness 42337014617 succeeded. Both prohibited-call steps passed. The earlier two cancellations remain cancellations. The earlier conditional acceptance-exception statement is not a claim about this later independently successful original workflow.

Execution #48 resumed the existing G10 pack additively: 122 original files retained; 275 final files and 63 raw command bundles. This was a Codex self-assessment and handoff, not independent audit approval. All 55 official files retained bytes, hashes, paths and recorded file metadata. The old helper also compared directory access times and failed for 21 directory atime changes; it was not relabeled as a strict pass. No timestamps were restored. Exact Brief v1.3 section compliance remained NOT VERIFIED because its source was unavailable; historical capture-time integrity, lost-run receipts, original batch exit and shutdown initiator remained unavailable.

## 2026-11-07 — A2 rationale record (G-1)

**Contemporary evidence, not a new approval:** commit 163a02c (2026-11-02 UTC), src/outage_test/client.py, already implements four attempts, five-second C0 attempt limits and 0.5-second waits. Its Breaker docstring states: “HALF_OPEN neutral outcomes (429, other 4xx, invalid 200, cancellation or programming error) release the probe but remain HALF_OPEN.” The frozen November 2 registration records HTTP 429 as retryable but neutral to the breaker. Neutral observations count as nonfailures in the CLOSED denominator; in HALF_OPEN, a 429 releases the probe without closing it. Only a validated success closes; relevant timeouts, connection failures and 5xx responses can reopen.

**Retrospective reasoning written during execution #50, not historical approval:** four attempts bound a deliberately simple retry baseline; a five-second per-attempt limit gives it a more generous wait than C1's two-second attempts; a fixed half-second wait gives a deterministic comparator for randomized backoff. These are design explanations, not evidence that these numeric choices were optimal. No contemporary rationale for choosing those exact C0 values was found in the inspected Git history.

**Retrospective HTTP 429 rationale:** a rate-limit response reports capacity pressure rather than proving service unavailability. Treating it as neutral avoids counting it as an availability failure, but counting neutral observations in the denominator can delay opening. This policy is not claimed universally safe or optimal. No contemporary explanatory approval of that rationale was found; no approval record exists for this new retrospective justification.

## 2026-11-07 — simplification and size history (G-2)

- Actual history: eee376c (2026-11-02 UTC) aligned retry order with the frozen registration; that was a correctness correction, not a size reduction. Commit 3859633 (2026-11-06 UTC) changed evidence reads/validation to preserve metadata; it added code and is not described as simplification.
- Execution #44 tried a synthetic fixture cache, then removed the entire wrapper, decorator and import. The saved local execution44-command-report.json records insufficient CI margin and a bool/int key distinction. No optimization was committed, and the observed timing reduction was not a controlled causal estimate.
- Execution #50 extracts plotting into bench/charts.py instead of growing the report module. This extraction and its <=250-line chart target are explicitly authorized by the owner in this execution; report.py targets <=400 lines. It adds no dependency, runtime mechanism or new experiment.
- Existing merged code is not evidence of an explicit size exception: **no approval record exists** for the inherited approximate module/function guideline overruns listed below. New reasoning does not retroactively authorize them. Counts are physical spans including comments/docstrings; no blank-line removal is used to conceal an overrun.
- Safe deletion/shortening: the reverted cache was removed without losing a test or requirement; the unused report.figure alias is removed by extraction. No evidence, validity check or assertion should be deleted merely to meet a count. Duplicated explanatory prose can be shortened; frozen code requires separate authorization.

## 2026-11-07 — schedule and budget reconciliation (G-3)

**Status: BEHIND. Dated retrospective reconciliation written 2026-11-07 during execution #50.** Original targets were unavailable in the initial repository inspection. The owner subsequently supplied the original dated plan and budgets in this execution, citing **P3_PROJECT_BRIEF.md v1.3, §§14 and 14.1, dated 2026-11-01**. The following planned dates are that owner-supplied source, not invented actual completion dates or a claim that the brief file exists on the VM.

| Original plan day | Planned milestones | Planned completion target |
| --- | --- | --- |
| Nov 1, Day 1 | G0 bootstrap, G1 plan, G2 provider | ~22% |
| Nov 2, Day 2 | G3 clients; start G4 harness | ~44% |
| Nov 3, Day 3 | Finish G4; G5 CI, G6 pilot, G7 registration/review; start G8 official run | ~72% |
| Nov 4, Day 4 | Finish G8; G9 report; G10 audit handoff | 100% |
| Nov 5, Day 5 | Hard-cap RESERVE for failures/fixes only | No new scope |

**Original numeric budget:** four working days target, five maximum; 3–5 focused owner hours/day, approximately 12–20 hours total, plus 1–2 hours Day 0 preparation; approximately 12–15 Codex executions, 3–4/day. The official tmux run was planned at approximately one unattended hour outside owner working hours. These are original targets, not observed durations.

**Actual work-session count:** 49 executions completed before #50 versus 12–15 planned, an overrun of **34–37 executions** at the start. Including the current execution gives **50 total**, or **35–38 above plan**. The November 4 target and November 5 hard cap were both exceeded by the start of #50. Actual focused owner hours, Day 0 preparation time and the complete original execution-by-execution timeline are **NOT VERIFIED**; CI or benchmark durations are not substituted for owner working time. Planned completion percentages above are not claimed as actual progress.

| Gate / milestone | Original planned date | Supported actual milestone / limitation |
| --- | --- | --- |
| G0 bootstrap | Nov 1 | Scaffold committed Nov 1 at 19:38:44−05:00, 2c6427e; full G0 acceptance date NOT VERIFIED |
| G1 plan | Nov 1 | No separate committed plan acceptance record found: NOT VERIFIED |
| G2 provider | Nov 1 | Merge 46f3d2a, Nov 1 at 22:06:33−05:00 |
| G3 clients | Nov 2 | Merge af1ed8c, Nov 1 at 22:53:03−05:00 |
| G4 harness | Start Nov 2; finish Nov 3 | Merge f1d3d38, Nov 2 at 01:39:36−05:00 |
| G5 CI | Nov 3 | Workflow present in e0516fe, Nov 1; separate G5 gate acceptance date NOT VERIFIED |
| G6 pilot publication | Nov 3 | Merge 8cd6d67, Nov 2 at 16:50:58−05:00 |
| G7 registration freeze | Nov 3 | Freeze ba0dc87, Nov 2 at 16:37:55−05:00 |
| G8 official capture / evidence | Start Nov 3; finish Nov 4 | Original startup log 2026-11-03T00:46:43Z; recovery log reports COMPLETE/exit 0 on Nov 5; evidence merged d7cd611, Nov 5 at 19:41:20−05:00 |
| G9 report | Nov 4 | Merge 34bc520, Nov 6 at 01:49:19−05:00 |
| G10 audit handoff | Nov 4 | Existing self-assessment handoff execution #48, Nov 7 UTC; independent audit acceptance NOT VERIFIED |
| README improvement | Not a separately dated original gate | Merge 4ab102d, 2026-11-07T04:20:21Z |

Actual commit times retain their recorded offsets; some occur on the following UTC date. Planned calendar labels are reproduced as supplied rather than silently converted. Git merges establish code-publication milestones, not proof of owner acceptance for an undocumented gate. Available original/recovery runner logs are the capture evidence; the original interrupted exit and lost receipts remain unavailable.
The separate finish-line Part A budget is three executions total: #49 planning, #50 implementation/A2, and #51 reserved for visual review and final correction/validation. Thus #50 uses **2 of 3**, with one execution remaining. One future PR is intended; no PR, push or merge is authorized in #50. Original gate slippage is not fabricated from this later budget.

## 2026-11-07 — presentation and A2 security/negative coverage (G-4 to G-8)

- G-4: tests/test_finishline.py extracts the exact unsafe-call pattern from frozen CI and scans src, bench and scripts. Constructed negative strings verify the pattern catches the prohibited constructs without embedding banned calls in source.
- G-5: the tracked-tree credential scan reuses the five credential-value patterns from the existing evidence test. No file or fixture is exempt. Header names and synthetic admin placeholders are not external credential values; the original evidence/log admin-field check remains unchanged. Matching values are never printed.
- G-6: existing test_missing_recovery_and_s3_gate already rejects one zero-duplicate C0 round for C3/C4. Additional synthetic cases cover one/all zero C0 rounds for both C0/C4 and C3/C4, requiring INCONCLUSIVE and a false efficacy gate. They also cover one/all zero C3 baseline rounds with positive C0 duplicates: NO MEASURABLE EFFECT, never EFFECTIVE, under the unchanged zero-baseline/spread rules.
- G-7: the new S1/S2/S3 chart includes goal metrics and decisive safety checks, all five cumulative configurations, three-run ranges and actual failed paired rounds. Stored decisions are validated through the unchanged comparison function before plotting.
- G-8: dated groupings retain every prior engineering-log entry. This presentation pass retains every HARMFUL verdict, documents untested fallback requirements, and keeps invalid/abandoned accounting and simulator limits visible. PNG previews are local review artifacts, not evidence or committed outputs.
- Verification results are reported from actual command logs after generation; none are presumed here. No experiments, new dependencies, frozen-file changes or audit-pack rewrites are part of this execution.

## 2026-11-07 — complete size inventory at execution #50

The extraction leaves report.py and charts.py below their module targets. Every remaining Python module over 400 lines and function over 40 physical lines is listed, including inherited test and report exceptions. No explicit size waiver was found; the general authorization for this execution is not represented as such a waiver.

| File / line | Module or function | Physical lines | Explicit over-budget approval |
| --- | --- | --- | --- |
| bench/analysis.py:83 | audit | 44 | no approval record exists |
| bench/report.py:90 | results | 67 | no approval record exists |
| bench/run_official.py | module | 507 | no approval record exists |
| bench/run_official.py:182 | assess | 45 | no approval record exists |
| bench/run_official.py:393 | discover | 58 | no approval record exists |
| src/outage_test/client.py:233 | submit | 42 | no approval record exists |
| src/outage_test/harness.py:160 | health_reasons | 70 | no approval record exists |
| src/outage_test/harness.py:258 | validate_run | 54 | no approval record exists |
| tests/test_official.py | module | 644 | no approval record exists |
| tests/test_official.py:224 | test_three_attempt_cap_across_restart | 45 | no approval record exists |
| tests/test_official.py:549 | test_recovery_corruption_blocks_before_capture | 41 | no approval record exists |
| tests/test_provider.py:246 | test_concurrency_queue_overflow_and_duplicate_serialization | 41 | no approval record exists |
| tests/test_registration.py | module | 417 | no approval record exists |

## 2026-11-07 — renderer verification setback

The first focused run passed 64 tests. After the narrow-layout change, the next focused run reported 62 passed and two deterministic-SVG failures. Repeated-render comparison identified changing clip IDs and adaptive subfigure geometry. The renderer now uses a fixed grid and stable ordinal clip IDs without rounding measurements or changing clipping geometry. The same byte-identical and README-isolation assertions remain intact; the corrected focused run passed 64 tests in 41.46 s. The failed outputs remain in local logs/execution50/. No assertion, threshold or tolerance was weakened. Dark variants were dropped to preserve required language, safety visuals and A2 work.

## 2026-11-07 — execution #50 verification and visual stop point

One full offline suite ran in tmux: **575 passed in 569.92 s**, command wall time 571.12 s, exit 0, no skips. It was not rerun. A final annotation-only adjustment moved the C4 callout away from plotted traffic; afterward the complete focused report/A2 suite passed **66 tests in 42.40 s**, exit 0, no skips. The final report generation and report --check both passed. Raw full-suite stdout/stderr and the controller are retained locally under logs/execution50/ and tmp/execution50/full-suite.sh; intermediate failures remain in separate logs.

The scientific chart JSON is field-for-field identical to the baseline after removing generator provenance: only report.py's hash and the new charts.py provenance entry change. Frozen source/registration/workflows/dependency locks, 55 official files (including file metadata), private G6 artifacts and the existing 275-file audit pack remain unchanged. Existing numeric/recomputation/security/tamper test functions are unchanged; only the presentation precision expectation and chart-call interface assertion were updated, with additional checks.

Both SVGs rerender byte-identically, use zero-based axes and valid clipping references, and have accessible title/description text. Rendered text bounds fit their canvases. Desktop and 440-pixel narrow PNGs are local review artifacts, never committed. The README screenshot uses a local Markdown approximation with Mermaid displayed as source; exact GitHub Mermaid styling still needs owner review. The ladder intentionally carries nine panels and paired-round details, so narrow-view density also needs visual approval.

All eight A2 gaps are addressed in this execution; unavailable actual owner hours and undocumented G0/G1/G5 acceptance dates remain explicitly NOT VERIFIED, not filled with estimates. No experiments, new dependencies, branch creation, commit, push, PR or merge occurred. Parts B/C and existing audit logs were not changed. Stop for owner visual approval; Part A has one reserved execution remaining.

## 2026-11-08 UTC (November 7 America/New_York) — Master v2.0 E3, execution #54

**G-3 status: BEHIND.** This is an additive retrospective update; earlier dated entries and their execution-specific budgets remain historical records. The original owner-supplied calendar plan was November 1 G0–G2; November 2 G3/start G4; November 3 finish G4, G5–G7/start G8; November 4 finish G8/G9/G10; November 5 hard-cap reserve only. The original budget was **12–15 Codex executions, a four-day target, five-day maximum and 12–20 focused owner hours**. These targets are not measured owner effort.

The following are local Git committer timestamps, normalized to UTC from the recorded offsets. They establish code or evidence publication, not undocumented owner acceptance. PR numbers come from merge subjects; PR #10 is the existing owner-identified presentation PR, whose head commit is recorded below. Its creation/acceptance date and live hosted status are not inferred from that commit.

| Gate / PR milestone | Supported UTC publication time | Git record / qualification |
| --- | --- | --- |
| G0 scaffold | 2026-11-02 00:38:44Z | 2c6427e; complete G0 acceptance NOT VERIFIED |
| G1 plan | NOT VERIFIED | No separate contemporaneous plan acceptance record located |
| G2 provider / PR #1 | 2026-11-02 03:06:33Z | Merge 46f3d2a |
| G3 clients / PR #2 | 2026-11-02 03:53:03Z | Merge af1ed8c |
| G4 harness / PR #3 | 2026-11-02 06:39:36Z | Merge f1d3d38 |
| G5 CI present | 2026-11-02 02:47:56Z | e0516fe; separate G5 acceptance NOT VERIFIED |
| G6 pilot publication / PR #4 | 2026-11-02 21:50:58Z | Merge 8cd6d67 |
| G7 registration freeze | 2026-11-02 21:37:55Z | ba0dc87; original approval location NOT VERIFIED |
| G8 runner / PR #5 | 2026-11-03 04:48:42Z | Merge f2260d1; not a capture-completion date |
| G8 recovery / PR #6 | 2026-11-05 23:36:59Z | Merge e4e2edd; original recovery approval location NOT VERIFIED |
| G8 official evidence / PR #7 | 2026-11-06 00:41:20Z | Merge d7cd611; publication of preserved evidence |
| G9 analysis / PR #8 | 2026-11-06 06:49:19Z | Merge 34bc520 |
| G10 handoff | NOT VERIFIED as a Git acceptance milestone | Existing execution #48 self-assessment records November 7 UTC; independent acceptance NOT VERIFIED |
| README publication / PR #9 | 2026-11-07 04:20:21Z | Merge 4ab102d |
| Existing PR #10 head | 2026-11-07 07:07:35Z | 3723b38; E2/E3 working-tree changes remain uncommitted |

The owner-supplied historical count before Master v2.0 is **51 executions**. E1 is #52, E2 #53 and this E3 #54: **54 including E3, 39–42 above the original 12–15 target**. The November 4 target and November 5 hard cap have been exceeded. **Owner working hours: NOT VERIFIED**; benchmark/CI durations and elapsed calendar time are not substitutes. Original G1/G7 decision records and authenticated registration/recovery approvals remain NOT VERIFIED. The owner states, “I did not shut down the VM”; the shutdown initiator remains unknown.

The separate Master v2.0 allowance is five execution slots, E1–E5, with E4 conditional on necessary fixes. E3 uses **three of five slots**. This entry does not start E4/E5 or authorize Git writes, experiments or new scope. The earlier Part A budget is not reused as a new authorization.

**G-4 implementation:** the existing pytest scanner now covers every `.py` and `.sh` file under `src`, `tests`, `bench` and `scripts`. It still extracts the exact prohibited-call regex from unchanged original CI; no second scanner or regex copy was added. Assertions require all four roots and both suffixes. Existing fabricated prohibited cases are retained, with additional whitespace cases. Constructed safe examples cover `asyncio.create_subprocess_exec`, a false shell option, safe YAML parsing and a harmless identifier; pattern fragments avoid self-matches. Existing credential and S3 negative-case assertions remain intact.

**G-1/G-2 review:** execution #50 already labels the C0 numeric and HTTP 429 rationale retrospective, not contemporaneous approval. Its simplification/reversion history remains intact. No missing approval is authenticated by this update. E2 left `bench/charts.py` at 187 lines and `bench/report.py` at 387, within 250/392 limits; `tests/test_report.py` is 426 lines, with 54 additions against its reviewed baseline. The existing `results()` function is now 72 lines at line 91, superseding the execution #50 size count of 67; no explicit size waiver is claimed. Other inherited size deviations remain as previously disclosed. Safe shortening could remove the unused report `Path` import; no evidence or assertion needs deletion.

**G-5 through G-9 review:** the existing tracked-file credential scan includes all four replacement SVGs before staging and after owner staging, and asserts obsolete SVGs are absent. The S3 zero-duplicate-baseline cases remain unchanged. E2's six-panel S1/S2/S3 ladder and four light/dark SVGs, objective README, full-precision report and strength annotations are preserved. All seven stepwise HARMFUL comparisons remain HARMFUL: four MARGINAL and three CLEAR. The S2 C3→C4 success decrease is 4.9999999999999964% before rounding, displayed 5.0% and CLEAR. Its boundary and synthetic 0.1%, 90% and mixed-failure tests remain in place. No scientific input or verdict is changed. Narrow annotation density still needs owner visual review; approval is not claimed.

Only the engineering log and existing finish-line test are changed in E3; no `test_hygiene.py` is needed. No full local suite or experiment is repeated. E2's prior full-suite result remains its historical result. E3 command transcripts and actual focused/freshness/protection results are recorded separately under `logs/execution54/`; final results are appended after validation.

**E3 validation:** focused `tests/test_finishline.py tests/test_report.py` passed **80 tests in 40.60 s**, exit 0, with no skips. This includes the exact-CI forbidden-call scanner, fabricated prohibited/safe cases, tracked-file/four-SVG credential scan, S3 negative cases, all G9 labels and presentation checks. `bench.report --check` reported **G9 freshness PASS**, exit 0. The unchanged original-CI forbidden-call command passed, exit 0. A separate review verified all 13 recorded UTC Git timestamps, preservation of the existing pattern extractor/credential/S3 functions, additive log history and all seven G9 strength labels, exit 0. Raw command, stdout, stderr (including empty streams) and exit bundles are under `logs/execution54/`. Validation uses a temporary no-atime directory iterator; no frozen analyzer or tests are patched on disk. Protected hashes, official file/directory metadata and exact E3 scope are checked against `tmp/execution54/before.json` before handoff. Hosted CI has not been run for these uncommitted changes; the owner controls staging and the final committed-tree checks. No owner visual approval or independent audit acceptance is claimed.

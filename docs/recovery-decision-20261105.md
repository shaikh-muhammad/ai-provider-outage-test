# Shutdown recovery decision, November 5, 2026

The owner authorized this narrowly scoped exception in P18-R2, execution 34,
on November 5, 2026. The prior boot journal records poweroff at
2026-11-03 01:17:00 UTC and termination of the batch's tmux pane 71939 at
01:17:01. The batch exit code and shutdown initiator remain unknown.

Retain slots 1–29 and the zero-byte reservation
`results/official/S3/C4/run-2-64520e2f05e143dc816e7961285adf0b.json`
unchanged. The reservation is slot 30, S3/C4, round 2, workload/fault seed
12002. Count it as attempt 1, status ABANDONED, excluded from valid statistics;
only attempts 2 and 3 remain. Continue in registered order, skipping VALID and
exhausted slots. The frozen registration, thresholds, client policies, source
modules, dependency locks and validity limits remain unchanged.

The retained records were independently audited: 29 VALID, zero INVALID,
15,960 jobs, 18,704 receipts and 174,100 loop samples. Their original runner
commit is `f2260d1d6d179fd1f1913d3ff87d1867c48cf571`, runner SHA-256
`c64fa8b158a783209d75972122edc628b95dca4ea5d6795b786832e52f619036`.
The SHA-256 of canonical JSON containing the ordered 29 lists of attempt
descriptors (`path`, `sha256`, `status`, `attempt`) is
`a36780d71956ea49aaef87fe0d8785126cc15ca2304ef143c5b708dbbdba0b66`.

After separate execution authorization, `--record-recovery` validates the
exact original inventory and creates only `recovery-20261105-slot30.json` in
the official directory, using exclusive creation and directory/file fsync.
It does not launch a provider. Normal execution requires that complete,
canonical sidecar, the unchanged reservation and the pinned retained records.
The sidecar binds the owner decision, original and recovery runner identities,
registration, slot, original empty-file hash and retained-record digest. New
attempts record the committed recovery runner's identity; originals retain
their original identity. The final manifest embeds the sidecar and its hash
under the ABANDONED attempt; CLI accounting discloses the abandonment.
Partial sidecars and all other empty/corrupt attempts block; nothing repairs,
overwrites or removes them. A fully durable sidecar can be reused unchanged.

Offline tests cover preservation, accounting, caps, order, manifest disclosure,
corruption and interrupted sidecar creation. This implementation task creates
no real recovery sidecar or new benchmark evidence. A later authorized run
must recheck hashes, provenance, host health and process cleanup.

The empty file contains no execution receipts or encoded attempt/provenance;
attempt 1 is conservatively inferred from the sequential runner. There is no
capture-time manifest or independent historical hash anchor. Current byte
validation cannot establish historical immutability or recover the lost run.

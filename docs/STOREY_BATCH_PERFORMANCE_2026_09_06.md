# Storey replacement performance, 2026-09-06

The reported Berlin Linebrush 6→10 change is command 1066. Its durable command
timestamps span **43.77 seconds** (2026-09-06 08:58:48–08:59:32 CEST). The later
7-storey replacement, command 1067, spans 66.37 seconds. These are server command
times, not an inferred roof calculation duration.

## Isolated benchmark

The real command payloads 1065 (6 storeys) and 1066 (10 storeys) were replayed
in a newly created flat sandbox world using the existing registry. Every run
rolled back its entire transaction. The user world was never edited.

| Same captured geometry, no profiler | Before | After |
| --- | ---: | ---: |
| Initial six-storey placement | 15.55 s | 4.16 s |
| Six→ten replacement, including final flush | 36.89 s | 9.14 s |

The replacement is about **75% faster**. A preceding run of the immutable-ref
version measured 8.59 seconds; timings vary with local CPU/database load.
The final batch executor itself measured 7.12 seconds, with the remainder in
command setup/final database flush. This does not claim instant persistence or
measure the editor's separate geometry preparation, HTTP delivery or rendering.

Before and after have exactly equal voxel arrays, palettes and full chunk
object-reference geometry: 23 active objects, 10 chunks, 9,762 affected cells,
145 append-only events. The benchmark uses the historical 2.645 m request as-is;
the editor's new 3 m default is a separate change, not a reason to alter the
recorded benchmark geometry.

## Causes and changes

- Overwrite ownership used nested old-cell/new-cell comparisons. The original
  profile counted 5.59 million cell comparisons. Coordinate indexes preserve
  world/local legacy matching and last-writer ownership with linear traversal.
- Each removed cell searched all object references again. One index is now
  built per chunk mutation input, including the explicit legacy metadata-only
  exception.
- The 37 child actions deserialized and updated only 10 chunks 145 times.
  An ObjectBatch-local context reuses the current mutable chunk input. Snapshot
  rows keep every intermediate revision/hash for their events but their large
  JSON is flushed only once at batch completion.
- Chunk-reference queries and projections of unchanged object refs are cached
  only within that context. New/changed refs enter the next operation correctly.
- Normalized object refs are recursively frozen within the batch. Immutable
  JSON trees may be shared across successive snapshots instead of repeatedly
  deep-normalizing every lower storey. Mutable voxel buffers are still copied;
  standalone mutation helpers retain their previous mutable result contract.
- A batch writes its aggregate receipt once. Immutable per-object event payloads
  are normalized once and reused while preserving the full persisted audit data.

No cache survives the command, including when an exception triggers rollback.
Independent edits, terrain underlay restoration, imported roof conversion,
last-writer ownership, generation validation and replay receipts remain active.

## Reproduction and regression coverage

`scripts/benchmark_planning_batch.py` accepts exported gzip JSON captures and
the two command row IDs. It writes timings and complete geometry for comparison
and always rolls back its sandbox:

```text
python scripts/benchmark_planning_batch.py CAPTURES.json.gz --before-id 1065 --after-id 1066 --output RESULT.json
```

Tests cover legacy coordinate equivalence, linear coordinate access, latest
ownership, immutable nested containers, ordinary JSON roundtrip, snapshot
revision independence, cache scope/retry cleanup, later child visibility, and
atomic DB success/rollback/receipt replay. The DB batch regression also verifies
one final snapshot UPDATE and an unbroken per-event revision/hash chain.

Validation: 72 focused tests passed; the updated LoD2 fixture then passed all
9 non-HTTP cases (75 distinct passing tests across both runs). Three old LoD2
fixtures first failed identically against both the previous and updated
runtime because they omitted the now-required generation descriptor. They now
exercise the current atomic replacement contract, including retired imported
roofs, preserved mined holes/foreign blocks and rollback. The unrelated full
HTTP startup-audit case was excluded after a controlled interruption.

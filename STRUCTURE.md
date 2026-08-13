# Storage structure

The agreed layout for every store the backend touches. **`pod_loader/paths.py` is the
executable form of this document** — code never writes a path literal, and this file
explains the reasoning that the code cannot.

Enforced in `pod-harness`, `pod-loader-rpc` and every workload repo.

---

## Why this exists

Three incompatible layouts had grown up in one bucket:

| scheme | wrote |
|---|---|
| `runstore.py` | `runs/<region>/<run_id>/` + `regions/<region>/latest.json` |
| `store.py` | `out/index.json`, `out/items/`, `out/profiles/<region>.json` |
| `events.py` | `logs/<pod_id>/jobs/<job_id>/` + `status/<job_id>/` |

Plus data filed under `code/` (`code/corpora/`, `code/corpus_research.json`), obsolete
build artefacts, and leftover probe files. 87,391 objects with no shared vocabulary.

None of that was carelessness — each scheme was reasonable alone. The failure was that
nothing said which one was *the* scheme.

---

## The organising question

Top level answers exactly one thing: **did we GET it, or did we MAKE it?**

```
corpus/                       external input — cannot be recreated, never delete
  raw/<source_id>/            exactly as downloaded, never modified
  recipes/<name>.json         corpus definitions (neutro, cuba)
  corpus_research.json        the source manifest

assets/                       everything WE produce or author
  derived/<stage>/<source>/   pipeline computations — expensive, reusable, deletable
    _derivation.json          what produced this, and with which parameters
  authored/<kind>/            human-made — reference recordings, lesson content
  generated/<kind>/<key>/     on-demand, content-keyed by the request
  profiles/<region>/          promoted outputs
    <run_id>/                 immutable per run
    current                   pointer to the live one

runs/<job_id>/                what happened — prunable by age
  spec.json  status.json  events.jsonl  job.log  console.log
  out/                        what THIS run produced

releases/<version>/           shippable, immutable, pinned by app builds
cache/                        trivially recreatable — HF, MFA, joblib
code/<repo>/<sha>/            scripts only, immutable, CI-published
code/<repo>/dev/              scripts, mutable, for the edit-run loop
```

### Adding a root

A new root must need a **different policy** — different retention, access, or deletion
rules. Different *contents* is not enough; that is what subdirectories are for. This test
is why `generated/` sits under `assets/` (we made it, same as `derived/`) while
`contributed/` does not live here at all.

---

## Three stores, not one

| store | holds | why |
|---|---|---|
| **RunPod volume** | `corpus/ assets/ runs/ cache/ code/` | mounted at `/workspace`, fast, and no credential ever reaches the pod |
| **R2** | client-facing `generated/`, `releases/` | **RunPod's S3 API supports no presigned URLs** (also no ACLs, no versioning) — anything a client fetches by URL cannot come off the volume |
| **R2, separate bucket** | `contributed/` | opt-in user recordings. Consent records, retention limits and deletion requests are bucket-level concerns |

Selected by `PODH_S3_PROFILE` — configuration, not code. See `pod_loader/objectstore.py`.

### `contributed/` — what it is and why it is isolated

Opt-in user audio. Two purposes, both from the product docs: it builds the annotated L2
Spanish corpus that does not otherwise exist, and it is the migration insurance policy —
keeping assessment recordings is what lets the acoustic model change later without
resetting every user's scores.

"Delete everything for user X" has to be a prefix delete in a bucket that contains nothing
else. Never a search across 87,000 corpus objects.

---

## The rules that make it work

### 1. One layout, two views

`corpus/raw/x` the S3 key **is** `/workspace/corpus/raw/x` the file. `paths.key()` and
`paths.path()` convert; nothing hand-writes either.

### 2. Immutable runs, moving pointers

A run writes its own directory and only then moves a pointer. `runstore.py` exists because
the alternative already cost real data: a restarted pod re-ran a job and *"a 132 KB
speaker-assignment map became 77 bytes"*, because every artifact went to a fixed path.

Rolling back is editing a pointer. A second run cannot corrupt a first.

### 3. Existence is not currency

A derived directory that exists may still be stale — produced with different parameters, or
from different inputs. `_derivation.json` records what made it:

```json
{"stage": "normalize", "source": "openslr_108",
 "params": {"sample_rate": 16000, "channels": 1},
 "inputs": {"raw_files": 2507, "raw_bytes": 4139203841},
 "code_rev": "a1b2c3d", "produced_at": "…", "produced_by": "job_01J…"}
```

A stage compares and decides: **fresh** (skip), **stale** (recompute), **absent** (compute).

Not content-addressing: hashing 15.5 GB to decide whether to skip is slower than the work
being avoided. Parameters plus an input fingerprint is cheap, and keeps paths readable.

Not "the directory exists, so skip" either — that is the `.DONE` marker bug, which
*"survived a wiped volume"* and reported `done: 24` over no outputs.

### 4. An index is never the source of truth

`assets/generated/` may carry an index for sweeping and attribution. It must be rebuildable
by scanning, because an authoritative index is the marker bug again in another costume.

### 5. `runs/<id>/out/` versus `assets/`

`runs/<id>/out/profile.json` is *what that run produced*. `assets/profiles/<region>/current`
is *what we decided to keep*. Without the split you either keep every run forever or delete
one and lose something that mattered. **Promotion is explicit.**

### 6. Generated is not authored

A synthesiser produces candidates in `assets/generated/` — content-keyed, no human involved,
served on demand. `assets/authored/` is what a person recorded or wrote. The product docs
require reference audio to be human-reviewed before shipping, so an approved candidate is
*promoted* into `releases/<version>/`; it is not shipped from where it was generated.

---

## Retention

| prefix | policy |
|---|---|
| `corpus/raw/` | never delete — the only thing that cannot be recomputed or re-authored |
| `assets/authored/` | never delete, back up |
| `assets/derived/` | GC when not referenced by a live pointer and older than N |
| `assets/generated/` | content-keyed, so re-requests are free; expire by last-access when cost appears |
| `runs/` | prune by age — ULIDs sort, so it is a prefix range |
| `releases/` | keep forever; app builds pin to versions |
| `cache/` | delete at any time |

---

## Migration from the old layout

| from | to | note |
|---|---|---|
| `corpus/normalized/<src>/` | `assets/derived/normalized/<src>/` | derived, not input |
| `corpus/work/<src>/` | `assets/derived/work/<src>/` | intermediates |
| `code/corpora/` | `corpus/recipes/` | data, not code — **and it collides with `code/<repo>/`** |
| `code/corpus_research.json` | `corpus/corpus_research.json` | |
| `out/_reports/` | `assets/profiles/`, `runs/<id>/out/` | by content |
| `logs/<job>.log`, `.DONE`, `.FAILED` | `runs/<job>/` | markers superseded by verification |
| `.cache/` | `cache/` | |
| `build/`, `_probe/` | delete | images come from GHCR now |

`corpus/raw/` does not move.

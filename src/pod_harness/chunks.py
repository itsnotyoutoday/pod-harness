"""Work through an object store in bounded local batches, because some tools need files.

## The problem this exists for

MFA does not read object storage. It takes a directory of audio and transcripts, forks
workers, and reads real files through the filesystem. Nothing in its interface admits a
bucket. The same is true of ffmpeg, of anything that shells out, and of every stage that
opens a path.

Meanwhile the corpus lives in object storage, because a network volume pins compute to one
datacenter — and measured on 2026-08-13, that pin made pods *unobtainable* rather than
merely slow: identical shapes placed without the volume and returned "no capacity" with it,
four rounds running.

So the corpus is remote and the tools are local. Something has to bridge that, and the
bridge cannot be "download everything": 11.69 GB today, more later, onto a container disk
that also has to hold the outputs.

## The shape of the answer

Fetch a bounded working set, hold it while it is in use, work on it locally, push the
results back, drop it, repeat.

    plan()        divide the work into chunks that FIT, measured from real free disk
    Working()     fetch one concurrently, hand back local paths, evict when done

Three things make it more than a loop:

**Chunks respect the tool's unit of work.** MFA aligns a corpus directory; splitting a
speaker across two chunks would produce two partial alignments rather than one whole one.
`group_by` keeps related items together and never splits a group, even if that makes a
chunk larger than the target.

**A chunk in use is locked.** Eviction is LRU by default, and LRU would happily delete the
directory MFA is reading. The lock is what stops a cache from becoming a bug.

**Fetching is concurrent, working is not.** A remote object costs ~30 ms; fetched one at a
time, 400 of them take 12 seconds and the store looks broken (measured: 8 MB/s). Fetched
32-way the same store returns 125 MB/s. Once local, sequential code is fine — container
disk measured ~150 MB/s, faster than either remote option.

That combination is the point: **concurrency where the latency is, sequential where the
tools are.** It means no stage has to be rewritten to work off an object store.
"""
from __future__ import annotations

import shutil
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Sequence


@dataclass
class Item:
    """One remote object and where it belongs locally."""

    key: str                      # object key in the store
    rel: str                      # path relative to the chunk root
    size: int = 0
    group: str = ""               # items sharing a group are never split across chunks


@dataclass
class Chunk:
    index: int
    items: list
    bytes: int = 0
    groups: int = 0

    def describe(self) -> dict:
        return {"index": self.index, "items": len(self.items),
                "gb": round(self.bytes / 1e9, 2), "groups": self.groups}


def plan(items: Iterable, *, budget_bytes: int | None = None,
         path: str = "/", fill: float = 0.5) -> tuple[list, str]:
    """Divide items into chunks that fit on disk, never splitting a group.

    A group is whatever the tool treats as indivisible — usually a speaker, since MFA
    aligns a corpus directory and half a speaker is not a smaller job, it is a broken one.
    A single group larger than the budget still becomes its own chunk: better to try and
    fail loudly on disk than to silently hand a tool half its input.
    """
    from .resources import plan_chunk_bytes

    why = ""
    if budget_bytes is None:
        budget_bytes, why = plan_chunk_bytes(path, fill=fill)

    by_group: dict = {}
    for it in items:
        by_group.setdefault(it.group or it.rel, []).append(it)

    chunks: list = []
    cur: list = []
    cur_bytes = 0
    cur_groups = 0
    for gname, gitems in by_group.items():
        gbytes = sum(i.size for i in gitems)
        if cur and cur_bytes + gbytes > budget_bytes:
            chunks.append(Chunk(len(chunks), cur, cur_bytes, cur_groups))
            cur, cur_bytes, cur_groups = [], 0, 0
        cur.extend(gitems)
        cur_bytes += gbytes
        cur_groups += 1
    if cur:
        chunks.append(Chunk(len(chunks), cur, cur_bytes, cur_groups))

    total = sum(c.bytes for c in chunks)
    note = (f"{len(chunks)} chunk(s) from {len(by_group)} group(s), "
            f"{total/1e9:.2f} GB total" + (f" — {why}" if why else ""))
    oversized = [c.index for c in chunks if c.bytes > budget_bytes]
    if oversized:
        note += (f"; chunk(s) {oversized} exceed the budget because a single group does — "
                 f"they are not split, and may not fit")
    return chunks, note


class Working:
    """A materialised chunk. Locked against eviction for as long as it is held.

        with Working(chunk, store, root) as local:
            mfa_align(local.dir, out_dir)      # ordinary files, ordinary tools
            local.publish(out_dir, "assets/derived/align")

    Eviction happens on exit, and only then — an LRU cache that deletes the directory a
    subprocess is reading turns a cache into a fault.
    """

    #: Chunks currently materialised and in use, so an evictor can ask.
    _locked: set = set()
    _lock = threading.Lock()

    def __init__(self, chunk, store, root: str | Path, *, workers: int = 32,
                 keep: bool = False, verbose: bool = True):
        self.chunk = chunk
        self.store = store
        self.root = Path(root)
        self.dir = self.root / f"chunk{chunk.index:04d}"
        self.workers = workers
        self.keep = keep
        self.verbose = verbose
        self.fetched = 0
        self.seconds = 0.0

    # -- lifecycle -----------------------------------------------------------------------

    def __enter__(self) -> "Working":
        with Working._lock:
            Working._locked.add(str(self.dir))
        self.materialise()
        return self

    def __exit__(self, *exc) -> None:
        with Working._lock:
            Working._locked.discard(str(self.dir))
        if not self.keep:
            self.evict()

    @classmethod
    def is_locked(cls, path: str | Path) -> bool:
        """Ask before evicting. The cache's LRU must consult this."""
        p = str(path)
        with cls._lock:
            return any(p == l or p.startswith(l + "/") for l in cls._locked)

    # -- the work ------------------------------------------------------------------------

    def materialise(self) -> dict:
        """Fetch every item concurrently. Skips what is already present at the right size.

        Skipping by size rather than re-fetching is what makes a retry cheap: a chunk
        interrupted halfway resumes instead of starting over. It is deliberately not a
        checksum — hashing every object to decide whether to skip costs more than the
        fetch it avoids.
        """
        from .parallel import pmap

        t0 = time.time()
        self.dir.mkdir(parents=True, exist_ok=True)
        cfg = self.store.require()

        def fetch(item):
            dest = self.dir / item.rel
            if dest.exists() and item.size and dest.stat().st_size == item.size:
                return 0
            dest.parent.mkdir(parents=True, exist_ok=True)
            body = self.store.client.get_object(Bucket=cfg.bucket, Key=item.key)["Body"]
            with open(dest, "wb") as f:
                shutil.copyfileobj(body, f)      # streamed: 32 workers holding whole
            return 1                             # objects is memory a pod does not have

        res = pmap(fetch, self.chunk.items, workers=self.workers,
                   label=f"chunk{self.chunk.index}", use_threads=True)
        self.fetched = sum(r for r in res.results if r)
        self.seconds = time.time() - t0
        if self.verbose:
            mb = self.chunk.bytes / 1e6
            print(f"    chunk {self.chunk.index}: {self.fetched}/{len(self.chunk.items)} "
                  f"fetched, {mb:.0f} MB in {self.seconds:.1f}s "
                  f"({mb/max(self.seconds,.01):.0f} MB/s)", flush=True)
        if res.errors:
            raise RuntimeError(
                f"chunk {self.chunk.index}: {len(res.errors)} object(s) failed to fetch; "
                f"first: {res.errors[0][2][:140]}\n"
                f"  Refusing to hand a tool an incomplete working set — a partial corpus "
                f"directory produces a partial alignment that looks like a whole one.")
        return {"fetched": self.fetched, "seconds": round(self.seconds, 1)}

    def publish(self, local_dir: str | Path, prefix: str, *, workers: int = 32) -> dict:
        """Push results back to the store BEFORE the chunk is evicted.

        Order matters and is easy to get wrong: evicting first loses the outputs, and
        there is nothing to re-derive them from once the inputs are gone too.
        """
        from .parallel import pmap

        local_dir = Path(local_dir)
        cfg = self.store.require()
        files = [f for f in local_dir.rglob("*") if f.is_file()]

        def put(f):
            key = f"{prefix.rstrip('/')}/{f.relative_to(local_dir).as_posix()}"
            self.store.put(key, f.read_bytes(), where="chunks.Working.publish")
            return 1

        res = pmap(put, files, workers=workers, label="publish", use_threads=True)
        if res.errors:
            raise RuntimeError(
                f"{len(res.errors)} output(s) failed to publish; first: "
                f"{res.errors[0][2][:140]}\n"
                f"  NOT evicting: the local copy is now the only copy.")
        return {"published": len(files), "prefix": prefix}

    def evict(self) -> dict:
        if Working.is_locked(self.dir):
            return {"evicted": False, "reason": "still locked"}
        freed = self.chunk.bytes
        shutil.rmtree(self.dir, ignore_errors=True)
        if self.verbose:
            print(f"    chunk {self.chunk.index}: evicted, {freed/1e9:.2f} GB freed",
                  flush=True)
        return {"evicted": True, "freed": freed}


def process(items, store, root, fn: Callable, *, budget_bytes: int | None = None,
            workers: int = 32, verbose: bool = True) -> list:
    """Run `fn(working)` over every chunk in turn. The whole pattern in one call.

    Sequential across chunks on purpose: two materialised at once defeats the point of
    bounding disk in the first place.
    """
    chunks, note = plan(items, budget_bytes=budget_bytes, path=str(root))
    if verbose:
        print(f"  {note}", flush=True)
    out = []
    for c in chunks:
        with Working(c, store, root, workers=workers, verbose=verbose) as w:
            out.append(fn(w))
    return out

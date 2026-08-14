"""Prove the chunker does what MFA needs, with a fake store and no network."""
import sys, shutil, tempfile, pathlib
sys.path.insert(0, "src")
from pod_harness.chunks import Item, plan, Working, process

ok = []
def check(name, cond):
    ok.append(bool(cond)); print(f"  {'✅' if cond else '❌'} {name}")

# 1. groups are never split — MFA aligns a speaker directory, half of one is broken
items = [Item(key=f"k{i}", rel=f"spk{i//10}/u{i}.wav", size=10_000_000,
              group=f"spk{i//10}") for i in range(50)]
chunks, note = plan(items, budget_bytes=120_000_000)
split = any(len({i.group for i in c.items} & {i.group for i in d.items}) > 0
            for a, c in enumerate(chunks) for b, d in enumerate(chunks) if a < b)
check("a group never spans two chunks", not split)
check(f"chunks fit the budget ({len(chunks)} chunks)",
      all(c.bytes <= 120_000_000 or c.groups == 1 for c in chunks))

# 2. a single oversized group becomes its own chunk rather than being cut
big = [Item(key="b", rel="huge/one.wav", size=500_000_000, group="huge")]
c2, note2 = plan(big, budget_bytes=100_000_000)
check("an oversized group is kept whole and flagged", len(c2) == 1 and "exceed" in note2)

# 3. a locked chunk is not evictable
class FakeStore:
    class _C:
        bucket = "b"
    def require(self): return self._C()
    @property
    def client(self):
        class C:
            @staticmethod
            def get_object(Bucket, Key):
                import io
                return {"Body": io.BytesIO(b"x" * 1024)}
        return C()
    def put(self, key, body, where=""): return {}

root = pathlib.Path(tempfile.mkdtemp())
small = [Item(key=f"k{i}", rel=f"g{i%2}/f{i}.bin", size=1024, group=f"g{i%2}")
         for i in range(6)]
chs, _ = plan(small, budget_bytes=10_000)
locked_during = []
with Working(chs[0], FakeStore(), root, workers=4, verbose=False) as w:
    locked_during.append(Working.is_locked(w.dir))
    files = list(w.dir.rglob("*.bin"))
check("files land on a real filesystem for tools to read", len(files) == len(chs[0].items))
check("the chunk is locked while in use", all(locked_during))
check("evicted after use", not w.dir.exists())
check("unlocked after use", not Working.is_locked(w.dir))

# 4. process() drives the whole loop and hands each chunk as local paths
seen = []
process(small, FakeStore(), root, lambda w: seen.append(len(list(w.dir.rglob("*.bin")))),
        budget_bytes=10_000, workers=4, verbose=False)
check(f"process() visited every chunk ({len(seen)})", sum(seen) == len(small))
check("nothing left on disk afterwards", not any(root.glob("chunk*")))
shutil.rmtree(root, ignore_errors=True)

print(f"\n  {sum(ok)}/{len(ok)} passed")
sys.exit(0 if all(ok) else 1)

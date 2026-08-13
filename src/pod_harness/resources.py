"""Size the work to the machine, and prove afterwards whether it was sized right.

Hardcoding "8 workers, batch 16" is wrong in both directions: it wastes a 32-core pod and
OOMs a 4-core one. Worse, without measurement nobody finds out — a run that used 6% of the
CPU looks identical to one that used 95%, because the only visible number is wall-clock.

So two jobs here:

    plan_*()   choose worker counts and batch sizes from the hardware actually present
    Sampler    watch utilisation DURING the run and report it, so the plan is falsifiable

## The constraint that actually binds

Rarely core count. For CPU stages it is memory per worker — librosa holds an entire decoded
utterance plus pyin's internal buffers, so processes are the limit. For GPU stages it is
VRAM against batch width, and a batch of long clips padded to the longest one can be far
larger than the average suggests.

## Honesty

`Sampler` reports mean AND peak. A mean of 40% with a peak of 100% is a pipeline stalling on
I/O between bursts, which is a different problem from a flat 40%, and averaging alone hides
the difference.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field


# --------------------------------------------------------------------------------------
# Hardware probe
# --------------------------------------------------------------------------------------

@dataclass
class Hardware:
    cores: int = 1
    ram_gb: float = 0.0
    gpu_name: str = ""
    gpu_count: int = 0
    gpu_mem_gb: float = 0.0
    cuda: bool = False
    # `cores` is what this CONTAINER may use. host_cores is what the machine has — they
    # differ on a pod, and confusing them is how 127 workers got planned for a slice.
    host_cores: int = 0
    cgroup_cpus: float | None = None

    def as_dict(self) -> dict:
        d = {"cores": self.cores, "ram_gb": round(self.ram_gb, 1),
             "gpu": self.gpu_name or None, "gpu_count": self.gpu_count,
             "gpu_mem_gb": round(self.gpu_mem_gb, 1), "cuda": self.cuda}
        if self.host_cores and self.host_cores != self.cores:
            d["host_cores"] = self.host_cores
            d["cgroup_cpus"] = self.cgroup_cpus
            d["note"] = (f"host reports {self.host_cores} CPUs but this container may use "
                         f"{self.cores}")
        return d


def _ram_gb() -> float:
    try:
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 1e9
    except Exception:
        pass
    try:
        import psutil
        return psutil.virtual_memory().total / 1e9
    except Exception:
        return 0.0


def _nvidia(query: str) -> list[str]:
    if not shutil.which("nvidia-smi"):
        return []
    try:
        out = subprocess.run(
            ["nvidia-smi", f"--query-gpu={query}", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=15)
        if out.returncode != 0:
            return []
        return [l.strip() for l in out.stdout.strip().splitlines() if l.strip()]
    except Exception:
        return []


def _cgroup_cpus() -> float | None:
    """CPUs this CONTAINER may use, which is not what `nproc` reports.

    A pod sees the host's processor count — a RunPod GPU pod reported 128 — while its
    cgroup quota may be a small fraction of that. Planning 127 workers against a ~16-CPU
    slice oversubscribes ~8x, and the resulting thrash looked like filesystem failures
    (80% of 7,394 reads failed) rather than like the scheduling problem it was.

    cgroup v2 exposes "max period" in cpu.max; v1 uses two files in µs.
    """
    try:
        v2 = "/sys/fs/cgroup/cpu.max"
        if os.path.exists(v2):
            quota, period = open(v2).read().split()
            if quota != "max":
                return max(1.0, float(quota) / float(period))
        q = "/sys/fs/cgroup/cpu/cpu.cfs_quota_us"
        p = "/sys/fs/cgroup/cpu/cpu.cfs_period_us"
        if os.path.exists(q) and os.path.exists(p):
            quota = float(open(q).read().strip())
            period = float(open(p).read().strip())
            if quota > 0:
                return max(1.0, quota / period)
    except Exception:
        pass
    return None


def probe() -> Hardware:
    host = os.cpu_count() or 1
    allowed = _cgroup_cpus()
    # Prefer the container's own limit. `sched_getaffinity` catches pinning that cgroups
    # do not express.
    try:
        affinity = len(os.sched_getaffinity(0))
    except Exception:
        affinity = host
    cores = int(min(host, affinity, allowed or host))
    hw = Hardware(cores=max(1, cores), ram_gb=_ram_gb())
    hw.host_cores = host
    hw.cgroup_cpus = allowed
    names = _nvidia("name")
    mems = _nvidia("memory.total")
    if names:
        hw.gpu_name = names[0]
        hw.gpu_count = len(names)
    if mems:
        try:
            hw.gpu_mem_gb = float(mems[0]) / 1024.0
        except ValueError:
            pass
    try:
        import torch
        hw.cuda = bool(torch.cuda.is_available())
    except Exception:
        hw.cuda = False
    return hw


# --------------------------------------------------------------------------------------
# Planning
# --------------------------------------------------------------------------------------

# Reading a corpus off a network volume is bounded by the filesystem, not by cores. This
# is the ceiling for any stage whose per-item work starts with a read from /workspace.
NETWORK_FS_WORKER_CAP = 24


def plan_workers(*, per_worker_gb: float = 1.2, reserve_cores: int = 1,
                 reserve_gb: float = 2.0, cap: int | None = None,
                 hw: Hardware | None = None) -> tuple[int, str]:
    """Worker count for a CPU stage, bounded by memory AND by the filesystem.

    The cap defaults to NETWORK_FS_WORKER_CAP rather than being optional, because the
    binding constraint is usually neither cores nor RAM. A 128-core pod planned 127
    workers and failed 80% of 7,394 reads against the MooseFS-backed volume; the same
    code capped at 32 failed none. Leaving the cap to each caller is how that happened —
    normalize passed one, measure did not.

    Pass `cap=0` to deliberately opt out for work that never touches the volume.

    Returns (workers, why) so the choice is reportable instead of mysterious.
    """
    hw = hw or probe()
    by_core = max(1, hw.cores - reserve_cores)
    by_ram = max(1, int((hw.ram_gb - reserve_gb) / max(per_worker_gb, 0.1))) \
        if hw.ram_gb else by_core
    n = min(by_core, by_ram)

    effective_cap = NETWORK_FS_WORKER_CAP if cap is None else (cap or None)
    if effective_cap:
        n = min(n, effective_cap)

    env = os.environ.get("PODH_WORKERS")
    if env and env.isdigit():
        return int(env), f"PODH_WORKERS={env} (override)"

    if effective_cap and n == effective_cap and effective_cap < min(by_core, by_ram):
        limit = f"I/O cap ({effective_cap}) — {by_core} cores would fit but the volume"
        limit += " is network-backed"
    else:
        limit = "cores" if by_core <= by_ram else f"RAM ({hw.ram_gb:.0f} GB)"
    return n, (f"{n} workers — {hw.cores} cores, {hw.ram_gb:.0f} GB RAM, "
               f"{per_worker_gb} GB/worker; bound by {limit}")


def plan_batch(*, per_item_mb: float = 60.0, floor: int = 8, ceiling: int = 256,
               hw: Hardware | None = None) -> tuple[int, str]:
    """Batch size for a GPU stage, bounded by VRAM.

    On CPU a large batch buys little and costs memory, so the floor applies there.
    """
    hw = hw or probe()
    env = os.environ.get("LINGUA_EMBED_BATCH")
    if env and env.isdigit():
        return int(env), f"LINGUA_EMBED_BATCH={env} (override)"
    if not hw.cuda or hw.gpu_mem_gb <= 0:
        return floor * 2, f"{floor*2} — CPU inference, batching helps little"
    usable_mb = hw.gpu_mem_gb * 1024 * 0.7          # leave room for the model and cuDNN
    n = int(max(floor, min(ceiling, usable_mb / max(per_item_mb, 1.0))))
    return n, (f"{n} — {hw.gpu_name} {hw.gpu_mem_gb:.0f} GB, "
               f"70% usable at ~{per_item_mb:.0f} MB/item")


# --------------------------------------------------------------------------------------
# Utilisation sampling
# --------------------------------------------------------------------------------------

@dataclass
class Utilisation:
    cpu: list[float] = field(default_factory=list)
    gpu: list[float] = field(default_factory=list)
    gpu_mem: list[float] = field(default_factory=list)

    def _stat(self, xs: list[float]) -> dict | None:
        if not xs:
            return None
        return {"mean": round(sum(xs) / len(xs), 1), "peak": round(max(xs), 1),
                "samples": len(xs)}

    def as_dict(self) -> dict:
        d = {"cpu_percent": self._stat(self.cpu),
             "gpu_percent": self._stat(self.gpu),
             "gpu_mem_percent": self._stat(self.gpu_mem)}
        return {k: v for k, v in d.items() if v}

    def verdict(self, cores: int) -> str:
        c = self._stat(self.cpu)
        g = self._stat(self.gpu)
        if g and g["mean"] < 25 and c and c["mean"] > 70:
            return ("GPU idle while CPU saturated — this stage is CPU-bound; renting a "
                    "GPU bought nothing")
        if c and c["mean"] < 100 / max(cores, 1) * 1.5:
            return (f"CPU mean {c['mean']}% of {cores} cores — effectively serial, "
                    f"raise workers")
        if c and c["mean"] > 85:
            return f"CPU mean {c['mean']}% — well utilised"
        if c and g and c["mean"] < 50 and g["mean"] < 50:
            return ("both CPU and GPU under 50% — bound by I/O or by a serial section, "
                    "not by compute")
        return "utilisation recorded"


class Sampler:
    """Background utilisation sampling. Use as a context manager around a stage."""

    def __init__(self, every: float = 5.0):
        self.every = every
        self.util = Utilisation()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._psutil = None
        try:
            import psutil
            self._psutil = psutil
        except Exception:
            pass

    def _loop(self) -> None:
        while not self._stop.is_set():
            if self._psutil:
                try:
                    self.util.cpu.append(self._psutil.cpu_percent(interval=None))
                except Exception:
                    pass
            else:
                try:
                    load1 = os.getloadavg()[0]
                    self.util.cpu.append(100.0 * load1 / max(os.cpu_count() or 1, 1))
                except Exception:
                    pass
            for q, sink in (("utilization.gpu", self.util.gpu),
                            ("utilization.memory", self.util.gpu_mem)):
                vals = _nvidia(q)
                if vals:
                    try:
                        sink.append(float(vals[0]))
                    except ValueError:
                        pass
            self._stop.wait(self.every)

    def __enter__(self) -> "Sampler":
        if self._psutil:
            try:
                self._psutil.cpu_percent(interval=None)   # prime the counter
            except Exception:
                pass
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)

    def report(self, cores: int | None = None) -> dict:
        d = self.util.as_dict()
        d["verdict"] = self.util.verdict(cores or os.cpu_count() or 1)
        return d

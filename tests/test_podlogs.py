"""Pod logs (pod-control APP-API §9): tails on request, full logs at the end.

Against the fake control plane of tests/_fake_control.py: reading a tail (colour codes,
progress-bar carriage returns, the 256 KB cap), the request on an /v1/alive reply answered
once, the end-of-pod upload through presigned PUTs (and skipped on a 404), podh-boot's
boot.log, the upload happening BEFORE /v1/done, and podh-worker's worker.log.
"""
import gzip
import os
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))
sys.path.insert(0, str(HERE))
for k in list(os.environ):
    if k.startswith("PODH_"):
        os.environ.pop(k)

from _fake_control import FakeControl  # noqa: E402
from pod_harness import heartbeat, podlogs  # noqa: E402

ok = []


def check(n, c):
    ok.append(bool(c)); print(f"  {'✅' if c else '❌'} {n}")


def wait_for(cond, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.05)
    return cond()


T = Path(tempfile.mkdtemp())
heartbeat.STOP_FILE = str(T / "stop")
LOGS = T / "logs"
LOGS.mkdir()

# -- reading a tail ---------------------------------------------------------------------------
print("— tails")
(LOGS / "workload.log").write_text(
    "".join(f"line {i}\n" for i in range(500))
    + "\x1b[32mgreen\x1b[0m \x1b]0;title\x07done\n"
    + "progress 10%\rprogress 50%\rprogress 100%\n")
got, cut = podlogs.read_tail(LOGS / "workload.log", 4)
check(f"the last 4 lines, colours stripped, a progress bar's last frame ({got})",
      got == ["line 498", "line 499", "green done", "progress 100%"] and not cut)
big = LOGS / "big.log"
big.write_text("".join(f"{i:07d} " + "x" * 1000 + "\n" for i in range(400)))
got, cut = podlogs.read_tail(big, 2000)
size = sum(len(x.encode()) + 1 for x in got)
check(f"a log over 256 KB keeps its newest lines under the cap, truncated ({len(got)} lines, "
      f"{size} B)", cut and size <= podlogs.TAIL_MAX_BYTES and got[-1].startswith("0000399")
      and all(len(x) == 1008 for x in got))
got, cut = podlogs.read_tail(big, 10)
check("...but 10 lines of it fit: not truncated", len(got) == 10 and not cut)
(LOGS / "not a name!.log").write_text("x\n")
(LOGS / "notes.txt").write_text("x\n")
have = podlogs.available(LOGS)
check(f"logs are <name>.log with a valid name ({sorted(have)})",
      sorted(have) == ["big", "workload"])
c = podlogs.collect(["workload", "missing"], 2, LOGS)
check("named: only those that exist", [x["name"] for x in c] == ["workload"])
check("empty names: every log", sorted(x["name"] for x in podlogs.collect([], 1, LOGS))
      == ["big", "workload"])

# -- a request on the /v1/alive reply ---------------------------------------------------------
print("— a request rides on /v1/alive")
fc = FakeControl()
os.environ.update(PODH_CONTROL_URL=fc.url, PODH_CONTROL_TOKEN="pt", RUNPOD_POD_ID="pod1",
                  PODH_CONTROL_JOB_ID="job1")
fc.routes[("POST", "/v1/pod/logs")] = lambda req: (200, {
    "job_id": "job1", "stored": [x["name"] for x in req["body"]["logs"]], "cleared": True})
said = []
r = podlogs.Responder(LOGS, say=said.append)
heartbeat._REPLY_HOOKS.append(r)
fc.alive_reply = {"ok": True, "logs": {"names": ["workload"], "lines": 3, "req_id": "r1"}}
heartbeat.alive()
check("the pod pushes POST /v1/pod/logs for the request",
      wait_for(lambda: len(fc.bodies("/v1/pod/logs")) == 1))
b = fc.bodies("/v1/pod/logs")[0]
check(f"…with req_id, its identity and the asked lines ({b.get('req_id')}, {b['logs']})",
      b.get("req_id") == "r1" and b.get("pod_id") == "pod1" and b.get("job_id") == "job1"
      and b["logs"] == [{"name": "workload", "truncated": False,
                         "lines": ["line 499", "green done", "progress 100%"]}])
heartbeat.alive()
heartbeat.alive()
time.sleep(0.3)
check("the same request on later replies is answered once",
      len(fc.bodies("/v1/pod/logs")) == 1)
fc.alive_reply = {"ok": True}
heartbeat.alive()
time.sleep(0.2)
check("no `logs` key (an older server): nothing is pushed", len(fc.bodies("/v1/pod/logs")) == 1)
fc.alive_reply = {"ok": True, "logs": {"names": [], "lines": 1, "req_id": "r2"}}
heartbeat.alive()
check("empty names: every log in one push",
      wait_for(lambda: len(fc.bodies("/v1/pod/logs")) == 2)
      and sorted(x["name"] for x in fc.bodies("/v1/pod/logs")[1]["logs"]) == ["big", "workload"])
del fc.routes[("POST", "/v1/pod/logs")]
fc.routes[("POST", "/v1/pod/logs")] = (404, {"detail": "Not Found"})
fc.alive_reply = {"ok": True, "logs": {"names": [], "lines": 1, "req_id": "r3"}}
heartbeat.alive()
check("a server without /v1/pod/logs (404): said once, then requests are ignored",
      wait_for(lambda: r.unsupported) and any("no /v1/pod/logs" in s for s in said))
heartbeat._REPLY_HOOKS.remove(r)

# -- the end-of-pod upload --------------------------------------------------------------------
print("— full logs at the end")
SIGNED = {}


def upload(req):
    names = [x["name"] for x in req["body"]["names"]]
    SIGNED.update({x["name"]: x["size"] for x in req["body"]["names"]})
    return 200, {"job_id": "job1", "expires_s": 3600,
                 "urls": {n: f"{fc.url}/put/logs/{n}.log.gz" for n in names},
                 "refs": {n: f"store://logs/logs/batch/job1/{n}.log.gz" for n in names}}


fc.routes[("POST", "/v1/pod/logs/upload")] = upload
res = podlogs.upload_all(LOGS, say=said.append)
check(f"every log uploaded ({res})", sorted(res["uploaded"]) == ["big", "workload"]
      and not res["failed"])
wl = fc.blobs.get("logs/workload.log.gz", b"")
check("the PUT body is the whole log, gzipped, and its size went to /upload",
      gzip.decompress(wl) == (LOGS / "workload.log").read_bytes()
      and SIGNED.get("workload") == len(wl))
put = [q for q in fc.requests if q["method"] == "PUT"]
check("the bucket never sees the pod token", all(not q["headers"].get("x-podh-token") for q in put))
fc.put_fail["logs/big.log.gz"] = 1
res = podlogs.upload_all(LOGS, say=said.append)
check("one failed PUT does not stop the others", res["failed"] == ["big"]
      and res["uploaded"] == ["workload"])
fc.routes[("POST", "/v1/pod/logs/upload")] = (404, {"error": "no log store configured"})
res = podlogs.upload_all(LOGS, say=said.append)
check(f"no log store (404): skipped, nothing PUT ({res})",
      res.get("skipped") == "no log store" and not res["uploaded"])
fc.routes[("POST", "/v1/pod/logs/upload")] = upload


def slow_upload(req):
    time.sleep(1.5)
    return upload(req)


fc.routes[("POST", "/v1/pod/logs/upload")] = slow_upload
t0 = time.time()
res = podlogs.upload_all(LOGS, budget_s=1.0, say=said.append)
check(f"the budget bounds the whole upload ({time.time() - t0:.1f}s, {res})",
      time.time() - t0 < 3 and not res["uploaded"])
fc.routes[("POST", "/v1/pod/logs/upload")] = upload

# -- podh-boot: boot.log, and the upload before /v1/done --------------------------------------
print("— podh-boot")
from pod_harness.boot import core  # noqa: E402

ctx = core.Context(boot_dir=T / "bd")
core.say("hello from a stage")
check("podh-boot's stage lines go to boot.log as well",
      "hello from a stage" in (T / "bd" / "logs" / "boot.log").read_text())
(T / "bd" / "logs" / "workload.log").write_text("Traceback: it broke\n")
n0 = len(fc.calls)
ctx.fail(core.BootFailure("models: a file is missing", stage="models"))
paths = [p for p, _, _ in fc.calls[n0:]]
check(f"a failed stage uploads its logs BEFORE reporting done ({paths})",
      "/v1/pod/logs/upload" in paths and "/v1/done" in paths
      and paths.index("/v1/pod/logs/upload") < paths.index("/v1/done"))
check("…boot.log and workload.log among them",
      b"Traceback: it broke" in gzip.decompress(fc.blobs.get("logs/workload.log.gz", b""))
      and b"models: a file is missing" in gzip.decompress(fc.blobs.get("logs/boot.log.gz", b"")))
n1 = len(fc.calls)
ctx.finish(0, "again")
check("a second ending uploads nothing more",
      "/v1/pod/logs/upload" not in [p for p, _, _ in fc.calls[n1:]])

# -- podh-worker: worker.log ------------------------------------------------------------------
print("— podh-worker")
os.environ["PODH_BOOT_DIR"] = str(T / "wbd")
from pod_harness.worker import loop  # noqa: E402

loop._WORKER_LOG = None
loop._log("[podh-worker] a line")
check("podh-worker's lines go to worker.log in podh-boot's logs dir",
      "[podh-worker] a line" in (T / "wbd" / "logs" / "worker.log").read_text())

fc.close()
print(f"\n{sum(ok)}/{len(ok)} passed")
sys.exit(0 if all(ok) else 1)

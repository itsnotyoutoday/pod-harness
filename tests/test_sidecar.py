"""Sidecar mode: /v1 beside a workload it did not build.

Auth, bounded and followable log reads, no escaping an artifact root, progress events, and
the heartbeat staying OFF unless enabled with a liveness URL. No network, no GPU.
"""
import os, sys, tempfile
from pathlib import Path

sys.path.insert(0, "src")
ok = []


def check(n, c):
    ok.append(bool(c)); print(f"  {'✅' if c else '❌'} {n}")


tmp = Path(tempfile.mkdtemp())
(tmp / "logs").mkdir()
(tmp / "logs" / "comfyui.log").write_text("line1\nline2\n")
(tmp / "art").mkdir()
(tmp / "art" / "out.txt").write_text("result")
(tmp / "secret.txt").write_text("nope")
os.symlink(tmp / "secret.txt", tmp / "art" / "link.txt")

os.environ.update({"PODH_API_TOKEN": "T", "PODH_SIDECAR_LOGS": f"comfy={tmp/'logs'/'comfyui.log'},{tmp/'logs'}/*.log",
                   "PODH_SIDECAR_ARTIFACTS": str(tmp / "art"), "RUNPOD_POD_ID": "pod1"})
for k in ("PODH_SIDECAR_HEARTBEAT", "PODH_SIDECAR_LIVENESS", "PODH_CONTROL_URL"):
    os.environ.pop(k, None)

from fastapi.testclient import TestClient
from pod_harness import sidecar

c = TestClient(sidecar.app)
A = {"X-Podh-Token": "T"}

check("health needs no token", c.get("/v1/health").json()["mode"] == "sidecar")
check("everything else needs the token", c.get("/v1/logs").status_code == 401)
names = [x["name"] for x in c.get("/v1/logs", headers=A).json()["logs"]]
check("named and globbed logs are listed", "comfy" in names and "comfyui.log" in names)

r = c.get("/v1/logs/comfy", headers=A, params={"tail": 6}).json()
check("tail returns the end and admits the total", r["text"] == "line2\n" and r["total_bytes"] == 12)
off = r["next_offset"]
with open(tmp / "logs" / "comfyui.log", "a") as f:
    f.write("line3\n")
r = c.get("/v1/logs/comfy", headers=A, params={"offset": off, "wait": 2}).json()
check("offset follows only what is new", r["text"] == "line3\n" and r["next_offset"] == 18)
check("an unknown log is a 404", c.get("/v1/logs/nope", headers=A).status_code == 404)

check("an artifact under a root is served", c.get("/v1/artifacts/file", headers=A, params={"path": "out.txt"}).text == "result")
check("'..' cannot leave the root", c.get("/v1/artifacts/file", headers=A, params={"path": "../secret.txt"}).status_code == 404)
check("a symlink cannot leave the root", c.get("/v1/artifacts/file", headers=A, params={"path": "link.txt"}).status_code == 404)
check("an absolute path outside is refused", c.get("/v1/artifacts/file", headers=A, params={"path": str(tmp / "secret.txt")}).status_code == 404)

s1 = c.post("/v1/progress", headers=A, json={"step": "model wan (14.3G in 190s)"}).json()["seq"]
c.post("/v1/progress", headers=A, json={"step": "ComfyUI up"})
ev = c.get("/v1/events", headers=A, params={"since_seq": s1}).json()
check("progress lines come back as events, after a cursor", [e["step"] for e in ev["events"]] == ["ComfyUI up"])
check("a progress line needs a step", c.post("/v1/progress", headers=A, json={}).status_code == 422)

check("system reports disks", "disks" in c.get("/v1/system", headers=A).json())
check("heartbeat is off by default", sidecar.start_heartbeat() is False)
os.environ["PODH_SIDECAR_HEARTBEAT"] = "1"
check("heartbeat stays off without a liveness URL", sidecar.start_heartbeat() is False)
check("done without pod-control config says so", c.post("/v1/done", headers=A, json={}).status_code == 503)
Path("/tmp/podh-stop-heartbeat").unlink(missing_ok=True)

r = c.post("/v1/boot", headers=A, json={"line": "downloading models", "stage": "models"}).json()
check("boot without pod-control config is kept locally and says it was not relayed",
      r["relayed"] is False and "PODH_CONTROL_URL" in r["error"])
ev = c.get("/v1/events", headers=A, params={"since_seq": r["seq"] - 1}).json()["events"]
check("...and shows up as an event, tagged with its stage", ev[0]["step"] == "[models] downloading models")
check("a boot line needs a line", c.post("/v1/boot", headers=A, json={"stage": "x"}).status_code == 422)

# Relays, against a fake pod-control on 127.0.0.1.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _fake_control import FakeControl  # noqa: E402
fc = FakeControl()
os.environ.update(PODH_CONTROL_URL=fc.url, PODH_CONTROL_TOKEN="ct")
r = c.post("/v1/boot", headers=A, json={"line": "ComfyUI listening", "stage": "ready"}).json()
b = (fc.bodies("/v1/boot") or [{}])[-1]
check("boot is relayed to pod-control with line and stage",
      r["relayed"] is True and b.get("line") == "ComfyUI listening" and b.get("stage") == "ready"
      and b.get("pod_id") == "pod1")
r = c.post("/v1/done", headers=A, json={"rc": 7, "reason": "CUDA unknown error", "stage": "gpu_check"}).json()
b = (fc.bodies("/v1/done") or [{}])[-1]
check("done relays rc, reason and stage",
      r["requested"] is True and (b.get("rc"), b.get("reason"), b.get("stage")) == (7, "CUDA unknown error", "gpu_check"))
check("done with a non-numeric rc is a 422, not a 500",
      c.post("/v1/done", headers=A, json={"rc": "seven"}).status_code == 422)
fc.close()
os.environ.pop("PODH_CONTROL_URL")
Path("/tmp/podh-stop-heartbeat").unlink(missing_ok=True)

os.environ["PODH_API_TOKEN"] = ""
check("no token configured refuses rather than serving open", c.get("/v1/logs", headers=A).status_code == 503)

print(f"\n  {sum(ok)}/{len(ok)} passed")
sys.exit(0 if all(ok) else 1)

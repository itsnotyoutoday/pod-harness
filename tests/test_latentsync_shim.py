"""00_podh_latentsync against stub LatentSync modules (torch and cv2 stubbed): patches attach, small faces, faceless frames, selection hook.

    python3 tests/test_latentsync_shim.py
"""
import sys, types, importlib.util
try:
    import numpy as np
except ImportError:          # the shim runs in the image's venv; CI's bare python has no numpy
    print("skipped: numpy not installed"); sys.exit(0)
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
D = str(ROOT / "docker" / "comfyui")
STUBS = str(ROOT / "tests" / "stubs")
torch = types.ModuleType("torch"); torch.stack = lambda xs: list(xs); sys.modules["torch"] = torch
cv2 = types.ModuleType("cv2"); cv2.INTER_CUBIC = 0
cv2.resize = lambda f, size, interpolation=0: np.zeros((size[1], size[0]), dtype=f.dtype) + f.mean(); sys.modules["cv2"] = cv2
spec = importlib.util.spec_from_file_location("custom_nodes.00_podh_latentsync", f"{D}/00_podh_latentsync.py")
m = importlib.util.module_from_spec(spec); sys.modules[spec.name] = m; spec.loader.exec_module(m)
assert sys.modules["podh_latentsync"] is m
sys.path.insert(0, STUBS)
fd = __import__("latentsync.utils.face_detector", fromlist=["x"])
lp = __import__("latentsync.pipelines.lipsync_pipeline", fromlist=["x"])
assert fd.FaceDetector.__call__.podh_patched and lp.LipsyncPipeline.affine_transform_video.podh_patched
class F:  # a face with an identity value
    def __init__(s, who, bbox): s.who, s.bbox = who, bbox
class App:
    def __init__(s, per_frame): s.per_frame = per_frame
    def get(s, img, *a, **k): return s.per_frame(img)
# small frame → searched at 2x, box scaled back
det = fd.FaceDetector(App(lambda img: [F("a", (20, 40, 60, 100))] if img.shape[0] == 200 else []))
box, lmk = det(np.zeros((100, 50)))
assert box == (10, 20, 30, 50), box
# pipeline: frames 0..4 at 25fps; face "b" is the speaker; frame 2 has only "a"
frames = [np.full((100, 50), i, dtype=float) for i in range(5)]
def per_frame(img):
    i = int(img.mean())
    return [F("a", (0, 0, 10, 10))] + ([] if i == 2 else [F("b", (2, 2, 12, 12))])
class Proc:
    def __init__(s): s.face_detector = fd.FaceDetector(App(per_frame))
    def affine_transform(s, frame):
        box, _ = s.face_detector(frame)
        if box is None: raise RuntimeError("Face not detected!")
        return f"face{int(frame.mean())}", box, "M"
pipe = lp.LipsyncPipeline(Proc())
faces, boxes, mats = pipe.affine_transform_video(frames)  # no selection: first face, every frame
assert mats == ["M"] * 5
prepared = []
class Sel:
    def prepare(s, app): prepared.append(app)
    def __call__(s, img, faces): return next((f for f in faces if f.who == "b"), None)
m.set_next_selector(Sel(), windows=[(0.0, 0.13)])  # frames 0..3 (0, .04, .08, .12)
faces, boxes, mats = pipe.affine_transform_video(frames)
assert prepared, "prepare not called"
assert mats == ["M", "M", None, "M", None], mats  # frame 2 faceless (passes through), frame 4 outside windows
assert boxes[0] == (1, 1, 6, 6)  # the selected face, not the first
assert fd.FaceDetector.podh_select is None
faces, boxes, mats = pipe.affine_transform_video(frames)  # one-shot: gone
assert boxes[0] == (0, 0, 5, 5)
m.set_next_selector(lambda img, faces: None, windows=[(0, 1)])  # never the speaker → fails
try:
    pipe.affine_transform_video(frames); raise SystemExit("should fail")
except RuntimeError as e: assert "selected face not detected in 5 of 5" in str(e), e
m.register(Sel(), persistent=True)
for _ in range(2): assert pipe.affine_transform_video(frames)[1][0] == (1, 1, 6, 6)
m.clear()
try: pipe("x"); raise SystemExit("VAE guard missing")
except RuntimeError as e: assert "VAE" in str(e)
print("all ok")

"""LatentSync 1.6 fixes, loaded by ComfyUI as custom_nodes/00_podh_latentsync.py (before the other nodes: name order).

Patched when the LatentSync wrapper first imports its modules (it does so lazily, at inference):
  1. Small faces. LatentSync's detector drops faces under 50 × 80 px, so a full-body 9:16 shot (a ~50 px face in a
     480 × 832 clip) came back "Face not detected". Frames under 1280 px are searched at twice the size; boxes and
     landmarks are scaled back.
  2. Frames without a face. One faceless frame (a turned head) used to fail the whole clip. Now such frames pass through
     unsynced, as long as no more than MAX_FACELESS of the frames are faceless (env PODH_LATENTSYNC_MAX_FACELESS).
  3. No VAE, no pass. Without sd-vae-ft-mse in its checkpoints/, the wrapper builds a VAE with random weights and paints
     every mouth as coloured noise. The pass now fails instead.
  4. A face-selection hook, for clips with several faces:

        import sys
        ls = sys.modules["podh_latentsync"]           # this module (it registers itself under that name)
        ls.set_next_selector(select, windows=[(1.2, 3.4)])

     `select(frame, faces) -> face | None` is called for each detected frame with insightface's faces and picks the
     one to sync (None: no face in this frame → passed through). If `select` has a `prepare(app)` attribute, it's
     called once at the start of the pass with LatentSync's insightface FaceAnalysis (e.g. to embed a reference
     portrait); an exception there fails the pass. `windows`: (start_s, end_s) ranges of the clip to sync; frames
     outside them pass through unchanged. The selection is taken by the next LatentSync pass only, and is dropped after
     that pass whatever happens, or after max_age_s if no pass ran. `register(select, windows=None, persistent=True)`
     sets one for every pass until `clear()`.

Works without LatentSync installed (nothing is patched). Build check:
    python 00_podh_latentsync.py probe <LatentSync wrapper dir>
"""
from __future__ import annotations

import importlib.abc
import os
import sys
import time

NODE_CLASS_MAPPINGS: dict = {}
NODE_DISPLAY_NAME_MAPPINGS: dict = {}

try:
    import numpy as np
    import torch
except Exception:  # noqa: BLE001 — not ComfyUI's venv: nothing to patch
    np = torch = None

MAX_FACELESS = float(os.environ.get("PODH_LATENTSYNC_MAX_FACELESS", 0.5))  # more faceless frames than this: the pass fails
SMALL_FRAME_PX = 1280  # frames whose longer side is under this are searched at 2×
LATENTSYNC_FPS = 25  # LatentSync lines frames up with the audio at 25 fps

_state: dict = {}  # "next": the one-shot selection; "always": a persistent one


def register(select, windows=None, *, persistent: bool = True, max_age_s: float = 600) -> None:
    """Set the face selection for LatentSync passes: every pass until clear() (persistent), or only the next one."""
    if select is not None and not callable(select):
        raise TypeError("select must be callable: select(frame, faces) -> face | None")
    entry = {"select": select, "windows": [(float(a), float(b)) for a, b in windows] if windows is not None else None,
             "at": time.time(), "max_age_s": float(max_age_s)}
    _state["always" if persistent else "next"] = entry
    _guard_latentsync_nodes()


def set_next_selector(select, windows=None, max_age_s: float = 600) -> None:
    """The face selection for the next LatentSync pass only (see the module doc)."""
    register(select, windows, persistent=False, max_age_s=max_age_s)


def clear() -> None:
    _state.pop("next", None)
    _state.pop("always", None)


def _take() -> dict | None:
    entry = _state.pop("next", None)
    if entry is not None and time.time() - entry["at"] > entry["max_age_s"]:
        print("[podh] LatentSync: ignoring a stale face selection (the pass it was meant for never ran)")
        entry = None
    return entry or _state.get("always")


def _patch_detector(mod) -> None:
    plain = mod.FaceDetector.__call__

    def selecting(self, frame, threshold=0.5):
        select = getattr(mod.FaceDetector, "podh_select", None)
        if select is None:
            return plain(self, frame, threshold)
        real_get = self.app.get

        def chosen(img, *a, **k):
            face = select(img, real_get(img, *a, **k))
            return [face] if face is not None else []

        self.app.get = chosen
        try:
            return plain(self, frame, threshold)
        finally:
            self.app.get = real_get

    def call(self, frame, threshold=0.5):
        if not isinstance(frame, np.ndarray) or max(frame.shape[:2]) >= SMALL_FRAME_PX:
            return selecting(self, frame, threshold)
        import cv2

        h, w = frame.shape[:2]
        box, lmk = selecting(self, cv2.resize(frame, (w * 2, h * 2), interpolation=cv2.INTER_CUBIC), threshold)
        if box is None:
            return None, None
        return tuple(int(v / 2) for v in box), np.round(np.asarray(lmk) / 2).astype(np.int_)

    call.podh_patched = True
    mod.FaceDetector.__call__ = call


def _patch_pipeline(mod) -> None:
    cls = mod.LipsyncPipeline

    def affine_transform_video(self, video_frames):
        entry = _take()
        if entry is None or entry["select"] is None:
            return _transform(self, video_frames, entry["windows"] if entry else None)
        det = self.image_processor.face_detector
        select = entry["select"]
        if callable(getattr(select, "prepare", None)):
            select.prepare(det.app)
        type(det).podh_select = select
        try:
            return _transform(self, video_frames, entry["windows"], selected=True)
        finally:
            type(det).podh_select = None

    def _transform(self, video_frames, windows, selected: bool = False):
        faces, boxes, mats, miss, outside = [], [], [], [], []
        for i, frame in enumerate(video_frames):
            if windows is not None and not any(a <= i / LATENTSYNC_FPS <= b for a, b in windows):
                faces.append(None)
                boxes.append(None)
                mats.append(None)
                outside.append(i)  # outside the windows: the frame is left as it is
                continue
            try:
                face, box, mat = self.image_processor.affine_transform(frame)
            except RuntimeError as exc:
                if "Face not detected" not in str(exc):
                    raise
                face = box = mat = None
                miss.append(i)
            faces.append(face)
            boxes.append(box)
            mats.append(mat)
        n = len(video_frames) - len(outside)  # the frames this pass is meant to sync
        who = "the selected face" if selected else "Face"
        if n == 0 or len(miss) > n * MAX_FACELESS:
            raise RuntimeError(f"{who} not detected in {len(miss)} of {n} frames (too small, turned, hidden"
                               f"{' or not in frame' if selected else ''})")
        good = [i for i in range(len(video_frames)) if faces[i] is not None]
        for i in miss + outside:  # a stand-in face keeps the model's input continuous; the frame itself is left as it was
            j = min(good, key=lambda g: abs(g - i))
            faces[i], boxes[i] = faces[j], boxes[j]
        if miss:
            print(f"[podh] LatentSync: no face in {len(miss)} of {n} frames — those pass through unsynced")
        return torch.stack(faces), boxes, mats

    def restore_video(self, faces, video_frames, boxes, affine_matrices):
        video_frames = video_frames[: len(faces)]
        out = []
        for index, face in enumerate(faces):
            if affine_matrices[index] is None:  # no face (or outside the windows): unchanged
                out.append(video_frames[index])
                continue
            x1, y1, x2, y2 = boxes[index]
            face = mod.torchvision.transforms.functional.resize(
                face, size=(int(y2 - y1), int(x2 - x1)), interpolation=mod.transforms.InterpolationMode.BICUBIC,
                antialias=True)
            out.append(self.image_processor.restorer.restore_img(video_frames[index], face, affine_matrices[index]))
        return np.stack(out, axis=0)

    affine_transform_video.podh_patched = True
    cls.affine_transform_video, cls.restore_video = affine_transform_video, restore_video

    from pathlib import Path

    root = Path(mod.__file__).resolve().parents[2]  # <wrapper>/latentsync/pipelines/lipsync_pipeline.py
    orig_call = vars(cls).get("__call__")
    if orig_call is None:
        return

    def __call__(self, *args, **kwargs):
        if not vae_present(root):
            raise RuntimeError("LatentSync's VAE (sd-vae-ft-mse) isn't on this pod — a random one would paint the mouth "
                               "as noise; not syncing")
        return orig_call(self, *args, **kwargs)

    cls.__call__ = __call__


def vae_present(root) -> bool:
    """The VAE where the wrapper's inference script looks for it (scripts/inference.py vae_locations)."""
    ck = root / "checkpoints"
    folder = any((ck / d / "config.json").is_file() and any((ck / d / w).is_file() for w in (
        "diffusion_pytorch_model.safetensors", "diffusion_pytorch_model.bin")) for d in ("vae", "sd-vae-ft-mse"))
    return folder or any((ck / f).is_file() for f in ("vae/sd-vae-ft-mse.safetensors", "sd-vae-ft-mse.safetensors"))


def _guard_latentsync_nodes() -> None:
    """A one-shot selection belongs to the LatentSync pass that follows in this prompt: once that pass has run (or
    failed before it reached the face step), it's gone, never left for the next prompt. Wraps each LatentSync node's
    function once, from ComfyUI's loaded node list (those nodes load after this file, so it's looked up when needed)."""
    try:
        import nodes as comfy_nodes
    except Exception:  # noqa: BLE001 — not in ComfyUI: the age limit still applies
        return
    for key, node_cls in list(getattr(comfy_nodes, "NODE_CLASS_MAPPINGS", {}).items()):
        fn_name = getattr(node_cls, "FUNCTION", None)
        fn = vars(node_cls).get(fn_name) if isinstance(fn_name, str) and isinstance(node_cls, type) else None
        if "latentsync" not in key.lower() or not callable(fn) or isinstance(fn, (classmethod, staticmethod)) \
                or getattr(fn, "podh_guarded", False):
            continue

        def guarded(self, *a, _fn=fn, **k):
            try:
                return _fn(self, *a, **k)
            finally:
                _state.pop("next", None)

        guarded.podh_guarded = True
        setattr(node_cls, fn_name, guarded)


PATCHES = {"latentsync.utils.face_detector": _patch_detector, "latentsync.pipelines.lipsync_pipeline": _patch_pipeline}


class _Patcher(importlib.abc.MetaPathFinder):
    """Patches a module right after it's first imported."""

    def find_spec(self, name, path, target=None):
        if name not in PATCHES:
            return None
        for finder in sys.meta_path:
            if finder is self or not hasattr(finder, "find_spec"):
                continue
            spec = finder.find_spec(name, path, target)
            if spec is not None and spec.loader is not None:
                run = spec.loader.exec_module

                def exec_module(module, run=run):
                    run(module)
                    try:
                        PATCHES[name](module)
                        print(f"[podh] patched {name}")
                    except Exception as exc:  # noqa: BLE001 — unpatched beats broken
                        print(f"[podh] couldn't patch {name}: {exc}")

                spec.loader.exec_module = exec_module
                return spec
        return None


def probe(wrapper_dir: str) -> str:
    """Import LatentSync's two modules from the wrapper's folder, as the wrapper does, and check both came out patched.
    Raises when they didn't."""
    if torch is None:
        raise RuntimeError("no torch/numpy here: the patches aren't armed")
    sys.path.insert(0, wrapper_dir)
    import importlib

    det = importlib.import_module("latentsync.utils.face_detector")
    pipe = importlib.import_module("latentsync.pipelines.lipsync_pipeline")
    bad = [n for n, fn in (("FaceDetector.__call__", det.FaceDetector.__call__),
                           ("LipsyncPipeline.affine_transform_video", pipe.LipsyncPipeline.affine_transform_video))
           if not getattr(fn, "podh_patched", False)]
    if bad:
        raise RuntimeError("not patched: " + ", ".join(bad))
    return "[podh] LatentSync patches work (small faces, faceless frames, VAE guard, face selection)"


if "podh_latentsync" not in sys.modules:  # ComfyUI loads this file under its path's name: findable by this one too
    import types

    _me = sys.modules.get(__name__)
    if _me is None:  # a loader that didn't register the module: the hook API alone
        _me = types.SimpleNamespace(register=register, set_next_selector=set_next_selector, clear=clear,
                                    vae_present=vae_present, probe=probe)
    sys.modules["podh_latentsync"] = _me
if torch is not None and not any(isinstance(f, _Patcher) for f in sys.meta_path):
    sys.meta_path.insert(0, _Patcher())
    print("[podh] LatentSync patches armed (small faces, faceless frames, VAE guard, face selection)")

if __name__ == "__main__" and len(sys.argv) == 3 and sys.argv[1] == "probe":
    print(probe(sys.argv[2]))

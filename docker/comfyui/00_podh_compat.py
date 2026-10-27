"""Loaded by ComfyUI as custom_nodes/00_podh_compat.py (the leading 00 sorts it first), before
any other custom node imports torchaudio or PyAV. Two compatibility patches for running node
packs on a current ComfyUI stack; each is a no-op where it isn't needed.

1. Newer torchaudio routes save() and load() through TorchCodec, which this image doesn't ship
   (it would need the image's exact torch build). When torchaudio says it needs TorchCodec, WAV
   audio is written with Python's own wave module and read by a small RIFF reader (8/16/24/32-bit
   PCM, 32/64-bit float). Anything else torchaudio raises is raised as before.
2. PyAV 19 dropped av.open()'s metadata_errors / metadata_encoding, which torchvision.io.read_video
   still passes; they are dropped only when this PyAV refuses them.

It defines no nodes.
"""
from __future__ import annotations

import struct
import time
import wave

NODE_CLASS_MAPPINGS: dict = {}
NODE_DISPLAY_NAME_MAPPINGS: dict = {}

try:
    import numpy as np
    import torch
    import torchaudio
except Exception:  # noqa: BLE001 — no torchaudio here: nothing to patch
    torchaudio = None


def _needs_codec(exc: Exception) -> bool:
    return "torchcodec" in str(exc).lower()


def _save_wav(uri, src, sample_rate: int, channels_first: bool = True) -> None:
    a = src.detach().to("cpu", torch.float32)
    if a.dim() == 1:
        a = a.unsqueeze(0)
    if not channels_first:
        a = a.t()
    pcm = (a.clamp(-1.0, 1.0).numpy().T * 32767.0).astype("<i2")
    with wave.open(uri if hasattr(uri, "write") else str(uri), "wb") as w:
        w.setnchannels(int(a.shape[0]))
        w.setsampwidth(2)
        w.setframerate(int(sample_rate))
        w.writeframes(pcm.tobytes())


def _riff(f):
    """A WAV file's format tag, channels, rate, bits per sample and sample bytes. Read by hand: the wave module refuses
    WAVE_FORMAT_IEEE_FLOAT (3), which 32-bit float audio uses."""
    head = f.read(12)
    if len(head) < 12 or head[:4] != b"RIFF" or head[8:12] != b"WAVE":
        raise ValueError("not a WAV file")
    fmt = None
    while True:
        h = f.read(8)
        if len(h) < 8:
            raise ValueError("WAV file without a data chunk")
        cid, size = h[:4], struct.unpack("<I", h[4:])[0]
        if cid == b"fmt ":
            body = f.read(size + (size & 1))
            tag, n, rate, _, _, bits = struct.unpack("<HHIIHH", body[:16])
            if tag == 0xFFFE and len(body) >= 26:  # WAVE_FORMAT_EXTENSIBLE: the real tag opens the subformat GUID
                tag = struct.unpack("<H", body[24:26])[0]
            fmt = (tag, n, rate, bits)
        elif cid == b"data":
            if fmt is None:
                raise ValueError("WAV data before its format")
            return (*fmt, f.read(size))
        else:
            f.read(size + (size & 1))


def _load_wav(uri, channels_first: bool = True):
    if hasattr(uri, "read"):
        tag, n, rate, bits, raw = _riff(uri)
    else:
        with open(str(uri), "rb") as f:
            tag, n, rate, bits, raw = _riff(f)
    width = bits // 8
    raw = raw[: len(raw) - len(raw) % (width * n)]  # a cut-off last frame
    if tag == 3 and width in (4, 8):  # IEEE float, already in -1..1
        data = np.frombuffer(raw, dtype="<f4" if width == 4 else "<f8").astype(np.float32)
    elif tag == 1 and width == 3:  # 24-bit PCM: three little-endian bytes, sign-extended
        b = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3).astype(np.int32)
        v = b[:, 0] | (b[:, 1] << 8) | (b[:, 2] << 16)
        data = (np.where(v >= 1 << 23, v - (1 << 24), v) / float(1 << 23)).astype(np.float32)
    elif tag == 1 and width in (1, 2, 4):
        data = np.frombuffer(raw, dtype={1: np.uint8, 2: "<i2", 4: "<i4"}[width]).astype(np.float32)
        data = (data - 128.0) / 128.0 if width == 1 else data / float(2 ** (8 * width - 1))
    else:
        raise RuntimeError(f"WAV fallback: format {tag} at {bits} bits isn't supported")
    t = torch.from_numpy(data.reshape(-1, n).T.copy())
    return (t if channels_first else t.t()), rate


if torchaudio is not None:
    _orig_save, _orig_load = torchaudio.save, torchaudio.load

    def save(uri, src, sample_rate, channels_first=True, *args, **kwargs):
        try:
            return _orig_save(uri, src, sample_rate, channels_first, *args, **kwargs)
        except (ImportError, RuntimeError, ModuleNotFoundError) as exc:
            if not _needs_codec(exc):
                raise
            return _save_wav(uri, src, sample_rate, channels_first)

    def load(uri, *args, **kwargs):
        try:
            return _orig_load(uri, *args, **kwargs)
        except (ImportError, RuntimeError, ModuleNotFoundError) as exc:
            if not _needs_codec(exc):
                raise
            return _load_wav(uri, kwargs.get("channels_first", True))

    torchaudio.save, torchaudio.load = save, load
    print("[podh] torchaudio save/load fall back to WAV when TorchCodec is missing")


# -- PyAV 19 dropped av.open()'s metadata_errors / metadata_encoding ------------------------------------------------
# torchvision.io.read_video still passes metadata_errors="ignore", and LatentSync reads its finished clip back with it:
# with the PyAV the image installs (ComfyUI asks av>=…, unbounded), every mouth pass failed at its last line with
# "open() got an unexpected keyword argument 'metadata_errors'" and the clip went on unsynced. Older PyAV still takes
# them, so they're dropped only when this PyAV refuses them.
_AV_GONE = ("metadata_errors", "metadata_encoding")

try:
    import av as _av
except Exception:  # noqa: BLE001 — no PyAV here: nothing to patch
    _av = None

if _av is not None and not getattr(_av.open, "_podh", False):
    _orig_av_open = _av.open

    def _av_open(*args, **kwargs):
        try:
            return _orig_av_open(*args, **kwargs)
        except TypeError as exc:
            gone = [k for k in _AV_GONE if k in kwargs and k in str(exc)]
            if not gone:
                raise
            return _orig_av_open(*args, **{k: v for k, v in kwargs.items() if k not in _AV_GONE})

    _av_open._podh = True


NODE_CLASS_MAPPINGS: dict = {}
NODE_DISPLAY_NAME_MAPPINGS: dict = {}

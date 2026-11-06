#!/usr/bin/env python3
"""
ReelDS builder -- does all the heavy lifting on the PC so the DSi only has to copy bytes.

  python reelds.py build  <library_dir> [<library_dir> ...] --out <SD card root>
  python reelds.py preview <file.rdv> <out.mp4>      # see exactly what the DSi will show
  python reelds.py info    <file.rdv>
  python reelds.py                                   # no arguments: opens the desktop GUI

What the PC does (so the DSi doesn't have to):
  * transcodes every video to the .rdv block codec (256 px wide, adaptive 255-colour
    palettes per scene, 4x4 skip/fill/2-colour/raw blocks) and .rda raw PCM audio
  * deinterlaces, picks a frame rate that divides the source rate, normalises loudness
  * picks a thumbnail automatically (or uses a sidecar image) and renders every piece of
    UI -- tiles, hero screens, now-playing panels, row labels, digits -- as ready-to-blit
    BGR555 bitmaps, so the DSi never rasterises a font or scales an image
  * writes catalog.bin, the only index the DSi reads

Needs: Python 3.9+, numpy, Pillow (>= 10.1 recommended) and ffmpeg. The reelds.cmd / reelds.sh
launchers in the repo root provide all of these from vendor/, so nothing has to be installed.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from fractions import Fraction
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, str(Path(__file__).resolve().parent))
import games as G  # noqa: E402  (pc/games.py)

# --------------------------------------------------------------------------------------
# constants shared with the DS side (keep in sync with source/*.h)
# --------------------------------------------------------------------------------------
ENCODER_VERSION = 1
SCREEN_W, SCREEN_H = 256, 192
TILE_W, TILE_H = 72, 40
HEADER_W, HEADER_H = 256, 20
LABEL_W, LABEL_H = 240, 13
DIGIT_CHARS = "0123456789:"
DIGIT_W, DIGIT_H = 6, 11
FRAME_KEY = 1
FRAME_PAL = 2
RDV_HEADER = struct.Struct("<4sHHHH7I6I")  # 64 bytes
CAT_VERSION = 4
CAT_HEADER = struct.Struct("<4sHHHH12I")   # 60 bytes
CAT_PAGE = struct.Struct("<3I4H")           # 20 bytes: header, tile, hero, first_row, n_rows, n_eps, pad
CAT_ROW = struct.Struct("<IHH")             # 8 bytes: label, first_entry, count
ENTRY_PAGE = 0x8000                         # entry value flag: opens a page instead of playing
CAT_ITEM = struct.Struct("<12s10I")         # 52 bytes: id, thumb, hero, panel, frames, fps n/d,
                                            #   duration, path (games), kind, save size

VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".mov", ".webm", ".m4v", ".mpg", ".mpeg", ".ts",
              ".m2ts", ".wmv", ".flv", ".ogv", ".3gp", ".divx", ".vob"}
ART_EXTS = (".jpg", ".jpeg", ".png", ".webp")

# lam: bytes-vs-error trade-off (higher = smaller files, blockier)
# skip_max: largest per-pixel error a "copy from last frame" block may leave behind
QUALITY = {
    "low":    {"lam": 1400.0, "skip_max": 3 * 36 ** 2},
    "normal": {"lam": 600.0,  "skip_max": 3 * 26 ** 2},
    "high":   {"lam": 220.0,  "skip_max": 3 * 18 ** 2},
}

BG_RGB = (20, 20, 20)
RED = (229, 9, 20)
GREY = (170, 170, 170)

BAYER4 = (np.array([[0, 8, 2, 10], [12, 4, 14, 6], [3, 11, 1, 9], [15, 7, 13, 5]],
                   dtype=np.float32) + 0.5) / 16.0 - 0.5


def log(*a):
    print(*a, flush=True)


# --------------------------------------------------------------------------------------
# colour helpers
# --------------------------------------------------------------------------------------
def to5(a):
    return (np.asarray(a, dtype=np.int32) * 31 + 127) // 255


def from5(c5):
    c5 = np.asarray(c5, dtype=np.int32)
    return (c5 << 3) | (c5 >> 2)


def image_to_bgr555(im: Image.Image, dither=True) -> bytes:
    """Serialise an image as the DS-side Img blob: u16 w, u16 h, BGR555 pixels (bit 15 set)."""
    a = np.asarray(im.convert("RGB"), dtype=np.float32)
    h, w, _ = a.shape
    if dither:
        pat = np.tile(BAYER4, (h // 4 + 1, w // 4 + 1))[:h, :w, None]
        a = a + pat * 8.0
    c = np.clip(np.round(a * 31.0 / 255.0), 0, 31).astype(np.uint16)
    px = c[..., 0] | (c[..., 1] << 5) | (c[..., 2] << 10) | 0x8000
    return struct.pack("<HH", w, h) + px.astype("<u2").tobytes()


# --------------------------------------------------------------------------------------
# ffmpeg helpers
# --------------------------------------------------------------------------------------
_FFMPEG = None
_FF_CAPS = None


def ffmpeg_exe() -> str:
    """Bundled ffmpeg (vendor/ via imageio-ffmpeg) first, then $REELDS_FFMPEG, then PATH."""
    global _FFMPEG
    if _FFMPEG:
        return _FFMPEG
    cand = os.environ.get("REELDS_FFMPEG")
    if not cand:
        try:   # the binary shipped inside the bundled imageio-ffmpeg wheel
            import imageio_ffmpeg
            bins = sorted((Path(imageio_ffmpeg.__file__).parent / "binaries").glob("ffmpeg*"))
            cand = str(bins[-1]) if bins else imageio_ffmpeg.get_ffmpeg_exe()
        except Exception:
            cand = None
    if not cand:
        cand = shutil.which("ffmpeg")
    if not cand:
        sys.exit("error: ffmpeg not found. Run the encoder through reelds.cmd / reelds.sh "
                 "(bundled ffmpeg) or install ffmpeg.")
    _FFMPEG = cand
    return cand


def need_tools():
    ffmpeg_exe()


def ff_caps():
    """Which optional filters/encoders this ffmpeg build has (builds differ per platform)."""
    global _FF_CAPS
    if _FF_CAPS is None:
        out = ""
        for what in ("-filters", "-encoders"):
            r = subprocess.run([ffmpeg_exe(), "-hide_banner", what], capture_output=True, text=True)
            out += r.stdout
        names = set()
        for ln in out.splitlines():
            parts = ln.split()
            if len(parts) >= 2:
                names.add(parts[1])
        _FF_CAPS = names
    return _FF_CAPS


def parse_ratio(s, default=Fraction(1)):
    try:
        if s and ":" in s:
            n, d = s.split(":")
        elif s and "/" in s:
            n, d = s.split("/")
        else:
            return default
        n, d = int(n), int(d)
        return Fraction(n, d) if n > 0 and d > 0 else default
    except (ValueError, ZeroDivisionError):
        return default


KNOWN_RATES = {23.976: Fraction(24000, 1001), 29.97: Fraction(30000, 1001),
               59.94: Fraction(60000, 1001), 47.952: Fraction(48000, 1001),
               119.88: Fraction(120000, 1001)}


def parse_rate(txt: str) -> Fraction:
    f = float(txt.rstrip("k")) * (1000 if txt.endswith("k") else 1)
    for k, v in KNOWN_RATES.items():
        if abs(f - k) < 0.006:
            return v
    return Fraction(f).limit_denominator(1001)


def probe(path: Path) -> dict:
    """Read stream info from `ffmpeg -i` (no ffprobe needed, so one bundled binary suffices)."""
    r = subprocess.run([ffmpeg_exe(), "-hide_banner", "-nostdin", "-i", str(path)],
                       capture_output=True, text=True, errors="replace")
    txt = r.stderr
    vids = [ln for ln in txt.splitlines()
            if re.search(r"Stream #\d+:\d+.*: Video:", ln) and "attached pic" not in ln]
    if not vids:
        raise RuntimeError("no video stream (" + (txt.strip().splitlines() or ["?"])[-1][:120] + ")")
    v = vids[0]
    m = re.search(r"[\s,](\d{2,5})x(\d{2,5})(?=[\s,\[]|$)", v)
    if not m:
        raise RuntimeError("could not read video size")
    w, h = int(m.group(1)), int(m.group(2))
    sm = re.search(r"SAR (\d+):(\d+)", v)
    sar = parse_ratio(f"{sm.group(1)}:{sm.group(2)}") if sm else Fraction(1)
    fm = re.search(r"([\d.]+k?) fps", v) or re.search(r"([\d.]+k?) tbr", v)
    fps = parse_rate(fm.group(1)) if fm else Fraction(24)
    if fps > 120 or fps < 1:
        fps = Fraction(24)
    dm = re.search(r"Duration: (\d+):(\d+):([\d.]+)", txt)
    dur = int(dm.group(1)) * 3600 + int(dm.group(2)) * 60 + float(dm.group(3)) if dm else 0.0
    has_audio = re.search(r"Stream #\d+:\d+.*: Audio:", txt) is not None
    return {"w": w, "h": h, "sar": sar, "fps": fps, "duration": dur, "has_audio": has_audio}


def pick_fps(src: Fraction, cap: float) -> Fraction:
    """Largest src/k that is <= cap, so frames are dropped evenly (no judder)."""
    k = 1
    while src / k > cap + 0.01:
        k += 1
    return src / k


def geometry(p: dict):
    dar = Fraction(p["w"]) * p["sar"] / p["h"]
    if dar >= Fraction(4, 3):
        sw = SCREEN_W
        h = int(round(float(SCREEN_W / dar) / 4)) * 4
    else:
        h = SCREEN_H
        sw = int(round(float(SCREEN_H * dar) / 4)) * 4
    return max(4, min(SCREEN_W, sw)), max(4, min(SCREEN_H, h))


# --------------------------------------------------------------------------------------
# the block codec
# --------------------------------------------------------------------------------------
def build_palette(frames):
    """255 colours for a scene (+ index 0 = black), snapped to the DS's 5-bit colour space."""
    step = max(1, len(frames) // 8)
    samples = [Image.fromarray(f).reduce(2) for f in frames[::step][:8]]
    w, h = samples[0].size
    mont = Image.new("RGB", (w, h * len(samples)))
    for i, s in enumerate(samples):
        mont.paste(s, (0, i * h))
    q = mont.quantize(colors=255, method=Image.Quantize.MEDIANCUT, kmeans=2)
    pal = np.array(q.getpalette() or [], dtype=np.int32).reshape(-1, 3)[:255]
    if len(pal) < 255:
        pal = np.vstack([pal, np.zeros((255 - len(pal), 3), np.int32)])
    c5 = np.vstack([[[0, 0, 0]], to5(pal)])
    return c5  # (256,3) 5-bit values


class FrameEncoder:
    def __init__(self, height, lam, skip_max, dither):
        self.H = height
        self.bw, self.bh = SCREEN_W // 4, height // 4
        self.N = self.bw * self.bh
        self.lam = lam
        self.skip_max = skip_max
        self.dither = dither
        self.prev = None
        self.pat = np.tile(BAYER4, (self.bh + 1, self.bw))[:height, :, None] * dither
        self.bitw = (1 << np.arange(16)).astype(np.int64)

    def set_palette(self, c5):
        self.c5 = c5
        self.pal = from5(c5).astype(np.float32)
        img = Image.new("P", (1, 1))
        img.putpalette(self.pal.astype(np.uint8).flatten().tolist())
        self.pal_img = img
        self.pal_bytes = (c5[:, 0] | (c5[:, 1] << 5) | (c5[:, 2] << 10)).astype("<u2").tobytes()

    def _map(self, arr_hw3):
        im = Image.fromarray(np.clip(np.round(arr_hw3), 0, 255).astype(np.uint8))
        return np.asarray(im.quantize(palette=self.pal_img, dither=Image.Dither.NONE))

    def _nearest(self, cols):
        return self._map(cols.reshape(1, -1, 3)).reshape(-1)

    def encode(self, rgb, key):
        N, H = self.N, self.H
        src = rgb.astype(np.float32)
        sb = src.reshape(self.bh, 4, self.bw, 4, 3).transpose(0, 2, 1, 3, 4).reshape(N, 16, 3)
        pal = self.pal

        # RAW: every pixel mapped to the palette (optionally ordered-dithered)
        idx = self._map(src + self.pat if self.dither else src)
        ib = idx.reshape(self.bh, 4, self.bw, 4).transpose(0, 2, 1, 3).reshape(N, 16)
        raw_err = ((pal[ib] - sb) ** 2).sum(axis=(1, 2))

        # FILL: one colour
        mean = sb.mean(axis=1)
        fill_c = self._nearest(mean)
        fill_err = ((pal[fill_c][:, None, :] - sb) ** 2).sum(axis=(1, 2))

        # TWO-COLOUR: split on luma
        luma = sb @ np.array([0.299, 0.587, 0.114], np.float32)
        m = luma > luma.mean(axis=1, keepdims=True)
        n1 = m.sum(axis=1)[:, None]
        n0 = 16 - n1
        s1 = (sb * m[..., None]).sum(axis=1)
        s0 = (sb * ~m[..., None]).sum(axis=1)
        mean1 = np.where(n1 > 0, s1 / np.maximum(n1, 1), mean)
        mean0 = np.where(n0 > 0, s0 / np.maximum(n0, 1), mean)
        cc = self._nearest(np.concatenate([mean0, mean1]))
        c0, c1 = cc[:N], cc[N:]
        tc_rec = np.where(m, c1[:, None], c0[:, None]).astype(np.uint8)
        tc_err = ((pal[tc_rec] - sb) ** 2).sum(axis=(1, 2))
        mask = (m.astype(np.int64) * self.bitw).sum(axis=1)

        lam = self.lam
        inf = np.full(N, np.inf, np.float32)
        if key or self.prev is None:
            skip_cost = inf
        else:
            d = ((pal[self.prev] - sb) ** 2).sum(axis=2)
            skip_cost = np.where(d.max(axis=1) <= self.skip_max, d.sum(axis=1), inf)
        costs = np.stack([skip_cost, fill_err + lam * 1, raw_err + lam * 16, tc_err + lam * 4])
        mode = costs.argmin(axis=0).astype(np.uint8)

        fill_rec = np.repeat(fill_c[:, None], 16, axis=1).astype(np.uint8)
        prev = self.prev if self.prev is not None else ib
        mc = mode[:, None]
        rec = np.select([mc == 0, mc == 1, mc == 2], [prev, fill_rec, ib], tc_rec).astype(np.uint8)
        self.prev = rec

        pays = [None,
                fill_c.astype(np.uint8)[:, None],
                ib.astype(np.uint8),
                np.stack([c0, c1, mask & 255, mask >> 8], axis=1).astype(np.uint8)]
        flags = FRAME_KEY | FRAME_PAL if key else 0
        out = bytearray([flags, 0, 0, 0])
        if key:
            out += self.pal_bytes
        out += build_ops(mode, pays)
        return bytes(out)


def build_ops(mode, pays):
    n_blocks = len(mode)
    # trailing skips are implicit
    last = n_blocks
    while last > 0 and mode[last - 1] == 0:
        last -= 1
    out = bytearray()
    if last == 0:
        return bytes(out)
    mode = mode[:last]
    bounds = (np.flatnonzero(np.diff(mode)) + 1).tolist()
    for s, e in zip([0] + bounds, bounds + [last]):
        m = int(mode[s])
        pay = pays[m]
        while s < e:
            n = min(64, e - s)
            out.append((m << 6) | (n - 1))
            if m:
                out += pay[s:s + n].tobytes()
            s += n
    return bytes(out)


def decode_ops_py(data, prev_blocks, n_blocks):
    """Reference decoder (mirrors source/decode.c). Returns (N,16) uint8 blocks."""
    blk = prev_blocks.copy()
    b = 0
    p = 0
    L = len(data)
    while b < n_blocks and p < L:
        op = data[p]
        p += 1
        m, n = op >> 6, (op & 63) + 1
        if b + n > n_blocks:
            raise ValueError("block overrun")
        if m == 1:
            v = np.frombuffer(data, np.uint8, n, p)
            blk[b:b + n] = v[:, None]
            p += n
        elif m == 2:
            blk[b:b + n] = np.frombuffer(data, np.uint8, n * 16, p).reshape(n, 16)
            p += n * 16
        elif m == 3:
            a = np.frombuffer(data, np.uint8, n * 4, p).reshape(n, 4).astype(np.int64)
            mask = a[:, 2] | (a[:, 3] << 8)
            bits = ((mask[:, None] >> np.arange(16)) & 1).astype(bool)
            blk[b:b + n] = np.where(bits, a[:, 1:2], a[:, 0:1])
            p += n * 4
        b += n
    return blk


# --------------------------------------------------------------------------------------
# encoding one title
# --------------------------------------------------------------------------------------
def scene_luma(fr):
    return fr[::8, ::8].astype(np.float32) @ np.array([0.299, 0.587, 0.114], np.float32)


def encode_video(job):
    src = Path(job["src"])
    out_v = Path(job["out_v"])
    out_a = Path(job["out_a"])
    art_out = Path(job["art_out"])
    st = job["settings"]
    name = job["label"]
    p = probe(src)
    fps = pick_fps(p["fps"], st["fps"])
    sw, H = geometry(p)
    q = QUALITY[st["quality"]]

    # ---- thumbnail / art (done first so a failed encode still tells us something) ----
    if not art_out.exists():
        pick_thumbnail(src, p["duration"], art_out)

    # ---- video ----
    vf = []
    if st["deinterlace"]:
        if "bwdif" in ff_caps():
            vf.append("bwdif=mode=send_frame:parity=auto:deint=interlaced")
        elif "yadif" in ff_caps():
            vf.append("yadif=mode=send_frame:parity=auto:deint=interlaced")
    vf.append(f"fps={fps.numerator}/{fps.denominator}")
    vf.append(f"scale={sw}:{H}:flags=area")
    vf.append("setsar=1")
    if sw < SCREEN_W:
        vf.append(f"pad={SCREEN_W}:{H}:{(SCREEN_W - sw) // 2}:0:black")
    cmd = [ffmpeg_exe(), "-nostdin", "-v", "error", "-i", str(src), "-map", "0:v:0",
           "-vf", ",".join(vf), "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, bufsize=1 << 20)
    fsize = SCREEN_W * H * 3
    enc = FrameEncoder(H, q["lam"], q["skip_max"], st["dither"])
    min_gop = max(4, int(fps / 2))
    max_gop = max(min_gop, int(round(float(fps) * st["keyint"])))
    est_frames = max(1, int(p["duration"] * float(fps)))

    tmp_v = out_v.with_suffix(".rdv.part")
    try:
        return _encode_body(src, proc, tmp_v, out_v, out_a, fsize, H, enc, min_gop, max_gop,
                            est_frames, fps, st, p, name)
    except BaseException:
        proc.kill()
        proc.wait()
        tmp_v.unlink(missing_ok=True)
        raise


def _encode_body(src, proc, tmp_v, out_v, out_a, fsize, H, enc, min_gop, max_gop, est_frames, fps, st, p, name):
    kfs = []
    state = {"n": 0, "bytes": 0, "next_report": 0.1}
    t0 = time.time()
    base_lam = enc.lam
    budget = st["max_rate"] * 1024 * fps.denominator / fps.numerator  # bytes per frame

    with open(tmp_v, "wb") as f:
        f.write(b"\0" * RDV_HEADER.size)

        def flush(gop):
            if not gop:
                return
            enc.set_palette(build_palette(gop))
            for i, fr in enumerate(gop):
                if i == 0:
                    kfs.append((state["n"], f.tell()))
                payload = enc.encode(fr, i == 0)
                # crude rate control: keep the stream inside what the SD card can feed
                if i and len(payload) > budget:
                    enc.lam = min(enc.lam * 1.25, base_lam * 40)
                elif i:
                    enc.lam = max(base_lam, enc.lam * 0.97)
                f.write(struct.pack("<I", len(payload)))
                f.write(payload)
                state["n"] += 1
                state["bytes"] += len(payload) + 4
            if state["n"] / est_frames >= state["next_report"]:
                log(f"  [{name}] {min(99, int(100 * state['n'] / est_frames))}%")
                state["next_report"] += 0.1

        gop, prev_l = [], None
        while True:
            buf = proc.stdout.read(fsize)
            if not buf or len(buf) < fsize:
                break
            fr = np.frombuffer(buf, np.uint8).reshape(H, SCREEN_W, 3)
            lum = scene_luma(fr)
            cut = prev_l is not None and len(gop) >= min_gop and np.abs(lum - prev_l).mean() > 28
            if cut or len(gop) >= max_gop:
                flush(gop)
                gop = []
            gop.append(fr)
            prev_l = lum
        flush(gop)
        proc.wait()
        if proc.returncode != 0 or state["n"] == 0:
            raise RuntimeError("ffmpeg video decode failed")

        kf_off = f.tell()
        for fn, off in kfs:
            f.write(struct.pack("<II", fn, off))
        if f.tell() > 0x7FFFFFFF:
            raise RuntimeError("encoded video > 2 GB; use --quality low or a lower --fps")

        # ---- audio ----
        rate = st["audio_rate"]
        af = []
        if st["audio_boost"]:
            af.append("dynaudnorm=f=200:g=15:p=0.9")
        if p["has_audio"]:
            acmd = [ffmpeg_exe(), "-nostdin", "-v", "error", "-y", "-i", str(src), "-map", "0:a:0", "-vn"]
            if af:
                acmd += ["-af", ",".join(af)]
        else:
            dur = state["n"] * fps.denominator / fps.numerator
            acmd = [ffmpeg_exe(), "-nostdin", "-v", "error", "-y", "-f", "lavfi",
                    "-i", f"anullsrc=r={rate}:cl=mono", "-t", f"{dur:.3f}"]
        acmd += ["-ac", "1", "-ar", str(rate), "-f", "s16le", "-acodec", "pcm_s16le", str(out_a)]
        r = subprocess.run(acmd, capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"ffmpeg audio failed: {r.stderr.strip()[:200]}")
        n_samples = out_a.stat().st_size // 2

        f.seek(0)
        f.write(RDV_HEADER.pack(b"RDV1", ENCODER_VERSION, SCREEN_W, H, 0, fps.numerator,
                                fps.denominator, state["n"], rate, n_samples, len(kfs),
                                kf_off, 0, 0, 0, 0, 0, 0))
    os.replace(tmp_v, out_v)
    secs = state["n"] * fps.denominator / fps.numerator
    dt = time.time() - t0
    kbps = state["bytes"] / max(secs, 0.001) / 1024
    log(f"  [{name}] done: {state['n']} frames @ {float(fps):.2f} fps, 256x{H}, "
        f"{kbps:.0f} KB/s video, encoded at {state['n'] / max(dt, 0.001):.0f} fps")
    return {"frames": state["n"], "fps_num": fps.numerator, "fps_den": fps.denominator,
            "duration": secs, "height": H}


def grab_frame(src: Path, t: float):
    cmd = [ffmpeg_exe(), "-nostdin", "-v", "error", "-ss", f"{t:.2f}", "-i", str(src), "-frames:v", "1",
           "-vf", "scale=-2:270:flags=area,setsar=1", "-f", "image2pipe", "-vcodec", "png", "-"]
    r = subprocess.run(cmd, capture_output=True)
    if r.returncode != 0 or not r.stdout:
        return None
    return Image.open(io.BytesIO(r.stdout)).convert("RGB")


def pick_thumbnail(src: Path, duration: float, out: Path):
    """Score a handful of candidate frames; keep the most 'interesting' one."""
    best, best_s = None, -1.0
    times = [duration * f for f in (0.08, 0.15, 0.25, 0.35, 0.5, 0.65)] if duration > 4 else [0.0]
    for t in times:
        im = grab_frame(src, t)
        if im is None:
            continue
        a = np.asarray(im.resize((96, 54)), dtype=np.float32)
        lum = a @ np.array([0.299, 0.587, 0.114], np.float32)
        sat = a.max(axis=2) - a.min(axis=2)
        s = float(lum.std() + 0.35 * sat.mean())
        if lum.mean() < 35 or lum.mean() > 225:
            s *= 0.3
        if s > best_s:
            best, best_s = im, s
    if best is None:
        best = Image.new("RGB", (480, 270), (40, 40, 40))
    best.save(out)


# --------------------------------------------------------------------------------------
# metadata
# --------------------------------------------------------------------------------------
JUNK = re.compile(r"\b(480p|576p|720p|1080[pi]|2160p|4k|uhd|x26[45]|h\.?26[45]|hevc|avc|xvid|divx|"
                  r"blu-?ray|brrip|bdrip|web[- .]?dl|webrip|hdtv|dvdrip|dvdscr|dvd|ntsc|pal|aac\d?|"
                  r"ac3|dts|proper|repack|remux|10bit|hdr)\b", re.I)


def parse_name(stem: str):
    s = re.sub(r"[\[\{][^\]\}]*[\]\}]", " ", stem).replace("_", " ")
    if (s.count(".") >= 2 and s.count(" ") < 2) or (" " not in s.strip() and "." in s):
        s = s.replace(".", " ")
    year = None
    cut = len(s)
    ym = [m for m in re.finditer(r"(?:^|[\s(])((?:19[2-9]|20[0-4])\d)(?=$|[\s)])", s) if m.start(1) > 0]
    ym = ym[-1] if ym else None
    if ym:
        year = ym.group(1)
        cut = min(cut, ym.start(1))
    jm = JUNK.search(s)
    if jm and jm.start() > 0:
        cut = min(cut, jm.start())
    title = re.sub(r"\s+", " ", s[:cut]).strip(" -.([")
    return (title or stem), year


def read_sidecar(path: Path):
    meta = {}
    txt = path.with_suffix(".txt")
    if txt.exists():
        lines = txt.read_text(encoding="utf-8", errors="replace").splitlines()
        body = []
        for ln in lines:
            m = re.match(r"^\s*(title|year|category)\s*:\s*(.*)$", ln, re.I)
            if m and not body:
                meta[m.group(1).lower()] = m.group(2).strip()
            else:
                body.append(ln)
        desc = " ".join(x.strip() for x in body if x.strip())
        if desc:
            meta["desc"] = desc
    for ext in ART_EXTS:
        for cand in (path.with_suffix(ext), path.with_suffix(ext.upper())):
            if cand.exists():
                meta["art"] = cand
                break
        if "art" in meta:
            break
    return meta


def fmt_duration(sec):
    sec = int(round(sec))
    h, m = sec // 3600, (sec % 3600) // 60
    if h:
        return f"{h}h {m}m"
    if m:
        return f"{m}m"
    return f"{sec}s"


# --------------------------------------------------------------------------------------
# UI rendering (everything the DSi displays is pre-rendered here)
# --------------------------------------------------------------------------------------
class Fonts:
    BOLD = ["DejaVuSans-Bold.ttf", "arialbd.ttf", "Arial Bold.ttf", "LiberationSans-Bold.ttf",
            "segoeuib.ttf", "Helvetica.ttc"]
    REG = ["DejaVuSans.ttf", "arial.ttf", "Arial.ttf", "LiberationSans-Regular.ttf",
           "segoeui.ttf", "Helvetica.ttc"]

    def __init__(self, reg=None, bold=None):
        self.reg_path = self._find([reg] if reg else self.REG)
        self.bold_path = self._find([bold] if bold else self.BOLD) or self.reg_path
        self.cache = {}

    @staticmethod
    def _find(names):
        for n in names:
            try:
                ImageFont.truetype(n, 10)
                return n
            except OSError:
                continue
        return None

    def get(self, size, bold=False):
        key = (size, bold)
        if key not in self.cache:
            path = self.bold_path if bold else self.reg_path
            if path:
                self.cache[key] = ImageFont.truetype(path, size)
            else:
                try:
                    self.cache[key] = ImageFont.load_default(size)
                except TypeError:
                    self.cache[key] = ImageFont.load_default()
        return self.cache[key]


def ellipsize(text, font, width):
    if font.getlength(text) <= width:
        return text
    while text and font.getlength(text + "…") > width:
        text = text[:-1]
    return text.rstrip() + "…"


def wrap(text, font, width, max_lines):
    """Greedy word wrap; the last line gets an ellipsis if text was cut."""
    words = text.split()
    lines, cur, i = [], "", 0
    while i < len(words) and len(lines) < max_lines:
        t = (cur + " " + words[i]).strip()
        if font.getlength(t) <= width or not cur:
            cur = t
            i += 1
        else:
            lines.append(cur)
            cur = ""
    if cur and len(lines) < max_lines:
        lines.append(cur)
    lines = [ellipsize(l, font, width) for l in lines]
    if i < len(words) and lines and not lines[-1].endswith("…"):
        last = lines[-1]
        while last and font.getlength(last + "…") > width:
            last = last[:-1]
        lines[-1] = last.rstrip() + "…"
    return lines


def cover(im: Image.Image, w, h, focus_y=0.4):
    sc = max(w / im.width, h / im.height)
    nw, nh = max(w, round(im.width * sc)), max(h, round(im.height * sc))
    im = im.resize((nw, nh), Image.LANCZOS)
    x = (nw - w) // 2
    y = int((nh - h) * focus_y)
    return im.crop((x, y, x + w, y + h))


def vgrad(w, h, y0, y1, a0, a1):
    """Alpha mask: a0 above y0, a1 below y1, linear in between."""
    ys = np.arange(h, dtype=np.float32)
    t = np.clip((ys - y0) / max(1, y1 - y0), 0, 1)
    col = (a0 + (a1 - a0) * t)[:, None]
    return Image.fromarray(np.repeat(col, w, axis=1).clip(0, 255).astype(np.uint8), "L")


def shadow_text(d, xy, text, font, fill, shadow=(0, 0, 0)):
    x, y = xy
    d.text((x + 1, y + 1), text, font=font, fill=shadow)
    d.text((x, y), text, font=font, fill=fill)


def render_tile(art, title, fonts, badge=None):
    # episodes: lead with the SxxEyy tag so truncation never hides which episode it is
    ep = re.search(r"\bS\d{1,2}E\d{1,3}\b", title, re.I)
    if ep:
        rest = (title[:ep.start()] + title[ep.end():]).strip(" -")
        title = ep.group(0).upper() + (" " + rest if rest else "")
    im = cover(art, TILE_W, TILE_H)
    bg = Image.new("RGB", im.size, (0, 0, 0))
    im = Image.composite(bg, im, vgrad(TILE_W, TILE_H, 18, TILE_H, 0, 210))
    d = ImageDraw.Draw(im)
    f = fonts.get(8, True)
    shadow_text(d, (3, TILE_H - 11), ellipsize(title, f, TILE_W - 6), f, (255, 255, 255))
    if badge:   # series: episode count in the top-right corner
        fb = fonts.get(7, True)
        w = int(fb.getlength(badge)) + 6
        d.rounded_rectangle((TILE_W - w - 2, 2, TILE_W - 2, 12), 2, fill=RED)
        d.text((TILE_W - w + 1, 3), badge, font=fb, fill=(255, 255, 255))
    return im


def render_hero(art, title, meta_line, desc, fonts):
    W, H = SCREEN_W, SCREEN_H
    ft, fm, fd = fonts.get(16, True), fonts.get(10), fonts.get(10)
    tl = wrap(title, ft, W - 16, 2)
    dl = wrap(desc, fd, W - 16, 3) if desc else []
    block_h = len(tl) * 18 + 14 + len(dl) * 12
    top = H - 6 - block_h
    im = cover(art, W, H, focus_y=0.3)
    bg = Image.new("RGB", (W, H), BG_RGB)
    im = Image.composite(bg, im, vgrad(W, H, top - 46, top + 6, 0, 255))
    # gentle left vignette behind the text
    xs = np.clip((140 - np.arange(W, dtype=np.float32)) / 140, 0, 1) * 90
    im = Image.composite(bg, im, Image.fromarray(np.repeat(xs[None, :], H, 0).astype(np.uint8), "L"))
    d = ImageDraw.Draw(im)
    shadow_text(d, (8, 5), "REELDS", fonts.get(10, True), RED)
    y = top
    for ln in tl:
        shadow_text(d, (8, y), ln, ft, (255, 255, 255))
        y += 18
    d.text((8, y), ellipsize(meta_line, fm, W - 16), font=fm, fill=GREY)
    y += 14
    for ln in dl:
        d.text((8, y), ln, font=fd, fill=(205, 205, 205))
        y += 12
    return im


def render_panel(art, title, meta_line, desc, fonts):
    im = Image.new("RGB", (SCREEN_W, SCREEN_H), BG_RGB)
    d = ImageDraw.Draw(im)
    im.paste(cover(art, 96, 54), (8, 8))
    ft = fonts.get(12, True)
    y = 8
    for ln in wrap(title, ft, 140, 3):
        d.text((112, y), ln, font=ft, fill=(255, 255, 255))
        y += 14
    d.text((112, y + 2), ellipsize(meta_line, fonts.get(9), 136), font=fonts.get(9), fill=GREY)
    fd = fonts.get(9)
    y = 70
    for ln in wrap(desc, fd, SCREEN_W - 16, 4) if desc else []:
        d.text((8, y), ln, font=fd, fill=(190, 190, 190))
        y += 11
    # progress bar + time readout are drawn by the DSi at y=140 / y=148
    d.line((8, 166, SCREEN_W - 8, 166), fill=(45, 45, 45))
    im.paste(render_hints("normal", fonts), (0, HINT_Y))
    return im


HINT_Y, HINT_H = 167, 25
HINTS = {
    "normal": ("A pause   Left/Right 10s   L/R 1 min",
               "Up/Down volume   B back   SELECT/tap screen off"),
    "notouch": ("A pause   Left/Right 10s   L/R 1 min",
                "Up/Down volume   B back   SELECT screen off"),
    "locked": ("A  play / pause",
               "To leave: hold START + SELECT for 4 seconds"),
}


def _shuffle_icon(d, cx, cy, size, fill, width):
    """Two crossing arrows (the usual 'shuffle' glyph), drawn with lines so no font needs it."""
    w, h = size, size * 0.62
    x0, x1, y0, y1 = cx - w / 2, cx + w / 2, cy - h / 2, cy + h / 2
    ah = size * 0.22
    for ya, yb in ((y0, y1), (y1, y0)):
        d.line([(x0, ya), (x0 + w * 0.3, ya), (x1 - w * 0.3, yb), (x1 - ah * 0.6, yb)], fill=fill,
               width=width, joint="curve")
        d.polygon([(x1, yb), (x1 - ah, yb - ah * 0.7), (x1 - ah, yb + ah * 0.7)], fill=fill)


def render_shuffle_tile(fonts):
    a = np.linspace(0, 1, TILE_W, dtype=np.float32)[None, :, None]
    c0, c1 = np.array([150, 6, 14], np.float32), np.array([229, 9, 20], np.float32)
    im = Image.fromarray((c0 + (c1 - c0) * a).repeat(TILE_H, 0).astype(np.uint8))
    d = ImageDraw.Draw(im)
    _shuffle_icon(d, TILE_W / 2, 15, 24, (255, 255, 255), 3)
    f = fonts.get(9, True)
    t = "Shuffle"
    d.text(((TILE_W - f.getlength(t)) / 2, 27), t, font=f, fill=(255, 255, 255))
    return im


def render_shuffle_hero(fonts):
    W, H = SCREEN_W, SCREEN_H
    yy = np.linspace(0, 1, H, dtype=np.float32)[:, None, None]
    top, bot = np.array([110, 4, 10], np.float32), np.array(BG_RGB, np.float32)
    im = Image.fromarray((top + (bot - top) * np.clip(yy * 1.4, 0, 1)).repeat(W, 1).astype(np.uint8))
    d = ImageDraw.Draw(im)
    shadow_text(d, (8, 5), "REELDS", fonts.get(10, True), RED)
    _shuffle_icon(d, W / 2, 68, 70, (255, 255, 255), 7)
    ft = fonts.get(18, True)
    t = "Shuffle"
    shadow_text(d, ((W - ft.getlength(t)) / 2, 112), t, ft, (255, 255, 255))
    fd = fonts.get(10)
    for i, ln in enumerate(("Plays something random from here,", "then keeps picking more.")):
        d.text(((W - fd.getlength(ln)) / 2, 142 + i * 13), ln, font=fd, fill=(205, 205, 205))
    return im


def render_hints(kind, fonts):
    """Controls hint at the bottom of the now-playing screen. The DSi swaps in the
    'notouch' / 'locked' variants (stored once in the catalog) to match settings.ini."""
    im = Image.new("RGB", (SCREEN_W, HINT_H), BG_RGB)
    d = ImageDraw.Draw(im)
    fh = fonts.get(9)
    a, b = HINTS[kind]
    d.text((8, 3), a, font=fh, fill=(140, 140, 140) if kind != "locked" else (230, 230, 230))
    d.text((8, 14), b, font=fh, fill=(140, 140, 140))
    return im


def render_label(text, fonts):
    im = Image.new("RGB", (LABEL_W, LABEL_H), BG_RGB)
    d = ImageDraw.Draw(im)
    f = fonts.get(10, True)
    d.text((0, 0), ellipsize(text, f, LABEL_W), font=f, fill=(229, 229, 229))
    return im


def render_header(fonts, title=None):
    """Bottom-screen header: the logo on the home page, the show's name inside a series."""
    im = Image.new("RGB", (HEADER_W, HEADER_H), BG_RGB)
    d = ImageDraw.Draw(im)
    f = fonts.get(9)
    hint = "A play   SELECT exit" if title is None else "A play   B back"
    hw = f.getlength(hint)
    if title is None:
        d.text((8, 3), "REELDS", font=fonts.get(13, True), fill=RED)
    else:
        d.rectangle((8, 5, 10, 15), fill=RED)
        ft = fonts.get(12, True)
        d.text((15, 3), ellipsize(title, ft, HEADER_W - 30 - hw), font=ft, fill=(255, 255, 255))
    d.text((HEADER_W - 8 - hw, 6), hint, font=f, fill=(130, 130, 130))
    return im


def render_digits(fonts):
    im = Image.new("RGB", (DIGIT_W * len(DIGIT_CHARS), DIGIT_H), BG_RGB)
    d = ImageDraw.Draw(im)
    f = fonts.get(10)
    for i, ch in enumerate(DIGIT_CHARS):
        w = f.getlength(ch)
        d.text((i * DIGIT_W + (DIGIT_W - w) / 2, 0), ch, font=f, fill=(220, 220, 220))
    return im


# --------------------------------------------------------------------------------------
# build command
# --------------------------------------------------------------------------------------
def scan_library(roots):
    found = []
    for root in roots:
        root = Path(root).resolve()
        if root.is_file():
            found.append((root.parent, root))
            continue
        for p in sorted(root.rglob("*")):
            if p.suffix.lower() in VIDEO_EXTS and p.is_file() and not p.name.startswith("."):
                found.append((root, p))
    return found


EP_RE = re.compile(r"\bS\d{1,2}\s*E\d{1,3}\b|\b\d{1,2}x\d{2,3}\b|\b(?:episode|ep)\.?\s*\d{1,3}\b", re.I)
SEASON_RE = re.compile(r"^(?:season|series|staffel|saison|temporada|s)\s*\d+\b|^(?:specials?|extras|bonus)$", re.I)
SERIES_ART = ("folder", "poster", "cover", "show", "series", "fanart")


def find_series(files):
    """Map each video to the show folder it belongs to (or None).

    A folder is a show if it has a series.txt, or holds episode-named videos, or holds
    season folders ("Season 1", "S02", "Specials"...). Season folders are never shows
    themselves, and library roots aren't either (unless they have a series.txt), so
    Shows/Ninjago/Season 1/ep.mkv -> show "Ninjago", season row "Season 1".
    """
    direct, children = {}, {}
    for root, p in files:
        direct.setdefault(p.parent, []).append(p)
        f = p.parent
        while f != root and f.parent != f:
            children.setdefault(f.parent, set()).add(f)
            f = f.parent
    cache = {}

    def is_series(f, root):
        if f in cache:
            return cache[f]
        r = False
        if (f / "series.txt").exists():
            r = True
        elif f != root and not SEASON_RE.match(f.name):
            if any(EP_RE.search(p.stem) for p in direct.get(f, [])):
                r = True
            elif any(SEASON_RE.match(c.name) and direct.get(c) for c in children.get(f, ())):
                r = True
        cache[f] = r
        return r

    out = {}
    for root, p in files:
        show, f = None, p.parent
        while True:
            if is_series(f, root):
                show = f
            if f == root or f.parent == f:
                break
            f = f.parent
        out[p] = show       # outermost show folder wins
    return out


def row_name(folder: Path, root: Path):
    rel = folder.relative_to(root).as_posix()
    return root.name if rel in ("", ".") else rel.replace("/", " - ")


SETTINGS_KEYS = {"autoplay": True, "lock_buttons": False, "lock_touch": False, "shuffle_tile": True,
                 "show_games": True}
SETTINGS_STR = {"reelds_path": ""}   # where reelds.nds lives (games' "Quit" comes back to it)


def read_settings(sd) -> dict:
    cfg = dict(SETTINGS_KEYS)
    cfg.update(SETTINGS_STR)
    f = Path(sd) / "reelds" / "settings.ini"
    if f.exists():
        for ln in f.read_text(errors="replace").splitlines():
            k, _, v = ln.partition("=")
            k = k.strip().lower()
            if k in cfg:
                cfg[k] = v.strip().lower() in ("1", "on", "yes", "true")
            elif k in SETTINGS_STR:
                cfg[k] = v.strip()
    return cfg


def write_settings(sd, **changes) -> dict:
    """settings.ini on the SD card is read by the DSi at startup (no rebuild needed)."""
    cfg = read_settings(sd)
    cfg.update({k: bool(v) for k, v in changes.items() if v is not None and k in SETTINGS_KEYS})
    cfg.update({k: str(v) for k, v in changes.items() if v is not None and k in SETTINGS_STR})
    d = Path(sd) / "reelds"
    d.mkdir(parents=True, exist_ok=True)
    (d / "settings.ini").write_text(
        "# ReelDS settings - read by the DSi at startup. Change them here or in the ReelDS app.\n"
        "# autoplay:     play the next episode when one ends\n"
        "# lock_buttons: during playback only A (play/pause) works; hold START+SELECT for 4 s to leave\n"
        "# lock_touch:   during playback the touchscreen does nothing\n"
        "# shuffle_tile: show a Shuffle tile that plays random videos (random episodes inside a show)\n"
        "# show_games:   show the Games row(s)\n"
        "# reelds_path:  where reelds.nds is on the card; quitting a game comes back to it\n"
        + "".join(f"{k}={v if isinstance(v, str) else int(v)}\n" for k, v in cfg.items()))
    return cfg


def natural_key(s):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]


def find_reelds_nds(sd: Path) -> str:
    """Locate reelds.nds on the card so a game's Quit can come back to it."""
    cands = []
    for d in (sd, sd / "_nds", sd / "roms" / "nds", sd / "roms", sd / "homebrew"):
        if d.is_dir():
            cands += [p for p in d.iterdir() if p.suffix.lower() == ".nds" and "reelds" in p.name.lower()]
    return G.ds_path(cands[0], sd) if cands else ""


def scan_games(roots, sd_root: Path):
    """Every .nds under the game folders -> catalog entries (copied to the card if needed)."""
    out, seen = [], set()
    for root in roots:
        root = Path(root).resolve()
        if not root.exists():
            log(f"  !! game folder not found: {root}")
            continue
        files = [root] if root.is_file() else sorted(root.rglob("*"), key=lambda p: natural_key(str(p)))
        for p in files:
            if p.suffix.lower() not in G.GAME_EXTS or not p.is_file() or p.name.startswith("."):
                continue
            if "reelds" in p.name.lower() or "saves" in p.relative_to(root).parts[:-1]:
                continue            # don't list ourselves, never look inside saves folders
            try:
                rom = G.read_rom(p)
            except (OSError, ValueError) as exc:
                log(f"  !! skipped {p.name}: {exc}")
                continue
            if rom["dsiware"]:
                log(f"  skipped {p.name}: DSiWare isn't supported")
                continue
            on_card = G.place_on_card(p, root if root.is_dir() else root.parent, sd_root)
            ds = G.ds_path(on_card, sd_root)
            if ds.lower() in seen:
                continue
            seen.add(ds.lower())
            rel = p.parent.relative_to(root).as_posix() if root.is_dir() else "."
            size = 0 if rom["homebrew"] else G.save_size_for(rom["code"])
            if G.ensure_save(on_card, size):
                log(f"  created save for {rom['name']} ({size // 1024} KB)")
            art_p = G.find_boxart(p, sd_root, rom["code"]) or G.find_boxart(on_card, sd_root, rom["code"])
            out.append({"id": G.game_id(ds), "kind": G.KIND_HOMEBREW if rom["homebrew"] else G.KIND_RETAIL,
                        "path": p, "on_card": on_card, "ds_path": ds, "rom": rom,
                        "title": rom["name"], "save_size": size, "show": None, "season": None,
                        "category": "Games" if rel in ("", ".") else rel.replace("/", " - "),
                        "art": Image.open(art_p).convert("RGB") if art_p else None})
    return out


def order_rows(names, wanted=None, default_key=natural_key):
    """Home-page row order: rows named in `wanted` first, in that order; the rest after them
    in the default order."""
    wanted = [w for w in (wanted or []) if w in names]
    rest = sorted((n for n in names if n not in wanted), key=default_key)
    return wanted + rest


def game_row_names(roots):
    """Row names game folders produce ("Games", or the sub-folder), without copying anything."""
    names = set()
    for root in roots:
        root = Path(root).resolve()
        if not root.is_dir():
            continue
        for p in root.rglob("*"):
            if (p.suffix.lower() in G.GAME_EXTS and p.is_file() and not p.name.startswith(".")
                    and "reelds" not in p.name.lower() and "saves" not in p.relative_to(root).parts[:-1]):
                rel = p.parent.relative_to(root).as_posix()
                names.add("Games" if rel in ("", ".") else rel.replace("/", " - "))
    return names


def home_row_names(library, games=()):
    """The home-page rows a build of these folders would produce, in default order, without
    encoding anything (used by the GUI's "Arrange rows" window). Game rows come last."""
    files = scan_library(library)
    shows = find_series(files)
    names = set()
    for root, p in files:
        show = shows[p]
        if show is None:
            cat = row_name(p.parent, root)
            side_cat = read_sidecar(p).get("category")
            names.add(side_cat or cat)
        else:
            names.add(row_name(show.parent, root) if show != root else root.name)
    g = game_row_names(games)
    return order_rows(names | g, default_key=lambda c: (c in g and c not in names, natural_key(c)))


def cmd_rows(a):
    for n in home_row_names(a.library, getattr(a, "games", None) or []):
        print(n)


def cmd_build(a):
    need_tools()
    out_root = Path(a.out).resolve() / "reelds"
    vid_dir = out_root / "videos"
    vid_dir.mkdir(parents=True, exist_ok=True)
    cache = Path(a.cache).expanduser().resolve() if a.cache else Path.home() / ".cache" / "reelds"
    cache.mkdir(parents=True, exist_ok=True)
    man_path = cache / "manifest.json"
    manifest = json.loads(man_path.read_text()) if man_path.exists() else {}

    settings = {"fps": a.fps, "quality": a.quality, "dither": a.dither, "audio_rate": a.audio_rate,
                "audio_boost": not a.no_audio_boost, "deinterlace": not a.no_deinterlace,
                "keyint": a.keyint, "max_rate": a.max_rate}
    skey = json.dumps([ENCODER_VERSION, settings], sort_keys=True)

    sd_root = Path(a.out).resolve()
    game_entries = scan_games(a.games or [], sd_root)
    if game_entries:
        n_hb = sum(1 for g in game_entries if g["kind"] == G.KIND_HOMEBREW)
        log(f"found {len(game_entries)} game(s) ({n_hb} homebrew)")

    entries = []
    files = scan_library(a.library or [])
    shows = find_series(files)
    for root, p in files:
        rel = p.relative_to(root)
        vid = hashlib.sha1(str(Path(root.name) / rel).lower().encode()).hexdigest()[:8]
        show = shows[p]
        if show is None:
            category, season = row_name(p.parent, root), None
        else:   # the show's tile sits in its parent's row; episodes go in season rows
            category = row_name(show.parent, root) if show != root else root.name
            sub = p.parent.relative_to(show).as_posix()
            season = "Episodes" if sub in ("", ".") else sub.replace("/", " - ")
        stat = p.stat()
        entries.append({"id": vid, "path": p, "category": category, "show": show, "season": season,
                        "sig": [stat.st_size, int(stat.st_mtime), skey]})
    if not entries and not game_entries:
        sys.exit("no videos or games found")
    if entries:
        log(f"found {len(entries)} video(s)")

    jobs = []
    for e in entries:
        m = manifest.get(e["id"])
        out_v, out_a = vid_dir / f"{e['id']}.rdv", vid_dir / f"{e['id']}.rda"
        if m and m.get("sig") == e["sig"] and out_v.exists() and out_a.exists() and not a.force:
            e["tech"] = m["tech"]
            continue
        jobs.append({"src": str(e["path"]), "out_v": str(out_v), "out_a": str(out_a),
                     "art_out": str(cache / f"{e['id']}.png"), "settings": settings,
                     "label": e["path"].stem[:40], "id": e["id"]})

    if jobs:
        log(f"encoding {len(jobs)} video(s) with {a.jobs} worker(s)...")
        by_id = {e["id"]: e for e in entries}
        with ProcessPoolExecutor(max_workers=a.jobs) as ex:
            futs = {ex.submit(encode_video, j): j for j in jobs}
            for fut in as_completed(futs):
                j = futs[fut]
                try:
                    tech = fut.result()
                except Exception as exc:  # keep going with the rest of the library
                    log(f"  !! failed: {j['src']}: {exc}")
                    by_id[j["id"]]["failed"] = True
                    continue
                e = by_id[j["id"]]
                e["tech"] = tech
                manifest[e["id"]] = {"sig": e["sig"], "tech": tech, "src": str(e["path"])}
                man_path.write_text(json.dumps(manifest, indent=1))
    entries = [e for e in entries if not e.get("failed") and "tech" in e]
    if not entries and not game_entries:
        sys.exit("nothing encoded successfully; catalog not written")

    # ---- metadata + artwork ----
    fonts = Fonts(a.font, a.font_bold)
    if not fonts.reg_path:
        log("warning: no TrueType font found, using Pillow's built-in font (use --font)")
    for e in entries:
        side = read_sidecar(e["path"])
        title, year = parse_name(e["path"].stem)
        e["title"] = side.get("title", title)
        e["year"] = side.get("year", year)
        if side.get("category"):
            e["category"] = side["category"]
        e["desc"] = side.get("desc", "")
        art_p = side.get("art") or (cache / f"{e['id']}.png")
        if not Path(art_p).exists():
            pick_thumbnail(e["path"], e["tech"]["duration"], cache / f"{e['id']}.png")
            art_p = cache / f"{e['id']}.png"
        e["art"] = Image.open(art_p).convert("RGB")

    pages = build_pages(entries + game_entries, a.row_order)
    write_catalog(out_root / "catalog.bin", pages, fonts)
    onoff = lambda v: None if v is None else v == "on"
    cfg = write_settings(a.out, autoplay=onoff(a.autoplay), lock_buttons=onoff(a.lock_buttons),
                         lock_touch=onoff(a.lock_touch), shuffle_tile=onoff(a.shuffle),
                         show_games=onoff(a.show_games),
                         reelds_path=find_reelds_nds(sd_root) or None)
    if game_entries and not cfg["reelds_path"]:
        log("note: reelds.nds isn't on the card yet - copy it to the card root and build again, "
            "so quitting a game comes back to ReelDS")
    log("settings: " + ", ".join(f"{k}={v if isinstance(v, str) else ('on' if v else 'off')}"
                                 for k, v in cfg.items()))

    if a.prune:
        keep = {e["id"] for e in entries}
        for f in vid_dir.iterdir():
            if f.suffix in (".rdv", ".rda") and f.stem not in keep:
                f.unlink()
                log(f"  pruned {f.name}")
        # copies of games made by earlier builds (saves are never touched)
        gdir = out_root / "games"
        keep_g = {g["on_card"].resolve() for g in game_entries}
        if gdir.is_dir():
            for f in gdir.rglob("*"):
                if f.suffix.lower() in G.GAME_EXTS and f.is_file() and f.resolve() not in keep_g:
                    f.unlink()
                    log(f"  pruned {f.relative_to(out_root)}")
    total = sum(f.stat().st_size for f in vid_dir.iterdir() if f.is_file())
    log(f"catalog: {len(pages[0]['rows'])} row(s), {len(pages) - 1} show(s), {len(entries)} video(s), "
        f"{len(game_entries)} game(s), {total / 2**20:.0f} MB in {vid_dir}")


def build_pages(entries, row_order=None):
    """Page 0 is the home screen; every show gets its own page with one row per season."""
    shows = {}
    for e in entries:
        if e["show"] is not None:
            shows.setdefault(e["show"], []).append(e)

    home = {}   # row name -> [(sort key, entry)]
    pages = [None]
    for e in entries:
        if e["show"] is None:
            home.setdefault(e["category"], []).append((natural_key(e["path"].name), ("item", e)))
    for folder in sorted(shows, key=lambda f: natural_key(f.name)):
        eps = shows[folder]
        seasons = {}
        for e in eps:
            seasons.setdefault(e["season"], []).append(e)
        order = sorted(seasons, key=lambda n: (n == "Episodes" and len(seasons) > 1, natural_key(n)))
        rows = [(n, [("item", e) for e in sorted(seasons[n], key=lambda e: natural_key(e["path"].name))])
                for n in order]
        side = read_sidecar(folder / "series.txt") if (folder / "series.txt").exists() else {}
        title, year = parse_name(folder.name)
        title = side.get("title", title)
        art = None
        for stem in SERIES_ART:
            for ext in ART_EXTS:
                if (folder / (stem + ext)).exists():
                    art = Image.open(folder / (stem + ext)).convert("RGB")
                    break
            if art:
                break
        first_ep = rows[0][1][0][1]
        page = {"title": title, "year": side.get("year", year), "desc": side.get("desc", ""),
                "art": art or first_ep["art"], "rows": rows, "n_eps": len(eps), "n_seasons": len(rows)}
        pages.append(page)
        home.setdefault(eps[0]["category"], []).append((natural_key(folder.name), ("page", len(pages) - 1)))
        # episode tiles drop the show name (the page header already says it)
        names = sorted({title, parse_name(folder.name)[0]}, key=len, reverse=True)
        for e in eps:
            short = e["title"]
            for n in names:
                if short.lower().startswith(n.lower()) and len(short) > len(n):
                    short = short[len(n):].strip(" -.:")
                    break
            e["tile_title"] = short
            e["meta_prefix"] = "" if e["season"] == "Episodes" else e["season"]
    game_rows = {e["category"] for e in entries if e.get("kind", 0) != G.KIND_VIDEO}
    pages[0] = {"title": None, "rows": [(c, [x for _, x in sorted(home[c], key=lambda t: t[0])])
                                        for c in order_rows(home, row_order,
                                                            lambda c: (c in game_rows, natural_key(c)))]}
    return pages


def write_catalog(path: Path, pages, fonts):
    blobs = bytearray()

    def add_blob(b: bytes) -> int:
        while len(blobs) % 4:
            blobs.append(0)
        off = len(blobs)
        blobs.extend(b)
        return off

    def img(im, dither=True):
        return add_blob(image_to_bgr555(im, dither))

    digits_off = img(render_digits(fonts), False)
    cw_off = img(render_label("Continue Watching", fonts), False)
    notouch_off = img(render_hints("notouch", fonts), False)
    locked_off = img(render_hints("locked", fonts), False)
    shuf_tile_off = img(render_shuffle_tile(fonts))
    shuf_hero_off = img(render_shuffle_hero(fonts))
    shuf_label_off = img(render_label("Can't decide?", fonts), False)

    item_index, item_recs = {}, []

    def item(e):
        if e["id"] not in item_index and e.get("kind", 0) != G.KIND_VIDEO:
            th = img(G.render_game_tile(e["rom"], e["art"], fonts, TILE_W, TILE_H, shadow_text, ellipsize))
            he = img(G.render_game_hero(e["rom"], e["art"], fonts, SCREEN_W, SCREEN_H, RED, GREY,
                                        shadow_text, ellipsize, wrap))
            po = add_blob(e["ds_path"].encode("utf-8") + b"\0")
            item_index[e["id"]] = len(item_recs)
            item_recs.append((e["id"].encode(), th, he, None, 0, 0, 0, 0, po, e["kind"], e["save_size"]))
        if e["id"] not in item_index:
            t = e["tech"]
            where = e["meta_prefix"] if "meta_prefix" in e else e["category"]
            meta = "  ·  ".join(x for x in (e["year"], fmt_duration(t["duration"]), where) if x)
            th = img(render_tile(e["art"], e.get("tile_title", e["title"]), fonts))
            he = img(render_hero(e["art"], e["title"], meta, e["desc"], fonts))
            pa = img(render_panel(e["art"], e["title"], meta, e["desc"], fonts))
            item_index[e["id"]] = len(item_recs)
            item_recs.append((e["id"].encode(), th, he, pa, t["frames"], t["fps_num"], t["fps_den"],
                              int(round(t["duration"])), None, G.KIND_VIDEO, 0))
        return item_index[e["id"]]

    page_recs, row_recs, entry_vals = [], [], []
    for pg in pages:
        hdr = img(render_header(fonts, pg["title"]), False)
        tile = hero = 0
        if pg["title"] is not None:
            n = pg["n_eps"]
            badge = f"{n} EP" if n < 1000 else "999+"
            seasons = f"{pg['n_seasons']} seasons" if pg["n_seasons"] > 1 else ""
            meta = "  ·  ".join(x for x in (pg["year"], f"{n} episode{'s' * (n != 1)}", seasons) if x)
            tile = img(render_tile(pg["art"], pg["title"], fonts, badge))
            hero = img(render_hero(pg["art"], pg["title"], meta, pg["desc"], fonts))
        page_recs.append([hdr, tile, hero, len(row_recs), len(pg["rows"]), pg.get("n_eps", 0)])
        for name, ents in pg["rows"]:
            lab = img(render_label(name, fonts), False)
            row_recs.append((lab, len(entry_vals), len(ents)))
            for kind, v in ents:
                entry_vals.append(item(v) if kind == "item" else ENTRY_PAGE | v)
    if len(item_recs) >= ENTRY_PAGE or len(pages) >= ENTRY_PAGE or len(row_recs) > 0xFFFF:
        sys.exit("library too large for one catalog")

    off_pages = CAT_HEADER.size
    off_rows = off_pages + CAT_PAGE.size * len(page_recs)
    off_entries = off_rows + CAT_ROW.size * len(row_recs)
    off_items = (off_entries + 2 * len(entry_vals) + 3) & ~3
    base = off_items + CAT_ITEM.size * len(item_recs)
    out = bytearray(CAT_HEADER.pack(b"RCAT", CAT_VERSION, len(page_recs), len(row_recs), len(item_recs),
                                    off_pages, off_rows, off_entries, off_items,
                                    base + digits_off, base + cw_off, len(entry_vals),
                                    base + notouch_off, base + locked_off,
                                    base + shuf_tile_off, base + shuf_hero_off, base + shuf_label_off))
    for hdr, tile, hero, fr, nr, ne in page_recs:
        out += CAT_PAGE.pack(base + hdr, base + tile if tile else 0, base + hero if hero else 0,
                             fr, nr, min(ne, 0xFFFF), 0)
    for lab, first, cnt in row_recs:
        out += CAT_ROW.pack(base + lab, first, cnt)
    out += struct.pack(f"<{len(entry_vals)}H", *entry_vals)
    out += b"\0" * (off_items - len(out))
    for rid, th, he, pa, fr, fn, fd, dur, po, kind, sav in item_recs:
        out += CAT_ITEM.pack(rid, base + th, base + he, base + pa if pa is not None else 0, fr, fn, fd, dur,
                             base + po if po is not None else 0, kind, sav)
    out += blobs
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(out)
    os.replace(tmp, path)


# --------------------------------------------------------------------------------------
# inspection / preview
# --------------------------------------------------------------------------------------
def read_rdv(path):
    with open(path, "rb") as f:
        hdr = RDV_HEADER.unpack(f.read(RDV_HEADER.size))
        (magic, ver, w, h, flags, fn, fd, frames, rate, samples, kfc, kfo) = hdr[:12]
        if magic != b"RDV1":
            raise ValueError("not an .rdv file")
        return {"w": w, "h": h, "fps_num": fn, "fps_den": fd, "frames": frames, "rate": rate,
                "samples": samples, "kf_count": kfc, "kf_offset": kfo}


def iter_decoded(path):
    """Yields (frame_index, rgb (H,256,3) uint8) exactly as the DSi would display them."""
    info = read_rdv(path)
    H = info["h"]
    bw, bh = SCREEN_W // 4, H // 4
    blocks = np.zeros((bw * bh, 16), np.uint8)
    pal = np.zeros((256, 3), np.uint8)
    with open(path, "rb") as f:
        f.seek(RDV_HEADER.size)
        for i in range(info["frames"]):
            (size,) = struct.unpack("<I", f.read(4))
            data = f.read(size)
            p = 4
            if data[0] & FRAME_PAL:
                v = np.frombuffer(data, "<u2", 256, 4).astype(np.int32)
                pal = from5(np.stack([v & 31, (v >> 5) & 31, (v >> 10) & 31], 1)).astype(np.uint8)
                p += 512
            blocks = decode_ops_py(data[p:], blocks, bw * bh)
            idx = blocks.reshape(bh, bw, 4, 4).transpose(0, 2, 1, 3).reshape(H, SCREEN_W)
            yield i, pal[idx], idx


def cmd_info(a):
    info = read_rdv(a.file)
    size = os.path.getsize(a.file)
    secs = info["frames"] * info["fps_den"] / info["fps_num"]
    print(json.dumps(info, indent=1))
    print(f"duration {secs:.1f}s, {size / max(secs, 1e-3) / 1024:.0f} KB/s video")


def cmd_preview(a):
    need_tools()
    info = read_rdv(a.file)
    H = info["h"]
    yoff = (SCREEN_H - H) // 2
    rda = Path(a.file).with_suffix(".rda")
    cmd = [ffmpeg_exe(), "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s",
           f"{SCREEN_W}x{SCREEN_H}", "-r", f"{info['fps_num']}/{info['fps_den']}", "-i", "-"]
    if rda.exists():
        cmd += ["-f", "s16le", "-ar", str(info["rate"]), "-ac", "1", "-i", str(rda)]
    vcodec = ["-c:v", "libx264", "-crf", "16"] if "libx264" in ff_caps() else ["-c:v", "mpeg4", "-q:v", "2"]
    cmd += ["-vf", "scale=768:576:flags=neighbor", *vcodec, "-pix_fmt", "yuv420p", "-shortest", a.out]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    screen = np.zeros((SCREEN_H, SCREEN_W, 3), np.uint8)
    for i, rgb, _ in iter_decoded(a.file):
        if a.max_frames and i >= a.max_frames:
            break
        screen[yoff:yoff + H] = rgb
        proc.stdin.write(screen.tobytes())
    proc.stdin.close()
    proc.wait()
    print(f"wrote {a.out}")


def load_catalog(path):
    d = Path(path).read_bytes()
    h = CAT_HEADER.unpack_from(d, 0)
    _, ver, npg, nr, ni, opg, orow, oent, oit, odg, ocw, nent = h[:12]
    if ver != CAT_VERSION:
        raise ValueError(f"catalog version {ver}, expected {CAT_VERSION}")

    def img(off):
        w, hh = struct.unpack_from("<HH", d, off)
        px = np.frombuffer(d, "<u2", w * hh, off + 4).reshape(hh, w).astype(np.int32)
        return from5(np.stack([px & 31, (px >> 5) & 31, (px >> 10) & 31], -1)).astype(np.uint8)
    pages = [CAT_PAGE.unpack_from(d, opg + CAT_PAGE.size * i) for i in range(npg)]
    rows = [CAT_ROW.unpack_from(d, orow + CAT_ROW.size * r) for r in range(nr)]
    ents = struct.unpack_from(f"<{nent}H", d, oent)
    items = [CAT_ITEM.unpack_from(d, oit + CAT_ITEM.size * i) for i in range(ni)]
    return {"img": img, "pages": pages, "rows": rows, "entries": ents, "items": items, "digits": odg,
            "shuffle": h[14:17]}


def cmd_mock(a):
    """Render what the DSi browser shows (mirrors source/browser.c) -- handy for checking art."""
    render_mock(a.sd, a.page, a.row, a.col, a.scale).save(a.out)
    print(f"wrote {a.out}")


def render_mock(sd, page=0, row=0, col=0, scale=2) -> Image.Image:
    c = load_catalog(Path(sd) / "reelds" / "catalog.bin")
    img = c["img"]
    pg = c["pages"][page]
    bot = np.zeros((SCREEN_H, SCREEN_W, 3), np.uint8)
    bot[:] = from5(to5(BG_RGB))

    def blit(dst, x, y, src, cy0=0, cy1=SCREEN_H):
        h, w = src.shape[:2]
        for r in range(h):
            yy = y + r
            if cy0 <= yy < cy1:
                x0, x1 = max(0, x), min(SCREEN_W, x + w)
                if x1 > x0:
                    dst[yy, x0:x1] = src[r, x0 - x:x1 - x]

    SHUF = 0xFFFF

    def tile_of(v):
        if v == SHUF:
            return img(c["shuffle"][0])
        return img(c["pages"][v & 0x7FFF][1] if v & ENTRY_PAGE else c["items"][v][1])

    def hero_of(v):
        if v == SHUF:
            return img(c["shuffle"][1])
        return img(c["pages"][v & 0x7FFF][2] if v & ENTRY_PAGE else c["items"][v][2])

    shuffle_on = read_settings(sd)["shuffle_tile"] and c["shuffle"][0]

    blit(bot, 0, 0, img(pg[0]))
    rows = [(lab, list(c["entries"][first:first + cnt]))
            for lab, first, cnt in c["rows"][pg[3]:pg[3] + pg[4]]]
    if shuffle_on and rows:
        rows.insert(0, (c["shuffle"][2], [SHUF]))
    sel = None
    top = max(0, min(row, len(rows) - 3))          # scrolls like the DSi does
    for r, (lab, ents) in enumerate(rows):
        if r < top or r >= top + 3:
            continue
        y = HEADER_H + (r - top) * 57
        blit(bot, 8, y, img(lab), HEADER_H)
        for k, v in enumerate(ents):
            x = 8 + k * 78
            if x >= SCREEN_W:
                break
            blit(bot, x, y + 13, tile_of(v), HEADER_H)
            if r == row and k == col:
                sel = v
                ty = y + 13
                bot[max(ty - 2, 0):ty + 42, max(x - 2, 0):x + 74][[0, 1, -2, -1], :] = 255
                bot[max(ty - 2, 0):ty + 42, max(x - 2, 0):x + 74][:, [0, 1, -2, -1]] = 255
    if sel is None:
        sel = rows[0][1][0]
    gap = 24
    sheet = np.zeros((SCREEN_H * 2 + gap, SCREEN_W, 3), np.uint8)
    sheet[:SCREEN_H] = hero_of(sel)
    sheet[SCREEN_H + gap:] = bot
    return Image.fromarray(sheet).resize((SCREEN_W * scale, sheet.shape[0] * scale), Image.NEAREST)


def cmd_selftest(a):
    """Encode a generated clip end to end and decode it again. Proves the toolchain works."""
    import platform
    import tempfile
    ff = ffmpeg_exe()
    print(f"platform : {platform.system()} {platform.machine()}  (Python {platform.python_version()})")
    print(f"ffmpeg   : {ff}")
    print(f"numpy {np.__version__}, Pillow {Image.__version__}")
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        (td / "lib" / "Test").mkdir(parents=True)
        clip = td / "lib" / "Test" / "Self.Test.2024.mp4"
        r = subprocess.run([ff, "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=640x360:r=24",
                            "-f", "lavfi", "-i", "sine=f=440:r=44100", "-t", "3", "-pix_fmt", "yuv420p",
                            str(clip)], capture_output=True, text=True)
        if r.returncode != 0:
            sys.exit("selftest: ffmpeg could not create a test clip: " + r.stderr[:300])
        ns = argparse.Namespace(library=[str(td / "lib")], out=str(td / "sd"), cache=str(td / "cache"),
                                quality="normal", fps=30, max_rate=600, audio_rate=32768, dither=6.0,
                                keyint=5.0, jobs=1, font=None, font_bold=None, no_audio_boost=False,
                                no_deinterlace=False, force=False, prune=False, autoplay=None,
                                lock_buttons=None, lock_touch=None, shuffle=None, games=None,
                                show_games=None, row_order=None)
        cmd_build(ns)
        vids = list((td / "sd" / "reelds" / "videos").glob("*.rdv"))
        n = sum(1 for _ in iter_decoded(vids[0]))
        assert n == read_rdv(vids[0])["frames"] and n > 60, "frame count mismatch"
        assert (td / "sd" / "reelds" / "catalog.bin").stat().st_size > 1000
    print("selftest OK")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd")

    b = sub.add_parser("build", help="encode a library and write the SD card layout")
    b.add_argument("library", nargs="*", help="video folders (or files); sub-folders become rows")
    b.add_argument("--games", nargs="+", metavar="FOLDER",
                   help="folders of .nds games/homebrew (on the card, or copied to reelds/games/)")
    b.add_argument("--out", required=True, help="SD card root (creates <out>/reelds/)")
    b.add_argument("--quality", choices=QUALITY, default="normal")
    b.add_argument("--fps", type=float, default=30, help="frame-rate cap (default 30)")
    b.add_argument("--audio-rate", type=int, choices=(16384, 32768), default=32768,
                   help="PCM rate; both divide the DS sound clock exactly (default 32768)")
    b.add_argument("--dither", type=float, default=6.0, help="ordered dither strength, 0 = off")
    b.add_argument("--max-rate", type=int, default=600,
                   help="soft cap on video data rate in KB/s (default 600)")
    b.add_argument("--keyint", type=float, default=5.0, help="max seconds between keyframes")
    b.add_argument("--jobs", type=int, default=max(1, (os.cpu_count() or 2) // 2))
    b.add_argument("--cache", help="build cache dir (default ~/.cache/reelds)")
    b.add_argument("--font", help="TTF for regular text")
    b.add_argument("--font-bold", help="TTF for bold text")
    b.add_argument("--no-audio-boost", action="store_true", help="skip loudness levelling")
    b.add_argument("--no-deinterlace", action="store_true")
    b.add_argument("--force", action="store_true", help="re-encode everything")
    b.add_argument("--prune", action="store_true", help="delete encoded videos no longer in the library")
    b.add_argument("--autoplay", choices=("on", "off"), help="play the next episode automatically (default on)")
    b.add_argument("--lock-buttons", choices=("on", "off"), help="only A works during playback")
    b.add_argument("--lock-touch", choices=("on", "off"), help="touchscreen ignored during playback")
    b.add_argument("--shuffle", choices=("on", "off"), help="show the Shuffle tile (default on)")
    b.add_argument("--show-games", choices=("on", "off"), help="show the Games row(s) (default on)")
    b.add_argument("--row-order", nargs="+", metavar="ROW",
                   help="home-page rows to put first, in this order (others follow A-Z)")
    b.set_defaults(func=cmd_build)

    p = sub.add_parser("preview", help="render an .rdv to mp4 exactly as the DSi decodes it")
    p.add_argument("file")
    p.add_argument("out")
    p.add_argument("--max-frames", type=int, default=0)
    p.set_defaults(func=cmd_preview)

    i = sub.add_parser("info", help="print .rdv header")
    i.add_argument("file")
    i.set_defaults(func=cmd_info)

    mk = sub.add_parser("mock", help="render a PNG of the DSi browser screens from catalog.bin")
    mk.add_argument("sd", help="SD card root used with build --out")
    mk.add_argument("out")
    mk.add_argument("--page", type=int, default=0, help="0 = home, 1.. = shows")
    mk.add_argument("--row", type=int, default=0)
    mk.add_argument("--col", type=int, default=0)
    mk.add_argument("--scale", type=int, default=2)
    mk.set_defaults(func=cmd_mock)

    rw = sub.add_parser("rows", help="list the home-page rows a build of these folders would have")
    rw.add_argument("library", nargs="*")
    rw.add_argument("--games", nargs="+", metavar="FOLDER")
    rw.set_defaults(func=cmd_rows)

    g = sub.add_parser("gui", help="open the desktop app (default when run with no arguments)")
    g.set_defaults(func=lambda _a: __import__("gui").main())

    t = sub.add_parser("selftest", help="encode + decode a generated clip to check the setup")
    t.set_defaults(func=cmd_selftest)

    a = ap.parse_args()
    if a.cmd is None:           # double-clicked / no arguments: open the GUI
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import gui
        gui.main()
        return
    a.func(a)


if __name__ == "__main__":
    main()

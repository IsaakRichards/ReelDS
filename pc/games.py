"""
Games support for the ReelDS builder: everything about .nds files is worked out here on
the PC (title, publisher, icon, box art, homebrew vs retail, save size) so the DSi only has
to blit a tile and hand the file to a loader.

  * Retail DS games are started through nds-bootstrap (installed with TWiLight Menu++),
    with QUIT_PATH pointing back at ReelDS.
  * Homebrew (.nds built with devkitARM/BlocksDS) is booted directly.

Save files use TWiLight Menu++'s default location (<rom folder>/saves/<rom name>.sav), so
saves are shared with TWiLight. Sizes come from savesizes.json, generated from the ROM list
TWiLight Menu++ uses (originally from melonDS, GPL-3.0); unknown games get 512 KB.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import struct
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

GAME_EXTS = {".nds", ".dsi"}
KIND_VIDEO, KIND_RETAIL, KIND_HOMEBREW = 0, 1, 2
DEFAULT_SAVE = 512 * 1024

_SAVE_SIZES = None


def save_sizes() -> dict:
    global _SAVE_SIZES
    if _SAVE_SIZES is None:
        p = Path(__file__).with_name("savesizes.json")
        _SAVE_SIZES = json.loads(p.read_text()) if p.exists() else {}
    return _SAVE_SIZES


def save_size_for(code: str) -> int:
    return save_sizes().get(code, DEFAULT_SAVE)


# --------------------------------------------------------------------------------------
# ROM header + banner
# --------------------------------------------------------------------------------------
def _decode_icon(banner: bytes):
    """32x32 4bpp tiled icon + 16-colour BGR555 palette -> RGBA image (index 0 transparent)."""
    if len(banner) < 0x240:
        return None
    pix = np.frombuffer(banner, np.uint8, 0x200, 0x20)
    idx = np.empty(1024, np.uint8)
    idx[0::2] = pix & 15
    idx[1::2] = pix >> 4
    # 4x4 tiles of 8x8 pixels, row-major inside each tile
    idx = idx.reshape(4, 4, 8, 8).transpose(0, 2, 1, 3).reshape(32, 32)
    pal = np.frombuffer(banner, "<u2", 16, 0x220).astype(np.int32)
    rgb = np.stack([pal & 31, (pal >> 5) & 31, (pal >> 10) & 31], 1)
    rgb = ((rgb << 3) | (rgb >> 2)).astype(np.uint8)
    rgba = np.zeros((32, 32, 4), np.uint8)
    rgba[..., :3] = rgb[idx]
    rgba[..., 3] = np.where(idx == 0, 0, 255)
    return Image.fromarray(rgba, "RGBA")


def _is_homebrew(f, hdr: bytes, code: str) -> bool:
    """Same test TWiLight Menu++ uses: retail games start with Nintendo SDK boot code."""
    arm9_rom, arm9_exec, arm9_dest = struct.unpack_from("<3I", hdr, 0x20)   # offset, entry, load addr
    if code.startswith("HNA"):          # modcrypted retail
        return False
    entry = (0x02000800 if code.startswith("BIG") else arm9_exec)
    try:
        f.seek(arm9_rom + entry - arm9_dest)
        sig = struct.unpack("<4I", f.read(16))
    except (OSError, struct.error, ValueError):
        return True
    if (sig[0] == 0xE3A0C301 or 0xEA000000 <= sig[0] < 0xEC000000) and sig[1] == 0xE58CC208:
        if 0xEB000000 <= sig[2] < 0xEC000000 and 0xE3A00000 <= sig[3] < 0xE3A01000:
            return False                # SDK 2 / TWL SDK 5
        if sig[2] == 0xE1DC00B6 and sig[3] == 0xE3500000:
            return False                # SDK 3-5
        if sig[2] == 0xEAFFFFFF and sig[3] == 0xE1DC00B6:
            return False                # SDK 4
    return True


def read_rom(path: Path) -> dict:
    with open(path, "rb") as f:
        hdr = f.read(0x200)
        if len(hdr) < 0x160:
            raise ValueError("not a DS ROM (too small)")
        code = hdr[0x0C:0x10].decode("latin-1")
        unit = hdr[0x12]
        banner_off = struct.unpack_from("<I", hdr, 0x68)[0]
        banner = b""
        if 0x200 <= banner_off < path.stat().st_size:
            f.seek(banner_off)
            banner = f.read(0x1240)
        homebrew = _is_homebrew(f, hdr, code)
    title_lines = []
    if len(banner) >= 0x440:
        raw = banner[0x340:0x440].decode("utf-16-le", "replace").split("\0", 1)[0]   # English
        title_lines = [ln.strip() for ln in raw.replace("\r", "").split("\n") if ln.strip()]
    if not title_lines:
        internal = hdr[0:12].decode("latin-1").strip("\0 ").title()
        title_lines = [internal or path.stem]
    # banner title: "Name", optional "Subtitle", then "Publisher"
    name = title_lines[0]
    publisher = title_lines[-1] if len(title_lines) >= 2 else ""
    subtitle = " ".join(title_lines[1:-1])
    return {"code": code, "unit": unit, "homebrew": homebrew, "name": name, "subtitle": subtitle,
            "publisher": publisher, "icon": _decode_icon(banner) if banner else None,
            "dsiware": unit == 3 and not homebrew}


def find_boxart(rom: Path, sd_root: Path, code: str):
    """Sidecar image next to the ROM, else TWiLight Menu++'s boxart folder on the card."""
    for ext in (".png", ".jpg", ".jpeg", ".webp", ".bmp"):
        p = rom.with_suffix(ext)
        if p.exists():
            return p
    box = sd_root / "_nds" / "TWiLightMenu" / "boxart"
    if box.is_dir():
        for stem in (rom.name, code):
            for ext in (".png", ".bmp", ".jpg"):
                p = box / (stem + ext)
                if p.exists():
                    return p
    return None


# --------------------------------------------------------------------------------------
# where the ROM lives on the SD card
# --------------------------------------------------------------------------------------
def place_on_card(rom: Path, root: Path, sd_root: Path) -> Path:
    """ROMs already on the card are used where they are; others are copied to
    <card>/reelds/games/... (only when new or changed)."""
    try:
        rom.resolve().relative_to(sd_root.resolve())
        return rom.resolve()
    except ValueError:
        pass
    dest = sd_root / "reelds" / "games" / root.name / rom.relative_to(root)
    st = rom.stat()
    if not dest.exists() or dest.stat().st_size != st.st_size or dest.stat().st_mtime < st.st_mtime:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(rom, dest)
    return dest


def ds_path(on_card: Path, sd_root: Path) -> str:
    rel = on_card.resolve().relative_to(sd_root.resolve()).as_posix()
    return "sd:/" + rel


def save_path(on_card: Path) -> Path:
    return on_card.parent / "saves" / (on_card.stem + ".sav")


def ensure_save(on_card: Path, size: int) -> bool:
    """Create (or grow) the save file, filled with 0xFF like blank save memory."""
    if size <= 0:
        return False
    p = save_path(on_card)
    have = p.stat().st_size if p.exists() else 0
    if have >= size:
        return False
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "ab") as f:
        left = size - have
        chunk = b"\xff" * 65536
        while left:
            n = min(left, len(chunk))
            f.write(chunk[:n])
            left -= n
    return True


def game_id(ds: str) -> str:
    return hashlib.sha1(("game:" + ds.lower()).encode()).hexdigest()[:8]


# --------------------------------------------------------------------------------------
# artwork
# --------------------------------------------------------------------------------------
def _icon_colours(icon):
    a = np.asarray(icon)
    opaque = a[a[..., 3] > 0][:, :3].astype(np.float32)
    if not len(opaque):
        return (60, 60, 70), (20, 20, 20)
    c = opaque.mean(0)
    hi = tuple(int(v) for v in np.clip(c * 0.75 + 20, 0, 255))
    lo = tuple(int(v) for v in np.clip(c * 0.18, 0, 255))
    return hi, lo


def _gradient(w, h, top, bottom):
    t = np.linspace(0, 1, h, dtype=np.float32)[:, None, None]
    a = np.array(top, np.float32) * (1 - t) + np.array(bottom, np.float32) * t
    return Image.fromarray(np.repeat(a, w, 1).astype(np.uint8))


def _backdrop(w, h, art, icon):
    if art is not None:
        bg = art.copy()
        sc = max(w / bg.width, h / bg.height)
        bg = bg.resize((max(w, round(bg.width * sc)), max(h, round(bg.height * sc))), Image.LANCZOS)
        x, y = (bg.width - w) // 2, (bg.height - h) // 2
        bg = bg.crop((x, y, x + w, y + h)).filter(ImageFilter.GaussianBlur(6))
        return Image.blend(bg, Image.new("RGB", (w, h), (0, 0, 0)), 0.55)
    hi, lo = _icon_colours(icon) if icon is not None else ((60, 60, 70), (20, 20, 20))
    return _gradient(w, h, hi, lo)


def _contain(im, w, h):
    sc = min(w / im.width, h / im.height)
    return im.resize((max(1, round(im.width * sc)), max(1, round(im.height * sc))), Image.LANCZOS)


def _paste_icon(dst, icon, x, y, scale):
    big = icon.resize((32 * scale, 32 * scale), Image.NEAREST)
    dst.paste(big, (x, y), big)


def render_game_tile(g, art, fonts, tile_w, tile_h, shadow_text, ellipsize):
    icon = g["icon"]
    im = _backdrop(tile_w, tile_h, art, icon)
    if art is not None:
        fg = _contain(art, tile_w - 4, tile_h - 4)
        im.paste(fg, ((tile_w - fg.width) // 2, (tile_h - fg.height) // 2))
    elif icon is not None:
        _paste_icon(im, icon, (tile_w - 32) // 2, 1, 1)
    d = ImageDraw.Draw(im)
    f = fonts.get(8, True)
    strip = Image.new("RGB", (tile_w, 11), (0, 0, 0))
    im.paste(Image.blend(im.crop((0, tile_h - 11, tile_w, tile_h)), strip, 0.6), (0, tile_h - 11))
    shadow_text(d, (3, tile_h - 11), ellipsize(g["name"], f, tile_w - 6), f, (255, 255, 255))
    return im


def render_game_hero(g, art, fonts, sw, sh, red, grey, shadow_text, ellipsize, wrap):
    icon = g["icon"]
    im = _backdrop(sw, sh, art, icon)
    d = ImageDraw.Draw(im)
    shadow_text(d, (8, 5), "REELDS", fonts.get(10, True), red)
    # artwork on the left
    box_w, box_h = 112, 112
    if art is not None:
        fg = _contain(art, box_w, box_h)
        x, y = 8 + (box_w - fg.width) // 2, 34 + (box_h - fg.height) // 2
        d.rectangle((x - 1, y - 1, x + fg.width, y + fg.height), outline=(0, 0, 0))
        im.paste(fg, (x, y))
    elif icon is not None:
        _paste_icon(im, icon, 16, 42, 3)
    # text on the right
    x0, tw = 128, sw - 128 - 8
    ft = fonts.get(13, True)
    y = 36
    for ln in wrap(g["name"], ft, tw, 3):
        shadow_text(d, (x0, y), ln, ft, (255, 255, 255))
        y += 16
    fm = fonts.get(10)
    if g.get("subtitle"):
        for ln in wrap(g["subtitle"], fm, tw, 2):
            d.text((x0, y + 1), ln, font=fm, fill=(215, 215, 215))
            y += 12
    if g["publisher"]:
        d.text((x0, y + 2), ellipsize(g["publisher"], fm, tw), font=fm, fill=grey)
        y += 14
    kind = "Homebrew" if g["homebrew"] else "Nintendo DS game"
    d.text((x0, y + 4), kind, font=fm, fill=(150, 150, 150))
    # how to play / how to get back
    fb = fonts.get(12, True)
    d.rounded_rectangle((x0, 150, x0 + 62, 168), 4, fill=red)
    d.text((x0 + 8, 152), "A  Play", font=fb, fill=(255, 255, 255))
    if not g["homebrew"]:
        fh = fonts.get(8)
        for i, ln in enumerate(wrap("To come back: press L + Down + SELECT during the game, then pick Quit.",
                                    fh, sw - 16, 2)):
            d.text((8, sh - 22 + i * 10), ln, font=fh, fill=(175, 175, 175))
    return im

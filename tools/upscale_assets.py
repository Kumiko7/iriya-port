"""Build the upscaled ("高画質") art set from your own ROMs with an ESRGAN model.

Run it with a Python that has torch (CUDA), spandrel, numpy and Pillow, for
example the venv of a Stable Diffusion WebUI install:

    <webui>/venv/Scripts/python.exe tools/upscale_assets.py --model <webui>/models/ESRGAN/4x-UltraSharp.pth

It decodes every background, event picture, face sheet, card picture and title
screen in the two ROMs, upscales it 4x and writes WebP (quality 90) files to
data/upscaled/<game>/<path>.webp, already turned the way the
port shows them (backgrounds upright, face sheets as stored). Nothing is uploaded or bundled;
the files stay on your machine. Re-running skips files that already exist
(use --force to redo them). The port picks them up under オプション → 画質.
"""
import argparse
import json
import os
import struct
import sys
import time

import numpy as np
from PIL import Image, ImageFilter

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
from iriya_engine.rom import NDSRom, find_roms   # noqa: E402  (no pygame needed)
from iriya_engine import nnt                     # noqa: E402
from iriya_engine import paths                   # noqa: E402

FOLDERS = ('bg/', 'win/', 'card/', 'title/')
SKIP_PREFIX = ('title/name',)                    # chapter name plates are pixel text
SCALE = 4
TILE, PAD = 320, 24
CELL_PAD = 8                                     # source pixels around each face cell


def out_root():
    return paths.data_path('upscaled')


def fill_transparent(rgb, alpha):
    """Spread colours into transparent pixels so the model sees no dark fringe."""
    rgb = rgb.astype(np.float32)
    known = alpha > 0
    if known.all() or not known.any():
        return rgb
    acc = np.where(known[..., None], rgb, 0)
    w = known.astype(np.float32)
    for _ in range(12):
        s = np.zeros_like(acc)
        n = np.zeros_like(w)
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                s += np.roll(np.roll(acc, dy, 0), dx, 1)
                n += np.roll(np.roll(w, dy, 0), dx, 1)
        grow = (w == 0) & (n > 0)
        acc[grow] = s[grow] / n[grow, None]
        w[grow] = 1
        if (w > 0).all():
            break
    mean = rgb[known].mean(0)
    acc[w == 0] = mean
    return acc


def upscale_rgb(model, torch, rgb):
    """rgb float HxWx3 0..255 -> uint8 (4H)x(4W)x3, in overlapping tiles."""
    h, w, _ = rgb.shape
    x = torch.from_numpy(np.ascontiguousarray(rgb.transpose(2, 0, 1)) / 255.0).half().cuda()[None]
    out = np.zeros((h * SCALE, w * SCALE, 3), np.float32)
    for y0 in range(0, h, TILE):
        for x0 in range(0, w, TILE):
            y1, x1 = min(h, y0 + TILE), min(w, x0 + TILE)
            ty0, tx0 = max(0, y0 - PAD), max(0, x0 - PAD)
            ty1, tx1 = min(h, y1 + PAD), min(w, x1 + PAD)
            with torch.no_grad():
                t = model(x[:, :, ty0:ty1, tx0:tx1]).float().clamp(0, 1)[0].permute(1, 2, 0).cpu().numpy()
            oy, ox = (y0 - ty0) * SCALE, (x0 - tx0) * SCALE
            out[y0 * SCALE:y1 * SCALE, x0 * SCALE:x1 * SCALE] = \
                t[oy:oy + (y1 - y0) * SCALE, ox:ox + (x1 - x0) * SCALE]
    return (out * 255 + 0.5).astype(np.uint8)


def upscale_rgba(model, torch, rgba):
    """-> (rgb, alpha or None for opaque art), both 4x."""
    alpha = rgba[..., 3]
    rgb = upscale_rgb(model, torch, fill_transparent(rgba[..., :3], alpha))
    if alpha.min() == 255:
        return rgb, None
    # the mask goes through the model too, so edges come out smooth instead of
    # an enlarged 1-bit staircase
    a3 = np.repeat(alpha[..., None].astype(np.float32), 3, axis=2)
    a = upscale_rgb(model, torch, a3).mean(axis=2)
    # the cut-outs have 1-pixel steps along slanted edges that the model
    # keeps; a light blur turns them into a straight slant, and the contrast
    # curve sharpens the edge again
    a = np.asarray(Image.fromarray(a.astype(np.uint8)).filter(ImageFilter.GaussianBlur(3)), np.float32)
    return rgb, np.clip((a - 128) * 2.0 + 128, 0, 255).astype(np.uint8)


def face_cells(rom, p, shape):
    """Frame rectangles (x, y, w, h) of a face sheet, from its .buf file."""
    b = p[:-4] + '.buf'
    if b not in rom.files:
        return None
    d = rom.read(b)
    n = struct.unpack_from('<H', d, 0)[0]
    offs = struct.unpack_from('<%dH' % (n + 1), d, 2)
    cells = []
    for i in range(1, n + 1):
        if offs[i] + 0x48 > len(d):
            break
        x, y, w, h = struct.unpack_from('<4H', d, offs[i] + 0x40)
        w, h = min(w, shape[1] - x), min(h, shape[0] - y)
        if w > 0 and h > 0:
            cells.append((x, y, w, h))
    return cells or None


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--model', required=True, help='ESRGAN .pth (a 4x model, e.g. 4x-UltraSharp)')
    ap.add_argument('--roms', default=None, help='folder with the .nds files (default: data/roms)')
    ap.add_argument('--out', default=out_root())
    ap.add_argument('--quality', type=int, default=90)
    ap.add_argument('--force', action='store_true', help='redo files that already exist')
    ap.add_argument('--only', default='', help='only ROM paths starting with this, e.g. win/')
    args = ap.parse_args()

    import torch
    import spandrel

    roms = find_roms([args.roms] if args.roms else paths.search_dirs())
    if not roms:
        sys.exit('No ROMs found in ' + (args.roms or 'data/roms'))
    md = spandrel.ModelLoader().load_from_file(args.model)
    if md.scale != SCALE:
        sys.exit(f'{args.model} is a {md.scale}x model; a 4x model is needed')
    model = md.model.cuda().eval().half()
    jobs = []
    for gid, path in sorted(roms.items()):
        rom = NDSRom(path)
        for p in sorted(rom.files):
            if p.endswith('.nnt') and p.startswith(FOLDERS) and not p.startswith(SKIP_PREFIX) \
                    and p.startswith(args.only):
                jobs.append((gid, rom, p))
    try:
        with open(os.path.join(args.out, 'manifest.json'), encoding='utf-8') as f:
            old_upright = bool(json.load(f).get('upright'))
    except (OSError, ValueError):
        old_upright = not os.path.isdir(args.out)
    print(f'{len(jobs)} images, model {os.path.basename(args.model)}, output {args.out}')
    t0 = time.time()
    done = skipped = failed = 0
    total_bytes = 0
    for k, (gid, rom, p) in enumerate(jobs):
        dst = os.path.join(args.out, gid, p[:-4] + '.webp')
        stale = not old_upright and not p.startswith('win/')   # an older set stored them unturned
        if os.path.exists(dst) and not args.force and not stale:
            skipped += 1
            total_bytes += os.path.getsize(dst)
            continue
        try:
            rgba, _ = nnt.decode(rom.read(p))
        except Exception as e:              # a format the port can't decode either
            failed += 1
            print('  skip', p, e)
            continue
        if not p.startswith('win/'):           # the port shows these turned upright
            rgba = np.ascontiguousarray(np.rot90(rgba, 1))
        cells = face_cells(rom, p, rgba.shape) if p.startswith('win/') else None
        if cells:
            # a face sheet packs one expression per cell: upscale each on its
            # own so neither the model nor the edge blur leaks across cells
            rgb = np.zeros((rgba.shape[0] * SCALE, rgba.shape[1] * SCALE, 3), np.uint8)
            a = np.zeros(rgb.shape[:2], np.uint8)
            for x, y, w, h in cells:
                cell = np.pad(rgba[y:y + h, x:x + w], ((CELL_PAD,) * 2, (CELL_PAD,) * 2, (0, 0)), mode='edge')
                c_rgb, c_a = upscale_rgba(model, torch, cell)
                q, X, Y, W, H = CELL_PAD * SCALE, x * SCALE, y * SCALE, w * SCALE, h * SCALE
                rgb[Y:Y + H, X:X + W] = c_rgb[q:q + H, q:q + W]
                a[Y:Y + H, X:X + W] = 255 if c_a is None else c_a[q:q + H, q:q + W]
        else:
            rgb, a = upscale_rgba(model, torch, rgba)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        if a is None or a.min() == 255:
            img = Image.fromarray(rgb, 'RGB')
        else:
            img = Image.fromarray(np.dstack([rgb, a]), 'RGBA')
        img.save(dst, quality=args.quality, method=4, alpha_quality=100)
        total_bytes += os.path.getsize(dst)
        done += 1
        if done % 25 == 0 or k == len(jobs) - 1:
            el = time.time() - t0
            print(f'  {k + 1}/{len(jobs)}  {el:.0f}s  {total_bytes / 1e6:.0f} MB', flush=True)
    on_disk = sum(n.endswith('.webp') for _, _, names in os.walk(args.out) for n in names)
    with open(os.path.join(args.out, 'manifest.json'), 'w', encoding='utf-8') as f:
        json.dump({'model': os.path.basename(args.model), 'scale': SCALE, 'quality': args.quality,
                   'upright': True,
                   'files': on_disk, 'games': sorted(roms)}, f, indent=1)
    print(f'done: {done} new, {skipped} kept, {failed} skipped, {total_bytes / 1e6:.0f} MB, '
          f'{time.time() - t0:.0f}s')


if __name__ == '__main__':
    main()

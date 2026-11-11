"""Decoder for the game's .nnt image format (4/8bpp paletted, BGR555).

Header (little endian):
  0x00 u32 file size     0x04 u32 kind (0=tiled BG with map, 9=linear bitmap, 8/15=8x8 tiled)
  0x08 u16 tex w, h      0x0C u16 log2 w, h      0x10 u16 visible w, h
  0x14 u32 pal off       0x18 u32 pal size       0x1C u32 pix off   0x20 u32 pix size
  0x24 u32 map off       0x28 u32 map size       (kind 0 only)
"""
import struct
import numpy as np


def palette(d, off, size):
    p = np.frombuffer(d, dtype='<u2', count=size // 2, offset=off).astype(np.uint32)
    r = (p & 31) * 255 // 31
    g = ((p >> 5) & 31) * 255 // 31
    b = ((p >> 10) & 31) * 255 // 31
    return np.stack([r, g, b], axis=1).astype(np.uint8)


def header(d):
    (size, fmt, tw, th, lw, lh, w, h, poff, psize, ioff, isize) = struct.unpack_from('<IIHHHHHHIIII', d, 0)
    return dict(size=size, fmt=fmt, tw=tw, th=th, w=w, h=h, poff=poff, psize=psize, ioff=ioff, isize=isize)


def _unpack4(raw):
    out = np.empty(raw.size * 2, dtype=np.uint8)
    out[0::2] = raw & 15
    out[1::2] = raw >> 4
    return out


def _untile(idx, tw, th):
    tiles = idx.reshape(th // 8, tw // 8, 8, 8)
    return tiles.transpose(0, 2, 1, 3).reshape(th, tw)


def _p2split(n, mx):
    out = []
    while n > 0:
        s = mx
        while s > n:
            s //= 2
        out.append(s)
        n -= s
    return out


def _build_obj(px, tw, th, mx, order):
    """Assemble an image from DS OBJ cells (each cell's 8x8 tiles stored row-major)."""
    ws, hs = _p2split(tw, mx), _p2split(th, mx)
    xs = np.cumsum([0] + ws[:-1])
    ys = np.cumsum([0] + hs[:-1])
    cells = [(x, y, w, h) for y, h in zip(ys, hs) for x, w in zip(xs, ws)]
    if order == 'col':
        cells.sort(key=lambda c: (c[0], c[1]))
    img = np.zeros((th, tw), np.uint8)
    p = 0
    for x, y, w, h in cells:
        n = w * h
        img[y:y + h, x:x + w] = px[p:p + n].reshape(h // 8, w // 8, 8, 8).transpose(0, 2, 1, 3).reshape(h, w)
        p += n
    return img


def _roughness(idx, pal):
    rgb = pal[np.minimum(idx, len(pal) - 1)].astype(np.int32)
    return int(np.abs(np.diff(rgb, axis=0)).sum() + np.abs(np.diff(rgb, axis=1)).sum())


def _best_obj(px, tw, th, pal):
    best = None
    for mx in (8, 16, 32, 64):
        for order in ('row', 'col'):
            img = _build_obj(px, tw, th, mx, order)
            sc = _roughness(img, pal)
            if best is None or sc < best[0]:
                best = (sc, img)
    return best[1]


def decode_indexed(d):
    """Return (index ndarray HxW, palette Nx3, header)."""
    hd = header(d)
    pal = palette(d, hd['poff'], hd['psize'])
    fmt, tw, th = hd['fmt'], hd['tw'], hd['th']
    raw = np.frombuffer(d, dtype=np.uint8, count=hd['isize'], offset=hd['ioff'])
    if fmt == 0:
        moff, msize = struct.unpack_from('<II', d, 0x24)
        m = np.frombuffer(d, dtype='<u2', count=msize // 2, offset=moff)
        bpp = 8 if hd['isize'] % 64 == 0 and (m & 0x3FF).max() < hd['isize'] // 64 and (m >> 12).max() == 0 else 4
        if bpp == 4:
            tiles = _unpack4(raw).reshape(-1, 8, 8)
        else:
            tiles = raw.reshape(-1, 8, 8)
        t = tiles[m & 0x3FF].copy()
        hf = ((m >> 10) & 1).astype(bool)
        vf = ((m >> 11) & 1).astype(bool)
        t[hf] = t[hf][:, :, ::-1]
        t[vf] = t[vf][:, ::-1, :]
        if bpp == 4:
            pb = ((m >> 12) * 16).astype(np.uint8)
            t = np.where(t == 0, 0, t + pb[:, None, None])
        idx = t.reshape(th // 8, tw // 8, 8, 8).transpose(0, 2, 1, 3).reshape(th, tw)
    else:
        bpp = hd['isize'] * 8 // (tw * th)
        px = _unpack4(raw) if bpp == 4 else raw
        px = px[:tw * th]
        if fmt == 9:
            idx = px.reshape(th, tw)
        else:
            idx = _best_obj(px, tw, th, pal)
    return idx[:hd['h'], :hd['w']], pal, hd


def decode(d):
    """Return (rgba ndarray HxWx4, header). Index 0 is transparent."""
    idx, pal, hd = decode_indexed(d)
    rgb = pal[np.minimum(idx, len(pal) - 1)]
    a = np.where(idx == 0, 0, 255).astype(np.uint8)
    return np.dstack([rgb, a]), hd

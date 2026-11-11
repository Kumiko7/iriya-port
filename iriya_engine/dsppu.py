"""DS 2D screens from emulated video memory (dsarm9.Arm9): text BGs (4bpp/8bpp, extended palettes),
1D-mapped sprites (4bpp/8bpp, extended palettes, flips), priorities and master brightness.
That covers what the Game II shooter uses; affine/bitmap BGs, windows and alpha blending are not
implemented. render(arm9) -> (top, bottom) as 192x256x3 uint8 arrays."""
import numpy as np

_v = np.arange(32768, dtype=np.uint32)
LUT = np.stack([(_v & 31) * 255 // 31, ((_v >> 5) & 31) * 255 // 31, ((_v >> 10) & 31) * 255 // 31], 1).astype(np.uint8)
OBJ_SIZE = {(0, 0): (8, 8), (0, 1): (16, 16), (0, 2): (32, 32), (0, 3): (64, 64), (1, 0): (16, 8), (1, 1): (32, 8),
            (1, 2): (32, 16), (1, 3): (64, 32), (2, 0): (8, 16), (2, 1): (8, 32), (2, 2): (16, 32), (2, 3): (32, 64)}
LCDC = {'E': 0x6880000, 'F': 0x6890000, 'G': 0x6894000, 'H': 0x6898000, 'I': 0x68a0000}
ROWS = np.arange(192)
COLS = np.arange(256)


def _u16(b):
    return np.frombuffer(b, '<u2')


def _tiles(raw, bpp8):
    """bytes -> (n, 8, 8) colour indices."""
    a = np.frombuffer(raw, np.uint8)
    if bpp8:
        return a[:len(a) // 64 * 64].reshape(-1, 8, 8)
    t = np.empty(len(a) * 2, np.uint8)
    t[0::2], t[1::2] = a & 15, a >> 4
    return t[:len(t) // 64 * 64].reshape(-1, 8, 8)


class _Engine:
    def __init__(self, m, sub):
        self.m, self.sub = m, sub
        self.io = 0x4001000 if sub else 0x4000000
        self.dispcnt = m.r32(self.io)
        pal = _u16(m.read(0x5000400 if sub else 0x5000000, 0x400))
        self.bgpal, self.objpal = pal[:256], pal[256:]
        cnt = [m.r8(0x4000240 + i) for i in range(10)]
        vram = dict(zip('ABCDEFG', cnt[:7]))
        vram['H'], vram['I'] = cnt[8], cnt[9]
        self.extbg = self.extobj = None
        for b in 'EFGHI':
            c = vram[b]
            if not c & 0x80:
                continue
            mst = c & 7
            if not sub and b in 'EFG' and mst == 4:
                self.extbg = LCDC[b]
            elif not sub and b in 'EFG' and mst == 5:
                self.extobj = LCDC[b]
            elif sub and b == 'H' and mst == 2:
                self.extbg = LCDC[b]
            elif sub and b == 'I' and mst == 3:
                self.extobj = LCDC[b]

    def bg(self, k):
        m, d = self.m, self.dispcnt
        if not (d >> (8 + k)) & 1:
            return None
        cnt = m.r16(self.io + 8 + 2 * k)
        hofs = m.r16(self.io + 0x10 + 4 * k) & 0x1ff
        vofs = m.r16(self.io + 0x12 + 4 * k) & 0x1ff
        vbase = 0x6200000 if self.sub else 0x6000000
        cbase = ((cnt >> 2) & 15) * 0x4000 + (0 if self.sub else ((d >> 24) & 7) * 0x10000)
        sbase = ((cnt >> 8) & 31) * 0x800 + (0 if self.sub else ((d >> 27) & 7) * 0x10000)
        bpp8 = (cnt >> 7) & 1
        bw, bh = [(1, 1), (2, 1), (1, 2), (2, 2)][cnt >> 14]
        ent = _u16(m.read(vbase + sbase, 0x800 * bw * bh)).reshape(bh, bw, 32, 32)
        ent = ent.transpose(0, 2, 1, 3).reshape(bh * 32, bw * 32)
        tiles = _tiles(m.read(vbase + cbase, 1024 * (64 if bpp8 else 32)), bpp8)
        # visible tile rows/cols, then pixels
        ys = (ROWS + vofs) % (bh * 256)
        xs = (COLS + hofs) % (bw * 256)
        e = ent[(ys >> 3)[:, None], (xs >> 3)[None, :]]
        t = np.minimum(e & 0x3ff, len(tiles) - 1)
        py = np.where(e & 0x800, 7 - (ys & 7)[:, None], (ys & 7)[:, None])
        px = np.where(e & 0x400, 7 - (xs & 7)[None, :], (xs & 7)[None, :])
        idx = tiles[t, py, px].astype(np.int32)
        p = (e >> 12).astype(np.int32)
        if bpp8:
            if (d >> 30) & 1 and self.extbg is not None:
                slot = k + (2 if k < 2 and (cnt >> 13) & 1 else 0)
                ext = _u16(m.read(self.extbg + slot * 0x2000, 0x2000))
                col = ext[p * 256 + idx]
            else:
                col = self.bgpal[idx]
        else:
            col = self.bgpal[p * 16 + idx]
        return cnt & 3, col, idx != 0

    def objs(self):
        m, d = self.m, self.dispcnt
        if not (d >> 12) & 1:
            return []
        oam = _u16(m.read(0x7000400 if self.sub else 0x7000000, 0x400)).reshape(128, 4)
        vram = np.frombuffer(m.read(0x6600000 if self.sub else 0x6400000, 0x40000), np.uint8)
        bound = 32 << ((d >> 20) & 3) if (d >> 4) & 1 else 32
        ext = _u16(m.read(self.extobj, 0x2000)) if (d >> 31) & 1 and self.extobj is not None else None
        out = []
        for a0, a1, a2, _ in oam.tolist():
            if (a0 >> 8) & 3 != 0 or a0 >> 14 == 3:            # hidden or affine (not used) / bad shape
                continue
            w, h = OBJ_SIZE[(a0 >> 14, a1 >> 14)]
            x, y = a1 & 0x1ff, a0 & 0xff
            x = x - 512 if x >= 256 else x
            y = y - 256 if y >= 192 else y
            if x >= 256 or y >= 192 or x + w <= 0 or y + h <= 0:
                continue
            bpp8 = (a0 >> 13) & 1
            base = (a2 & 0x3ff) * bound
            n = w * h if bpp8 else w * h // 2
            raw = vram[base:base + n]
            if len(raw) < n:
                continue
            if not bpp8:
                r = np.empty(w * h, np.uint8)
                r[0::2], r[1::2] = raw & 15, raw >> 4
                raw = r
            idx = raw.reshape(h // 8, w // 8, 8, 8).transpose(0, 2, 1, 3).reshape(h, w).astype(np.int32)
            if (a1 >> 12) & 1:
                idx = idx[:, ::-1]
            if (a1 >> 13) & 1:
                idx = idx[::-1]
            pal = a2 >> 12
            if bpp8:
                col = ext[pal * 256 + idx] if ext is not None else self.objpal[idx]
            else:
                col = self.objpal[pal * 16 + idx]
            out.append(((a2 >> 10) & 3, x, y, col, idx != 0))
        return out

    def render(self):
        img = np.full((192, 256), self.bgpal[0], np.uint16)
        layers = [(r[0], k, r[1], r[2]) for k in range(4) for r in [self.bg(k)] if r]
        objs = self.objs()
        for pr in (3, 2, 1, 0):
            for _p, _k, col, op in sorted((L for L in layers if L[0] == pr), key=lambda L: -L[1]):
                np.copyto(img, col.astype(np.uint16), where=op)
            for p, x, y, col, op in reversed(objs):          # lower OAM index on top
                if p != pr:
                    continue
                h, w = op.shape
                x0, y0, x1, y1 = max(0, x), max(0, y), min(256, x + w), min(192, y + h)
                sub = img[y0:y1, x0:x1]
                np.copyto(sub, col[y0 - y:y1 - y, x0 - x:x1 - x].astype(np.uint16), where=op[y0 - y:y1 - y, x0 - x:x1 - x])
        out = LUT[img & 0x7fff]
        mb = self.m.r16(self.io + 0x6c)
        mode, f = mb >> 14, min(16, mb & 31)
        if mode in (1, 2) and f:
            o = out.astype(np.int16)
            o = o + (255 - o) * f // 16 if mode == 1 else o - o * f // 16
            out = o.astype(np.uint8)
        return out


def render(m):
    main = _Engine(m, False).render()
    sub = _Engine(m, True).render()
    return (main, sub) if (m.r16(0x4000304) >> 15) & 1 else (sub, main)

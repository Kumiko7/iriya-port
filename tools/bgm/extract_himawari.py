"""Extracts the BGM the alternative soundtrack needs from your own copy of
「ひまわり」 (ぶらんくのーと, 2009) into a separate data/himawari_bgm/ folder.

    python tools/bgm/extract_himawari.py [source] [--out data/himawari_bgm]

source can be the CD image (HIMAWARI.mdf / .iso / .bin), the installer slice
SETUP_1.BIN, an installed game's .xp3 archive, or a folder holding any of them;
by default data/himawari/ is searched. Only the tracks
in the mapping are written (the built-in one in iriya_engine/alt_bgm.py, or
yours from data/himawari_bgm/mapping.json; --all writes every track), as the game's
own .ogg files (not re-encoded), with their loop points in tracks.json.

How the data is reached: ISO 9660 data track -> SETUP/SETUP_1.BIN (an Inno
Setup slice that stores the files uncompressed) -> the KiriKiri XP3 archive ->
bgm/*.ogg and bgm/*.ogg.sli. XP3 entries are XORed with one byte per file,
(0xFE - adler32) & 0xFF.
"""
import argparse
import json
import mmap
import os
import re
import struct
import sys
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))
from iriya_engine.alt_bgm import FOLDER, needed, load_mapping   # noqa: E402
from iriya_engine import paths                                  # noqa: E402

XP3_MAGIC = b'XP3\r\n \n\x1a\x8bg\x01'


# --------------------------------------------------------------- disc images
class _Image:
    """A CD image with the data track as 2048-byte sectors (raw 2352 or cooked)."""

    def __init__(self, path):
        self.f = open(path, 'rb')
        self.m = mmap.mmap(self.f.fileno(), 0, access=mmap.ACCESS_READ)
        self.ssize = self.doff = None
        pos = self.m.find(b'\x01CD001\x01')
        while pos != -1:
            for ss, do in ((2352, 24), (2352, 16), (2048, 0), (2336, 8)):
                if (pos - do) % ss == 0:
                    self.ssize, self.doff, self.pvd = ss, do, (pos - do) // ss
                    break
            if self.ssize:
                break
            pos = self.m.find(b'\x01CD001\x01', pos + 1)
        if not self.ssize:
            raise ValueError('no ISO 9660 track')
        pvd = self.sector(self.pvd)
        self.root = struct.unpack_from('<I', pvd, 158)[0], struct.unpack_from('<I', pvd, 166)[0]
        self.delta = self._find_delta()

    def sector(self, i):
        o = i * self.ssize + self.doff
        return self.m[o:o + 2048]

    def _find_delta(self):
        """Image sector = LBA + delta (a CD Extra data session starts past a gap)."""
        lba = self.root[0]
        base = self.pvd - 16           # the image sector holding the session's LBA 0..
        guesses = [0, base - (lba - lba), -11400, -11250, -11550]
        rng = list(range(-lba, 1)) if lba < 400000 else []
        for d in guesses + rng:
            i = lba + d
            if i < 0 or i * self.ssize >= len(self.m):
                continue
            s = self.sector(i)
            if s[0] >= 34 and s[32] == 1 and s[33] == 0 and struct.unpack_from('<I', s, 2)[0] == lba:
                return d
        raise ValueError('ISO root directory not found')

    def read(self, lba, n, off=0):
        out = []
        first = off // 2048
        skip = off % 2048
        k = first
        while n > 0:
            s = self.sector(lba + self.delta + k)[skip:skip + n]
            out.append(s)
            n -= len(s)
            skip = 0
            k += 1
        return b''.join(out)

    def files(self):
        def walk(lba, ln, path):
            d = self.read(lba, ln)
            p = 0
            while p < len(d):
                L = d[p]
                if L == 0:
                    p = (p // 2048 + 1) * 2048
                    continue
                e_lba, e_len = struct.unpack_from('<I', d, p + 2)[0], struct.unpack_from('<I', d, p + 10)[0]
                flags, nl = d[p + 25], d[p + 32]
                name = d[p + 33:p + 33 + nl]
                p += L
                if name in (b'\0', b'\1'):
                    continue
                nm = name.decode('cp932', 'replace').split(';')[0]
                if flags & 2:
                    yield from walk(e_lba, e_len, path + nm + '/')
                else:
                    yield path + nm, e_lba, e_len
        return list(walk(self.root[0], self.root[1], ''))


class _ImageFile:
    """One file inside a disc image, read by offset."""

    def __init__(self, img, lba, size):
        self.img, self.lba, self.size = img, lba, size

    def __len__(self):
        return self.size

    def read(self, off, n):
        n = max(0, min(n, self.size - off))
        return self.img.read(self.lba, n, off) if n else b''

    def find(self, needle, limit):
        """Offset of needle within the first `limit` bytes (read in 1 MB steps)."""
        step = 1 << 20
        for o in range(0, min(limit, self.size), step):
            b = self.read(o, step + len(needle))
            i = b.find(needle)
            if i != -1:
                return o + i
        return -1


class _PlainFile:
    def __init__(self, path):
        self.f = open(path, 'rb')
        self.m = mmap.mmap(self.f.fileno(), 0, access=mmap.ACCESS_READ)
        self.size = len(self.m)

    def __len__(self):
        return self.size

    def read(self, off, n):
        return self.m[off:off + n]

    def find(self, needle, limit):
        return self.m.find(needle, 0, min(limit, self.size))


# ---------------------------------------------------------------------- XP3
class _XP3:
    def __init__(self, src, base):
        self.src, self.base = src, base
        idx = struct.unpack_from('<Q', src.read(base + 11, 8))[0]
        if idx == 0x17:                       # KiriKiri 2.28+ header with a cushion
            idx = struct.unpack_from('<Q', src.read(base + 0x20, 8))[0]
        head = src.read(base + idx, 17)
        if head[0] == 1:
            cs, us = struct.unpack_from('<QQ', head, 1)
            data = zlib.decompress(src.read(base + idx + 17, cs))
        else:
            us = struct.unpack_from('<Q', head, 1)[0]
            data = src.read(base + idx + 9, us)
        self.entries = {}
        q = 0
        while q + 12 <= len(data):
            tag, sz = data[q:q + 4], struct.unpack_from('<Q', data, q + 4)[0]
            body = data[q + 12:q + 12 + sz]
            q += 12 + sz
            if tag != b'File':
                continue
            r, ent = 0, {'segs': [], 'adlr': None, 'flags': 0, 'name': ''}
            while r + 12 <= len(body):
                t, s = body[r:r + 4], struct.unpack_from('<Q', body, r + 4)[0]
                b = body[r + 12:r + 12 + s]
                r += 12 + s
                if t == b'info':
                    ent['flags'] = struct.unpack_from('<I', b, 0)[0]
                    nl = struct.unpack_from('<H', b, 20)[0]
                    ent['name'] = b[22:22 + nl * 2].decode('utf-16-le', 'replace')
                elif t == b'segm':
                    ent['segs'] = [struct.unpack_from('<IQQQ', b, k) for k in range(0, len(b) - 27, 28)]
                elif t == b'adlr':
                    ent['adlr'] = struct.unpack_from('<I', b, 0)[0]
            if ent['name']:
                self.entries[ent['name'].replace('\\', '/').lower()] = ent

    def read(self, name):
        ent = self.entries.get(name.lower())
        if ent is None:
            return None
        parts = []
        for fl, off, _us, ps in ent['segs']:
            raw = self.src.read(self.base + off, ps)
            parts.append(zlib.decompress(raw) if fl & 7 == 1 else raw)
        d = b''.join(parts)
        if ent['flags'] & 0x80000000 and ent['adlr'] is not None:
            k = (0xFE - ent['adlr']) & 0xFF
            if k:
                d = bytes(_xor_table(k)[b] for b in d) if len(d) < 4096 else _xor(d, k)
        return d


def _xor_table(k):
    return bytes(b ^ k for b in range(256))


def _xor(d, k):
    return d.translate(_xor_table(k))


def _open_xp3(src, limit=1 << 28):
    base = src.find(XP3_MAGIC, limit)
    return _XP3(src, base) if base != -1 else None


# ------------------------------------------------------------------ helpers
def parse_sli(raw):
    """KiriKiri WaveLoopManager .sli -> (from, to) sample positions of the main loop."""
    for enc in ('utf-8-sig', 'utf-16', 'cp932'):
        try:
            t = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        return None
    if t.startswith('LoopStart=') or 'LoopStart=' in t[:40]:     # the older one-line form
        m1, m2 = re.search(r'LoopStart=(\d+)', t), re.search(r'LoopLength=(\d+)', t)
        if m1 and m2:
            a = int(m1.group(1))
            return a + int(m2.group(1)), a
    links = [(int(a), int(b)) for a, b in re.findall(r'From=(\d+);\s*To=(\d+);', t)]
    links = [(f, to) for f, to in links if to < f]
    return max(links) if links else None


def ogg_rate(data):
    i = data.find(b'\x01vorbis')
    if i == -1:
        return 44100
    return struct.unpack_from('<I', data, i + 12)[0]




def open_source(path):
    """-> (read(name), origin, [entry names]) for the archive in path (an image,
    installer slice, .xp3 or folder)."""
    if os.path.isdir(path):
        if os.path.isdir(os.path.join(path, 'bgm')):
            def read(rel):
                p = os.path.join(path, *rel.split('/'))
                return open(p, 'rb').read() if os.path.exists(p) else None
            names = []
            for root, _, fs in os.walk(path):
                rel = os.path.relpath(root, path).replace(os.sep, '/')
                names += [(f if rel == '.' else f'{rel}/{f}').lower() for f in fs]
            return read, path, names
        cands = []
        for root, _, names in os.walk(path):
            for f in names:
                lo = f.lower()
                p = os.path.join(root, f)
                if lo.endswith('.xp3') or lo == 'setup_1.bin':
                    cands.append((0, p))
                elif lo.endswith(('.mdf', '.iso', '.img')) or (lo.endswith('.bin') and os.path.getsize(p) > 1 << 27):
                    cands.append((1, p))
            if os.path.isdir(os.path.join(root, 'bgm')):
                cands.append((-1, root))
        for _, p in sorted(cands):
            try:
                return open_source(p)
            except (OSError, ValueError, struct.error, zlib.error):
                continue
        raise ValueError(f'no Himawari data found in {path}')
    lo = path.lower()
    if lo.endswith('.xp3') or lo.endswith('setup_1.bin'):
        xp = _open_xp3(_PlainFile(path))
        if xp is None:
            raise ValueError(f'no XP3 archive in {path}')
        return xp.read, path, list(xp.entries)
    img = _Image(path)
    for name, lba, size in sorted(img.files(), key=lambda f: -f[2]):
        if name.lower().endswith(('.xp3', '.bin')) and size > 1 << 20:
            xp = _open_xp3(_ImageFile(img, lba, size))
            if xp is not None and any(n.startswith('bgm/') for n in xp.entries):
                return xp.read, f'{path} : {name}', list(xp.entries)
    raise ValueError(f'no XP3 archive in {path}')


def bgm_names(names):
    """Track names (without .ogg) in an archive listing, as the game refers to them."""
    out = []
    for n in names:
        if n.startswith('bgm/') and n.endswith('.ogg') and '無音' not in n:
            out.append(n[4:-4])
    return sorted(set(out))


def extract(read, tracks, out):
    """Write the named tracks (+ loop points) into out, keeping what is there.
    Returns (written, missing)."""
    os.makedirs(out, exist_ok=True)
    info_path = os.path.join(out, 'tracks.json')
    try:
        with open(info_path, encoding='utf-8') as f:
            info = json.load(f)
    except (OSError, ValueError):
        info = {}
    written, missing = [], []
    for name in tracks:
        if name in info and os.path.exists(os.path.join(out, info[name]['file'])):
            continue
        data = read(f'bgm/{name}.ogg')
        if not data or not data.startswith(b'OggS'):
            missing.append(name)
            continue
        sli = read(f'bgm/{name}.ogg.sli')
        fn = name + '.ogg'
        with open(os.path.join(out, fn), 'wb') as f:
            f.write(data)
        info[name] = {'file': fn, 'rate': ogg_rate(data), 'loop': parse_sli(sli) if sli else None}
        written.append(name)
    with open(info_path, 'w', encoding='utf-8') as f:
        json.dump(info, f, indent=1, ensure_ascii=False)
    return written, missing


def main():
    ap = argparse.ArgumentParser(description=__doc__.split(chr(10))[0])
    ap.add_argument('source', nargs='?', default=paths.data_path('himawari'))
    ap.add_argument('--out', default=paths.data_path(FOLDER))
    ap.add_argument('--all', action='store_true', help='every BGM track, not only the mapped ones')
    args = ap.parse_args()
    read, origin, names = open_source(args.source)
    print('reading', origin)
    want = bgm_names(names) if args.all else needed(load_mapping(args.out))
    written, missing = extract(read, want, args.out)
    size = sum(os.path.getsize(os.path.join(args.out, f)) for f in os.listdir(args.out) if f.endswith('.ogg'))
    print(f'{len(written)} tracks written, {len(want) - len(written) - len(missing)} already there; '
          f'folder {size / 1e6:.1f} MB -> {args.out}')
    if missing:
        print('not found:', ', '.join(missing))


if __name__ == '__main__':
    main()

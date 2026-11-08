"""Per-game asset access: images, faces, font, ruby font, audio and scripts."""
import os
import struct
import numpy as np
import pygame

from . import nnt
from .rom import NDSRom
from .sdat import SDAT
from .textcodec import Font
from .cards import load_cards

# Chapter titles (from the games' title/name*.nnt plates) in script order.
CHAPTERS = {
    'iriya1': [
        ('0', '第三種接近遭遇１'), ('1', '第三種接近遭遇２'),
        ('2', 'ラブレター１'), ('3', 'ラブレター２'),
        ('4', '正しい原チャリの盗み方・前編１'), ('5', '正しい原チャリの盗み方・前編２'),
        ('6', '正しい原チャリの盗み方・後編１'), ('7', '正しい原チャリの盗み方・後編２'),
        ('8', '十八時四十七分三十二秒・前編１'), ('9', '十八時四十七分三十二秒・前編２'),
        ('A', '十八時四十七分三十二秒・後編１'), ('B', '十八時四十七分三十二秒・後編２'),
        ('C', 'スタッフロール'),
        ('D', 'あとがき'),
        ('E', 'それ以外のことについて言えば、'),
    ],
    'iriya2': [
        ('0', '無銭飲食列伝１'), ('1', '無銭飲食列伝２'),
        ('2', '水前寺応答せよ・前編１'), ('3', '水前寺応答せよ・前編２'),
        ('4', '水前寺応答せよ・後編１'), ('5', '水前寺応答せよ・後編２'),
        ('6', '夏休みふたたび・前編１'), ('7', '夏休みふたたび・前編２'),
        ('8', '夏休みふたたび・後編１'), ('9', '夏休みふたたび・後編２'),
        ('A', '最後の道１'), ('B', '最後の道２'),
        ('C', '南の島１'), ('D', '南の島２'),
        ('E', 'エピローグ１'), ('F', 'スタッフロール'), ('G', 'エピローグ２'),
        ('H', '番外編・ＥＳＰの冬'), ('I', 'グラウンド・ゼロ'),
        ('J', 'あとがき'),
    ],
}
GAME_TITLES = {'iriya1': 'イリヤの空、ＵＦＯの夏', 'iriya2': 'イリヤの空、ＵＦＯの夏Ⅱ'}


def _surface(rgba):
    h, w = rgba.shape[:2]
    surf = pygame.image.frombuffer(np.ascontiguousarray(rgba).tobytes(), (w, h), 'RGBA')
    return surf.convert_alpha() if pygame.display.get_surface() else surf.copy()


class FaceSheet:
    def __init__(self, surface, frames):
        self.surface = surface
        self.frames = frames  # list of pygame.Rect

    def frame(self, i):
        if not self.frames:
            return self.surface.subsurface(pygame.Rect(0, 0, min(128, self.surface.get_width()), min(64, self.surface.get_height())))
        i = max(0, min(len(self.frames) - 1, i))
        # some .buf frames reach past the sheet (w_4nin: 176 wide on a 128 sheet)
        r = self.frames[i].clip(self.surface.get_rect())
        return self.surface.subsurface(r if r.w and r.h else pygame.Rect(0, 0, 1, 1))


class GameData:
    def __init__(self, game_id, rom_path):
        self.id = game_id
        self.rom = NDSRom(rom_path)
        self.font = Font(self.rom.read('com/font.bin'), game_id)
        self.ruby_raw = self.rom.read('com/rubi.bin')
        snd = [p for p in self.rom.files if p.startswith('snd/') and p.endswith('.sdat')][0]
        self.sdat = SDAT(self.rom.read(snd))
        self._img = {}
        self._faces = {}
        self._glyphs = {}
        self._ruby = {}
        self._pcm = {}
        self._han = {}
        self.han_raw = self.rom.read('com/han.bin') if 'com/han.bin' in self.rom.files else b''
        self.chapters = list(CHAPTERS[game_id])
        self._cards = None
        self.alt = None              # the other game, for '2/' image names in bonus scripts
        self.extra_scripts = {}      # chapter id -> .nsb bytes (bonus chapters, see bonus.py)
        self.extra_titles = {}
        self.extra_images = {}       # name -> loader() for 'x/name' images

    # ---------------------------------------------------------------- cards
    @property
    def cards(self):
        if self._cards is None:
            self._cards = load_cards(self.rom.read('card/card.bin'), self.font)
        return self._cards[1]

    @property
    def card_categories(self):
        _ = self.cards
        return self._cards[0]

    def card_image(self, card, size=''):
        """Card art: size '' = large, 'b' = medium, 's' = small thumbnail."""
        for suffix in (size, 's', ''):
            p = f'card/{card.image}{suffix}.nnt'
            if p in self.rom.files:
                return self.image(p, rotate=True)
        return None

    # ---------------------------------------------------------------- files
    def has(self, path):
        return path in self.rom.files

    def script(self, chap):
        if chap in self.extra_scripts:
            return self.extra_scripts[chap]
        return self.rom.read(f'chapter{chap}.nsb')

    # --------------------------------------------------------------- images
    def image(self, path, rotate=False):
        """Decoded .nnt as a Surface. rotate=True turns DS-hardware-oriented art upright."""
        key = (path, rotate)
        s = self._img.get(key)
        if s is None:
            rgba, _ = nnt.decode(self.rom.read(path))
            if rotate:
                rgba = np.rot90(rgba, 1)  # counter-clockwise
            s = _surface(rgba)
            self._img[key] = s
        return s

    def bg_bytes(self, name):
        """Size in the ROM of the image bg(name) reads (0 if not a plain ROM file)."""
        if '|' in name or name[:2] in ('2/', 'x/'):
            return 0
        for d in ('bg/', 'card/', 'title/', 'com/'):
            p = f'{d}{name}.nnt'
            if p in self.rom.files:
                start, end = self.rom.files[p]
                return end - start
        return 0

    def bg(self, name):
        """Background by name. 'a|b' tries each; '2/name' reads the other game,
        'x/name' an image added at runtime (bonus chapters)."""
        if '|' in name or name[:2] in ('2/', 'x/'):
            for alt in name.split('|'):
                if alt.startswith('x/'):
                    key = ('x', alt)
                    s = self._img.get(key)
                    if s is None and alt[2:] in self.extra_images:
                        s = self.extra_images[alt[2:]]()
                        if s is not None:
                            self._img[key] = s
                elif alt.startswith('2/'):
                    s = self.alt.bg(alt[2:]) if self.alt is not None else None
                else:
                    s = self.bg(alt)
                if s is not None:
                    return s
            return None
        for d in ('bg/', 'card/', 'title/', 'com/'):
            p = f'{d}{name}.nnt'
            if p in self.rom.files:
                return self.image(p, rotate=True)
        return None

    # ------------------------------------------------- upscaled ("高画質") art
    # Files made by tools/upscale_assets.py: <hd_dir>/<rom path>.webp at 4x, in
    # the ROM's own orientation. Each is scaled once to the window's scale k.
    hd_dir = None
    hd_upright = False               # files already turned upright (manifest 'upright')
    HD_CACHE_BYTES = 320 * 1024 * 1024

    def _hd_get(self, key):
        c = self.__dict__.setdefault('_hd', {})
        s = c.pop(key, None)
        if s is not None:
            c[key] = s                       # most recently used last
        return s

    def _hd_put(self, key, s):
        c = self.__dict__.setdefault('_hd', {})
        c[key] = s
        size = sum(v.get_width() * v.get_height() * 4 for v in c.values() if v is not None)
        while size > self.HD_CACHE_BYTES and len(c) > 1:
            old = c.pop(next(iter(c)))
            if old is not None:
                size -= old.get_width() * old.get_height() * 4
        return s

    def _hd_image(self, path, rotate, k):
        """The 1x image `path` as a surface k times larger: from the upscaled file
        when there is one, else the original smoothly enlarged."""
        key = ('img', path, rotate, round(k, 4))
        s = self._hd_get(key)
        if s is not None:
            return s
        base = self.image(path, rotate)
        size = (max(1, round(base.get_width() * k)), max(1, round(base.get_height() * k)))
        src = None
        if self.hd_dir:
            f = os.path.join(self.hd_dir, path[:-4] + '.webp')
            if os.path.exists(f):
                try:
                    src = pygame.image.load(f)
                    if pygame.display.get_surface():
                        # opaque art blits much faster without an alpha channel
                        src = src.convert_alpha() if src.get_flags() & pygame.SRCALPHA else src.convert()
                    if rotate and not self.hd_upright:
                        src = pygame.transform.rotate(src, 90)
                except pygame.error:
                    src = None
        if src is None:
            src = base
        s = src if src.get_size() == size else pygame.transform.smoothscale(src, size)
        return self._hd_put(key, s)

    def bg_hd(self, name, k):
        """bg(name) at k times its size (same name rules as bg)."""
        key = ('bg', name, round(k, 4))
        s = self._hd_get(key)
        if s is not None:
            return s
        s = None
        for alt in name.split('|'):
            if alt.startswith('x/'):
                load = self.extra_images.get(alt[2:])
                if load is not None:
                    try:
                        s = load(k)
                    except TypeError:
                        s = None
                    if s is None and self.bg(alt) is not None:
                        b = self.bg(alt)
                        s = pygame.transform.smoothscale(b, (round(b.get_width() * k), round(b.get_height() * k)))
            elif alt.startswith('2/'):
                s = self.alt.bg_hd(alt[2:], k) if self.alt is not None else None
            else:
                for d in ('bg/', 'card/', 'title/', 'com/'):
                    p = f'{d}{alt}.nnt'
                    if p in self.rom.files:
                        s = self._hd_image(p, True, k)
                        break
            if s is not None:
                break
        return self._hd_put(key, s) if s is not None else None

    def face_hd(self, name, k):
        """face(name) with the sheet and its frame rectangles k times larger."""
        key = (name, round(k, 4))
        cache = self.__dict__.setdefault('_hdf', {})
        fs = cache.get(key)
        if fs is not None and fs.surface is self._hd_get(('img', f'win/{name}.nnt', False, key[1])):
            return fs
        base = self.face(name)
        if base is None:
            return None
        surf = self._hd_image(f'win/{name}.nnt', False, k)
        sx = surf.get_width() / base.surface.get_width()
        sy = surf.get_height() / base.surface.get_height()
        frames = [pygame.Rect(round(r.x * sx), round(r.y * sy), round(r.w * sx), round(r.h * sy))
                  for r in base.frames]
        frames = [r.clip(surf.get_rect()) for r in frames]
        fs = FaceSheet(surf, frames)
        if len(cache) > 64:
            cache.clear()
        cache[key] = fs
        return fs

    def face(self, name):
        fs = self._faces.get(name)
        if fs is None:
            p = f'win/{name}.nnt'
            if p not in self.rom.files:
                return None
            surf = self.image(p)
            frames = []
            b = f'win/{name}.buf'
            if b in self.rom.files:
                frames = self._parse_buf(self.rom.read(b))
            fs = FaceSheet(surf, frames)
            self._faces[name] = fs
        return fs

    @staticmethod
    def _parse_buf(d):
        # u16 count, then u16 offsets: [sheet header, frame0..frameN-1, end]
        n = struct.unpack_from('<H', d, 0)[0]
        offs = struct.unpack_from('<%dH' % (n + 1), d, 2)
        frames = []
        for i in range(1, n + 1):
            o = offs[i]
            if o + 0x48 > len(d):
                break
            # entry: u16 type, name[..], then rect at +0x40
            x, y, w, h = struct.unpack_from('<4H', d, o + 0x40)
            frames.append(pygame.Rect(x, y, w, h))
        return frames

    # ----------------------------------------------------------------- text
    def glyph(self, idx, vertical=True):
        """12x13 white glyph surface (alpha) for a font index; vertical forms for punctuation.

        Codes 0x20XX are half-width ASCII characters from com/han.bin.
        """
        if 0x2000 <= idx < 0x2300:     # half-width (lone or 縦中横 halves)
            return self.han_glyph(idx & 0xFF)
        key = (idx, vertical)
        s = self._glyphs.get(key)
        if s is None:
            src = idx
            if vertical and 614 <= idx < 1228:
                src = idx - 614
            if src >= self.font.count:
                src = 614
            bm = self.font.bitmap(src)
            s = self._mask_surface(bm)
            self._glyphs[key] = s
        return s

    def han_glyph(self, code):
        s = self._han.get(code)
        if s is None:
            g = self.han_raw[code * 24:code * 24 + 12]   # 24 bytes per glyph, 6 columns used
            bm = np.zeros((13, 6), bool)
            if len(g) == 12:
                cols = np.frombuffer(g, '<u2')
                for c in range(6):
                    for b in range(16):
                        if cols[c] & (1 << b) and 15 - b < 13:
                            bm[15 - b, c] = True
            s = self._mask_surface(bm)
            self._han[code] = s
        return s

    def ruby_glyph(self, idx):
        s = self._ruby.get(idx)
        if s is None:
            g = self.ruby_raw[idx * 8:idx * 8 + 8]
            bm = np.zeros((8, 8), bool)
            for col in range(min(8, len(g))):
                for y in range(8):
                    if g[col] & (1 << y):
                        bm[7 - y, col] = True
            s = self._mask_surface(bm)
            self._ruby[idx] = s
        return s

    @staticmethod
    def _mask_surface(bm):
        h, w = bm.shape
        rgba = np.zeros((h, w, 4), np.uint8)
        rgba[..., :3] = 255
        rgba[..., 3] = np.where(bm, 255, 0)
        return _surface(rgba)

    def text_glyphs(self, s):
        """Map a Unicode string to font glyph indices (for UI text)."""
        out = []
        for ch in s:
            if ch == '\n':
                out.append(None)
                continue
            o = ord(ch)
            if ch == ' ':
                ch = '　'
            elif 0x21 <= o <= 0x7e:
                ch = chr(o - 0x21 + 0xFF01)
            i = self.font.from_char.get(ch)
            if i is None:
                i = self.font.from_char.get('　')
            out.append(i)
        return out

    # ---------------------------------------------------------------- audio
    def pcm(self, strm_id):
        """(int16 mono array resampled to 44.1kHz, loop_start_sample or None)."""
        r = self._pcm.get(strm_id)
        if r is None:
            if not (0 <= strm_id < len(self.sdat)) or self.sdat.file_ids[strm_id] is None:
                return None
            st = self.sdat.strm(strm_id)
            pcm = st.decode()[:, 0].astype(np.float32)
            ratio = 44100 / st.rate
            n = int(len(pcm) * ratio)
            x = np.arange(n, dtype=np.float32) / ratio
            res = np.interp(x, np.arange(len(pcm), dtype=np.float32), pcm).astype(np.int16)
            loop = int(st.loop_start * ratio) if st.loop else None
            r = (res, loop, st.name)
            self._pcm[strm_id] = r
        return r

"""Compiles a parsed light-novel chapter (see bonus.py) into an .nsb script.

The output uses the same opcodes, layout and pacing conventions as the DS
scripts: a 4f line marker at the top of every column, 14 characters per column
(15 with a hanging 、。」), 8 columns per book page, op 41 waits at sentence
ends, 42 + 40 at paragraph ends, and a page-clear wait at scene breaks.
"""
import struct
import logging

log = logging.getLogger('bonus')

SCENE_BREAK = '\x00break'

SUBST = {'─': '―', '〜': '～', '噓': '嘘', '𠮟': '叱', '剝': '剥', '頰': '頬', '‐': '－',
         '‼': '！！', '⁉': '！？', '⁈': '？！', '﨑': '崎', '髙': '高', '·': '・', '•': '・'}
HANG = set('、。，．」』）')                # may hang as the 15th character of a column
SENTENCE_END = set('。！？')
CLOSERS = set('」』）')
SMALL_KANA = str.maketrans('ぁぃぅぇぉっゃゅょゎァィゥェォッャュョヮヵヶ', 'あいうえおつやゆよわアイウエオツヤユヨワカケ')
COL_UNITS = 28                               # 14 full-width characters, in half-width units
COLS_PER_PAGE = 8

FACES = {   # who: (window id, face sheet, image x inside the window, window y)
    'mayumi': (0x39, 'w_mayumi_01_l', 0, 8),
    'enomoto': (0x3a, 'w_enomoto_01_r', 50, 80),
}


def plain(para):
    if not isinstance(para, list):
        return para
    return ''.join(t[1] for t in para)


class Builder:
    def __init__(self, font):
        from .textcodec import RUBY_CHARS
        self.words = []
        self.cmap = {}
        for i, c in font.to_char.items():
            if i >= 614 and len(c) == 1 and (c not in self.cmap or i < self.cmap[c]):
                self.cmap[c] = i
        self.rmap = {}
        for i, c in enumerate(RUBY_CHARS):
            self.rmap.setdefault(c, i)
        self.line = 0
        self.page = 1
        self.col_in_page = 0
        self.colno = 0
        self.cur = 0
        self.col_open = False
        self.pending = None          # a paragraph end whose wait is not written yet
        self.missing = set()
        self.faces_on = set()

    # ------------------------------------------------------------ encoding
    def op(self, code, *args):
        self.words.append((code << 8) | (1 + len(args)))
        self.words.extend(int(a) & 0xFFFF for a in args)

    def op_str(self, code, nums, name):
        raw = name.encode('ascii') + b'\0'
        if len(raw) % 2:
            raw += b'\0'
        self.op(code, *nums, *struct.unpack('<%dH' % (len(raw) // 2), raw))

    def binary(self):
        code = struct.pack('<%dH' % len(self.words), *self.words)
        return struct.pack('<IHH', 8, min(self.line, 0xFFFF), 0) + code

    # ------------------------------------------------------------ text
    def glyph_of(self, ch):
        o = ord(ch)
        if ch == ' ':
            ch = '　'
        elif 0x21 <= o <= 0x7e:
            ch = chr(o - 0x21 + 0xFF01)
        i = self.cmap.get(ch)
        if i is None:
            self.missing.add(ch)
            i = self.cmap.get('〓', 614)
        return i

    def start_column(self):
        if self.col_open:
            self.op(0x40)
        self.line += 1
        if self.col_in_page >= COLS_PER_PAGE:
            self.page += 1
            self.col_in_page = 0
        self.op(0x4f, self.line, self.page, self.colno)
        self.colno += 1
        self.col_in_page += 1
        self.cur = 0
        self.col_open = True

    def end_wait(self, kind=0x42):
        """Write the wait that ends the previous paragraph (42 = click, 48/49/4a = page clear)."""
        if self.pending is None:
            return
        self.pending = None
        self.op(kind)
        if kind in (0x48, 0x49, 0x4a):
            self.col_open = False
            self.page += 1
            self.col_in_page = 0

    def units(self, para):
        """[(glyphs, width, ruby, text)] for one paragraph."""
        out = []
        for tok in para:
            if tok[0] == 't':
                for ch in ''.join(SUBST.get(c, c) for c in tok[1]):
                    out.append(([self.glyph_of(ch)], 2, None, ch))
            elif tok[0] == 'tcy':
                s = tok[1]
                if len(s) == 2 and s.isascii():
                    out.append(([0x2100 | ord(s[0]), 0x2200 | ord(s[1])], 2, None, s))
                else:
                    for ch in s:
                        out.append(([self.glyph_of(ch)], 2, None, ch))
            else:
                base = ''.join(SUBST.get(c, c) for c in tok[1])
                rt = tok[2].translate(SMALL_KANA).replace('‐', 'ー')
                codes = [self.rmap.get(c) for c in rt]
                ruby = (len(base), codes) if base and rt and None not in codes else None
                out.append(([self.glyph_of(c) for c in base], 2 * len(base), ruby, base))
        return out

    def paragraph(self, para):
        units = self.units(para)
        text = ''.join(u[3] for u in units)
        self.end_wait(0x42)
        self.start_column()
        pos = 0
        for glyphs, width, ruby, s in units:
            if self.cur + width > COL_UNITS and not (s in HANG and self.cur + width <= COL_UNITS + 2):
                self.start_column()
            self.words.extend(glyphs)
            self.cur += width
            if ruby is not None:
                nb, codes = ruby
                self.op(0x44, ((2 * nb) << 8) | (2 * len(codes)), *codes)
            pos += len(s)
            rest = text[pos:]
            if rest.strip('　') and self._sentence_break(s, rest):
                self.op(0x41)
        self.pending = True

    @staticmethod
    def _sentence_break(s, rest):
        last, nxt = s[-1:], rest[:1]
        if last == '。':
            return nxt not in CLOSERS and nxt not in SENTENCE_END
        if last in '！？':
            return nxt == '　'
        return False

    def blank(self):
        self.end_wait(0x42)
        self.start_column()
        self.pending = True

    # ------------------------------------------------------------ staging
    def bg(self, name, frames=30, x=8):
        if frames <= 0:
            self.op_str(0x70, [0, 3, x, 8], name)
            self.op(0x73, 0, 255, 0)
            return
        self.op_str(0x70, [1, 3, x, 8], name)
        self.op(0x71, 1, 4)
        self.op(0x73, 1, 0, 0)
        self.op(0x73, 1, 255, frames)
        self.op(0x58, frames)
        self.op(0x7d, 0, 1)
        self.op(0x71, 0, 3)
        self.op(0x71, 1, 4)

    def act(self, a):
        kind, args = a[0], tuple(a[1:])
        if kind == 'bg':
            self.bg(*args)
        elif kind == 'dim':                  # darken the background (night, dim rooms)
            alpha, frames = (args + (30,))[:2]
            self.op(0x73, 0, alpha, frames)
        elif kind == 'pan':                  # scroll the background to x over frames
            x, frames = args
            self.op(0x74, 0, x, 8, frames)
        elif kind == 'bgm':
            sid, fade = (args + (60,))[:2]
            self.op(0x96, sid, 0)
            self.op(0x93, 0, 0)
            self.op(0x90, fade)
        elif kind == 'bgm_out':
            self.op(0x91, args[0] if args else 90)
        elif kind == 'se':
            sid, ch = (args + (1,))[:2]
            self.op(0x9e, ch, sid, 0)
            self.op(0x98, ch, 0)
        elif kind == 'se_stop':
            ch, frames = (args + (1, 0))[:2]
            self.op(0x99, ch, frames)
        elif kind == 'wait':
            self.op(0x58, args[0])
        elif kind == 'shake':
            axis, amp, frames = args
            self.op(0x75, 0, axis, amp, frames)
        elif kind == 'flash':
            frames = args[0] if args else 30
            self.op(0x66, 255, 255, 255)     # 62 starts fully covered: a white flash
            self.op(0x62, 2, frames)
            self.op(0x58, frames)
            self.op(0x66, 0, 0, 0)
        elif kind == 'face':
            who, expr = args
            fid, sheet, ix, y = FACES[who]
            if who not in self.faces_on:
                self.faces_on.add(who)
                self.op(0x88, fid)
                self.op_str(0x80, [fid, 0], sheet)
                self.op(0x81, fid, 176, 56)
                self.op(0x8e, fid, 0, 0, 0, 64, 1)
                self.op(0x82, fid, 11)
                self.op(0x8b, fid, ix, 4, 1, 0)
                self.op(0x83, fid, 8, y, 0, 0)
                self.op(0xe1, fid, expr)
                self.op(0xe2, fid, 1)
                self.op(0x87, fid, 255, 15)  # new windows start invisible
            else:
                self.op(0xe1, fid, expr)
                self.op(0xe2, fid, 15)
        elif kind == 'face_off':
            for who in (args or tuple(self.faces_on)):
                if who in self.faces_on:
                    self.faces_on.discard(who)
                    self.op(0x87, FACES[who][0], 0, 30)
        else:
            raise ValueError(kind)

    def opening(self, title_image, first):
        """Title illustration, then the first scene (a list of actions incl. a 'bg')."""
        for li, pr in ((0, 3), (1, 4), (2, 2), (3, 5)):
            self.op(0x71, li, pr)
        self.op(0x66, 0, 0, 0)               # fades go through black
        self.op(0x63, 2, 1)                  # (0 frames would mean 60 on the DS)
        if title_image:
            self.op_str(0x70, [0, 3, 8, 8], title_image)
            self.op(0x73, 0, 255, 0)
            self.op(0x58, 30)
            self.op(0x62, 2, 90)
            self.op(0x58, 90 + 210)
            self.op(0x63, 2, 60)
            self.op(0x58, 70)
        for a in first:
            if a[0] == 'bg':
                self.bg(a[1], 0, *a[3:4])
            else:
                self.act(a)
        self.op(0x62, 2, 60)
        self.op(0x58, 60)

    def ending(self):
        self.end_wait(0x4a)
        if self.faces_on:
            self.act(('face_off',))
        self.op(0x91, 120)
        self.op(0x63, 2, 120)
        self.op(0x58, 150)
        self.op(0xff)


def compile_chapter(font, paras, staging, title_image):
    """staging: {'open': [actions], 'cues': [(anchor, [actions])]}.

    A cue runs before the first paragraph (at or after the previous cue's) whose
    text contains `anchor`. ('clear', op) in a cue ends the page there; a scene
    break in the book always clears the page with a dissolve.
    Returns (nsb bytes, unmatched cue anchors, characters missing from the font).
    """
    b = Builder(font)
    b.opening(title_image, staging.get('open', []))
    cues = list(staging.get('cues', []))
    want_clear = None
    for p in paras:
        if p == SCENE_BREAK:
            want_clear = 0x4a
            continue
        text = plain(p) if isinstance(p, list) else ''
        acts = []
        while cues and text and cues[0][0] in text:
            acts += cues.pop(0)[1]
        for a in acts:
            if a[0] == 'clear':
                want_clear = a[1] if len(a) > 1 else 0x4a
        if want_clear is not None:
            b.end_wait(want_clear)
            want_clear = None
        elif acts:
            b.end_wait(0x42)
        for a in acts:
            if a[0] != 'clear':
                b.act(a)
        if p == '':
            if b.col_open:
                b.blank()
        else:
            b.paragraph(p)
    b.ending()
    unused = [c[0] for c in cues]
    if unused:
        log.warning('bonus cues not matched: %s', unused)
    if b.missing:
        log.warning('characters missing from the DS font: %s', ''.join(sorted(b.missing)))
    return b.binary(), unused, b.missing

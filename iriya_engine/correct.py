"""Corrected scripts: the DS chapter text brought back to the light novels' wording.

The DS scripts were typed in from the books and carry misreads (転枚生, 心職,
カ for 力 …), small cuts and the JIS X 0208 forms of a few kanji (麺 for 麵).
With オプション → 本文 set to 「修正版」 the port compares each chapter's script
with the matching chapter of the user's EPUB at load time and patches the
glyphs to the book's text. Nothing from the books is stored in the port: the
only data here are hashes of the few places where the EPUB itself has the typo
(KEEP_DS), so the DS text is kept there.

Paragraphs whose length changes are re-wrapped like the DS lays out a column:
14 cells, one hanging 、。」』 / small kana, never ending a column on 「『.
Every original line marker (op 4f) is kept, so bookmarks, cards and the keyword
radar still see the same line ids; extra columns repeat their paragraph's last
marker.
"""
import difflib
import hashlib
import logging
import re
import unicodedata

from .vm import Instr

log = logging.getLogger('correct')

CANON = str.maketrans({'─': '―', '—': '―', '〜': '～', '~': '～'})
PROTECT = '\ue000'  # a DS glyph slot redrawn as English (textcodec.REDRAWN): never edited
HANG = set('、。」』')
COL_CELLS = 14
CLICKS = (0x41, 0x42, 0x48, 0x49, 0x4a)
HARD = (0x42, 0x48, 0x49, 0x4a)
PAGE_CLEAR = (0x48, 0x49, 0x4a)    # clicks that also clear the page: a column ends there

# Places where the EPUB has the typo and the DS is right, DS-only layouts (the
# cinema sign, the epilogue's repeated last paragraph) and the book's ASCII hyphen
# in MJ-12 / バンディット1-3 (it would lie sideways in vertical text): hashes of
# (game, chapter, DS context, DS text, book text), see edit_key().
KEEP_DS = {
    'd4a1c959ca1e', '49c47408d19e', '909f479146e8', '4a57cd257eed', 'f6a45d063dcc', 'cf3a9914527b',
    '6c1dd83ffe54', '06f082abc307', '39a8c19dad39', '4589b987c2ae', '36af51d510c7', '389a22efb82e',
    'aa939d677d63', 'a68124f48974', 'dd9cfd365a1e', 'c7455faa9b3a', 'b224c756304d', 'da72e04eb4ed',
    '7707436dd703', 'baf9fbb44a60', 'dc688fb1f3c1', 'ff2ca71b300e', 'ec884c781d8a', 'af6438a7b740',
    'cfa10aa71641', '37963dcd56f1', 'b5642734cb46', '001db3751448', '85f55d05e99f', '77d402384195',
    '83690262ffc8', '3b2b43ea7a48', '40b9c399481d', '49cf0fdf2ca8', '683d32de22aa', '1cedf18e86b6',
    '0ef79c42ab3d', '5601f988cf74', 'c5ca5d1d89c5', 'f845cac115f7', '3d0c0f20d8bd', '02afec32dcc4',
}


def norm1(ch):
    s = unicodedata.normalize('NFKC', ch)
    return (s if len(s) == 1 else ch).translate(CANON)


def edit_key(gid, chap, ctx, ds, bk):
    return hashlib.sha1(f'{gid}|{chap}|{ctx}|{ds}|{bk}'.encode('utf-8')).hexdigest()[:12]


# --------------------------------------------------------------------- sequences
def ds_tokens(prog, font):
    """[(instr index, char, starts a paragraph)] for the chapter's glyphs, spaces left out.
    A paragraph starts after a click that ends its column (42/48/49/4a, then 40)."""
    toks = []
    hard = False
    par = True
    for k, ins in enumerate(prog.instrs):
        op = ins.op
        if op == 'G':
            g = ins.glyph
            if g == 614 or g == 0x2020:
                continue
            ch = chr(g & 0xFF) if 0x2000 <= g < 0x2300 else font.char(g)
            c = norm1(ch) if len(ch) == 1 else PROTECT
            if c.isspace():
                continue
            toks.append((k, c, par))
            hard = par = False
        elif op in PAGE_CLEAR:
            par, hard = True, False
        elif op in HARD:
            hard = True
        elif op == 0x40 and hard:
            par = True
            hard = False
    return toks


def book_chars(paras, scene_break):
    """[(compare char, book char, starts a paragraph, paragraph indented)], ruby readings left out."""
    out = []
    for p in paras:
        if p == scene_break or not isinstance(p, list):
            continue
        first = True
        text = ''.join(t[1] for t in p)
        indent = text[:1] in ('　', ' ')
        for ch in text:
            c = norm1(ch)
            if c.isspace():
                continue
            out.append((c, ch, first, indent))
            first = False
    return out


def _units(s):
    return [u for u in re.split(r'(?<=[\n。！？」』])', s) if u]


def diff(a, b):
    """Char-level opcodes between strings a and b (sentence units first, for speed)."""
    ua, ub = _units(a), _units(b)
    pa = [0]
    for u in ua:
        pa.append(pa[-1] + len(u))
    pb = [0]
    for u in ub:
        pb.append(pb[-1] + len(u))
    out = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, ua, ub, autojunk=False).get_opcodes():
        if tag == 'equal':
            continue
        A, B = pa[i1], pb[j1]
        sa, sb = a[A:pa[i2]], b[B:pb[j2]]
        for t2, x1, x2, y1, y2 in difflib.SequenceMatcher(None, sa, sb, autojunk=False).get_opcodes():
            if t2 != 'equal':
                out.append((t2, A + x1, A + x2, B + y1, B + y2))
    return out


# --------------------------------------------------------------------- edits
class Edit:
    __slots__ = ('a1', 'a2', 'new', 'key', 'ds', 'bk', 'own_par')

    def __init__(self, a1, a2, new, key, ds, bk, own_par=False):
        self.a1, self.a2, self.new, self.key, self.ds, self.bk = a1, a2, new, key, ds, bk
        self.own_par = own_par


def find_edits(parts, book, keep=KEEP_DS):
    """parts: [(gid, chap, tokens)] for the game chapters of one book chapter, in order.
    -> ({chap: [Edit]}, skipped) with a1/a2 indexing that chapter's tokens."""
    flat, owner = [], []
    for gid, chap, toks in parts:
        for i, t in enumerate(toks):
            flat.append(t[1])
            owner.append((gid, chap, i, t[2]))
    a = ''.join(flat)
    b = ''.join(c[0] for c in book)
    edits = {chap: [] for _, chap, _ in parts}
    skipped = []
    for tag, a1, a2, b1, b2 in diff(a, b):
        ds, bk = a[a1:a2], b[b1:b2]
        if PROTECT in a[max(0, a1 - 2):a2 + 2]:
            skipped.append(('protect', ds, bk))
            continue
        at = min(a1, len(owner) - 1)
        gid, chap, i1, _ = owner[at]
        if a1 == len(owner):           # appended after the last DS glyph
            i1 += 1
        if a2 > a1 and owner[a2 - 1][1] != chap:
            skipped.append(('span', ds, bk))
            continue
        ctx = a[max(0, a1 - 4):a1]
        key = edit_key(gid, chap, ctx, ds, bk)
        if key in keep:
            skipped.append(('keep', ds, bk))
            continue
        new = book[b1:b2]
        # whole book paragraphs inserted where a DS paragraph starts: keep them apart
        own_par = (tag == 'insert' and new and new[0][2] and (b2 == len(book) or book[b2][2])
                   and (a1 == len(owner) or owner[a1][3]))
        edits[chap].append(Edit(i1, i1 + (a2 - a1), new, key, ds, bk, own_par))
    return edits, skipped


# --------------------------------------------------------------------- glyphs
# JIS2004 forms the DS font lacks -> the JIS X 0208 form it has (1x text, or a
# high-res font without them).
FALLBACK = dict(zip('麵嚙噓頰剝吞搔瘦摑醬鹼塡囊繫簞壼噬', '麺噛嘘頬剥呑掻痩掴醤鹸填嚢繋箪壷噛'))
VIRTUAL = 0x40000          # glyph codes from here: characters the DS font doesn't have


def _cells(g):
    if g == 0x2020:
        return 0.5
    if g < VIRTUAL and g & 0xFF00 == 0x2100:
        return 0
    return 1


class Corrector:
    """Patches one game's chapter programs to the EPUB text (see the module doc)."""

    def __init__(self, game, epubs):
        from .assets import CHAPTERS
        from .bonus_script import SCENE_BREAK
        self.game, self.epubs, self.scene_break = game, epubs, SCENE_BREAK
        self.chapters = list(CHAPTERS.get(game.id, []))
        self._edits = {}
        font = game.font
        self.cmap = {}
        for i, c in font.to_char.items():
            if 614 <= i < VIRTUAL and len(c) == 1 and (c not in self.cmap or i < self.cmap[c]):
                self.cmap[c] = i
        self.virtual = {}           # char -> code
        if not hasattr(game, 'virtual_glyphs'):
            game.virtual_glyphs = {}

    # ------------------------------------------------------------ edits
    def _book(self, title):
        bt = re.sub(r'[１２]$', '', title or '')
        if not bt or bt == 'あとがき':
            return None
        for v, e in sorted(self.epubs.items()):
            if any(t == bt for t, _ in e.toc):
                return v, bt
        return None

    def has(self, chap):
        return self._book(dict(self.chapters).get(chap)) is not None

    def edits(self, chap):
        if chap in self._edits:
            return self._edits[chap]
        from .bonus import parse_chapter
        from .vm import Program
        hit = self._book(dict(self.chapters).get(chap))
        if hit is None:
            self._edits[chap] = []
            return []
        v, bt = hit
        parts = [(self.game.id, c, ds_tokens(Program(self.game.script(c)), self.game.font))
                 for c, t in self.chapters if self._book(t) == hit]
        _, paras = parse_chapter(self.epubs[v], bt)
        edits, _skipped = find_edits(parts, book_chars(paras, self.scene_break))
        for c, el in edits.items():
            self._edits[c] = el
        return self._edits.setdefault(chap, [])

    # ------------------------------------------------------------ glyph codes
    def char_of(self, g):
        if g < VIRTUAL and 0x2000 <= g < 0x2300:
            return chr(g & 0xFF)
        return self.game.font.char(g)

    def glyph_for(self, ch, like=None):
        """Font code for book character ch; `like` = the DS glyph it replaces or sits next to."""
        c = ch.translate(CANON)
        if like is not None:
            lc = self.char_of(like)
            if len(lc) == 1 and norm1(lc) == norm1(c):
                return like
            if like < VIRTUAL and 0x2000 <= like < 0x2300:      # half-width slot: stay half-width
                a = unicodedata.normalize('NFKC', c)
                if len(a) == 1 and ' ' < a < '\x7f':
                    return (like & 0xFF00) | ord(a)
        for cand in (c, unicodedata.normalize('NFKC', c)):
            if cand in self.cmap:
                return self.cmap[cand]
            if len(cand) == 1 and ' ' < cand < '\x7f':          # ASCII -> full width
                fw = chr(ord(cand) + 0xFEE0)
                if fw in self.cmap:
                    return self.cmap[fw]
        code = self.virtual.get(c)
        if code is None:
            fb = FALLBACK.get(c)
            fb_code = self.cmap.get(fb) if fb else None
            if fb_code is None:
                fb_code = like if like is not None and like < VIRTUAL else self.cmap.get('〓', 614)
            code = VIRTUAL + len(self.game.virtual_glyphs)
            self.game.virtual_glyphs[code] = fb_code
            self.game.font.to_char[code] = c
            self.virtual[c] = code
        return code

    # ------------------------------------------------------------ apply
    def apply(self, chap, prog):
        """Patch prog (a fresh Program of chapter chap) in place. Returns the number of edits."""
        el = self.edits(chap)
        if not el:
            return 0
        toks = ds_tokens(prog, self.game.font)
        L = list(prog.instrs)
        self._forced = early_breaks(L)
        self._special = special_glyphs(L)
        tok = [L[t[0]] for t in toks]          # the token glyph instrs (identity survives edits)
        dirty = set()                          # ids of instrs whose paragraph needs re-wrapping
        pars = []
        for e in sorted(el, key=lambda e: (e.a1, e.a2), reverse=True):
            if e.own_par and e.a1 < len(tok):
                pars.append((tok[e.a1], e))
                continue
            olds = tok[e.a1:e.a2]
            like = olds[0].glyph if olds else (tok[e.a1 - 1].glyph if e.a1 > 0 else None)
            codes = [self.glyph_for(ch[1], olds[k].glyph if k < len(olds) else like)
                     for k, ch in enumerate(e.new)]
            n = min(len(olds), len(codes))
            for k in range(n):
                olds[k].glyph = codes[k]
            if len(olds) == len(codes):
                continue
            if len(olds) > len(codes):                       # drop the rest
                pos = L.index(olds[n])
                gone = set(map(id, olds[n:]))
                L = [x for x in L if id(x) not in gone]
                for q in (pos - 1, pos):
                    if 0 <= q < len(L):
                        dirty.add(id(L[q]))
                continue
            # add the rest after the last kept glyph (and its ruby / keyword ops)
            anchor = olds[n - 1] if n else (tok[e.a1 - 1] if e.a1 > 0 else None)
            if anchor is None:
                pos = L.index(tok[0])
            else:
                pos = L.index(anchor) + 1
                while pos < len(L) and L[pos].op in (0x44, 0x54):
                    pos += 1
            add = [Instr(None, 'G', (), c) for c in codes[n:]]
            L[pos:pos] = add
            dirty.update(map(id, add))
        for before, e in pars:
            self._insert_paragraph(L, before, e)
        prog.instrs = self._rewrap(L, dirty)
        reindex(prog)
        return len(el)

    def _insert_paragraph(self, L, before, e):
        """A whole book paragraph the DS cut: put it back as its own paragraph, ending on a
        click, just before the DS paragraph that starts with `before`."""
        i = L.index(before)
        a0 = next((a for a, b in paragraphs(L) if a <= i <= b), None)
        if a0 is None:
            return
        # after the paragraph's lead-in (page turn, staging), at its first line marker
        j = next((k for k in range(a0, i + 1) if L[k].op == 0x4f), None)
        if j is None:
            return
        args = L[j].args
        like = before.glyph
        body = [Instr(None, 0x4f, args)]
        if e.new and e.new[0][3]:
            body.append(Instr(None, 'G', (), 614))
        body += [Instr(None, 'G', (), self.glyph_for(ch[1], like)) for ch in e.new]
        body += [Instr(None, 0x42, ()), Instr(None, 0x40, ())]
        L[j:j] = body
        # long book paragraphs need wrapping too
        tmp = wrap_paragraph(body, self.char_of, {}, set())
        L[j:j + len(body)] = tmp

    def _rewrap(self, L, dirty):
        """Re-lay-out the columns of every paragraph that holds a dirty instruction."""
        if not dirty:
            return L
        targets = jump_targets(L)
        out, last = [], 0
        for a, b in paragraphs(L):
            seg = L[a:b + 1]
            if any(id(x) in dirty for x in seg) and not any(x.off in targets for x in seg):
                seg = wrap_paragraph(seg, self.char_of, self._forced, self._special)
            out.extend(seg)
            last = b + 1
        out.extend(L[last:])
        return out


def paragraphs(L):
    """[(first, last)] instruction index ranges of the paragraphs: each ends on the 40
    after a click that ends its column (42 40), or on a page-clearing click (48/49/4a)."""
    out, start, hard = [], 0, False
    for k, ins in enumerate(L):
        if ins.op in PAGE_CLEAR:
            out.append((start, k))
            start, hard = k + 1, False
        elif ins.op in HARD:
            hard = True
        elif ins.op == 'G':
            hard = False
        elif ins.op == 0x40 and hard:
            out.append((start, k))
            start, hard = k + 1, False
    return out


def jump_targets(L):
    """Byte offsets of every goto / gosub / thread target."""
    t = set()
    for ins in L:
        a = ins.args
        if ins.op in (0xb0, 0xb4) and len(a) >= 2:
            t.add(((a[0] | (a[1] << 16)) & 0xFFFFFF) * 2)
        elif ins.op in (0xb5, 0xb6) and len(a) >= 3:
            t.add(((a[1] | (a[2] << 16)) & 0xFFFFFF) * 2)
    return t


def column_cells(seg):
    cols, cur = [], 0.0
    for x in seg:
        if x.op == 'G':
            cur += _cells(x.glyph)
        elif x.op == 0x40 or x.op in PAGE_CLEAR:
            cols.append(cur)
            cur = 0.0
    return cols


def early_breaks(L):
    """{glyph id: blank columns after it} for the glyphs after which the DS ends a column
    by hand (early, before a 「 or after a 、, or with blank columns after it): a re-wrap
    keeps those breaks."""
    out, cells, lastg, prev, hard = {}, 0.0, None, None, False
    for x in L:
        if x.op == 'G':
            cells += _cells(x.glyph)
            lastg, hard = x, False
        elif x.op in PAGE_CLEAR:
            if lastg is not None:
                prev = lastg
            cells, lastg, hard = 0.0, None, False
        elif x.op in HARD:
            hard = True
        elif x.op == 0x40:
            if lastg is None:
                if prev is not None and id(prev) in out:
                    out[id(prev)] += 1                 # a blank column
            elif not hard and cells < COL_CELLS:
                out[id(lastg)] = 0
            elif not hard:
                out.setdefault(id(lastg), -1)          # full column: only blanks may follow
            if lastg is not None:
                prev = lastg
                if hard:
                    out.pop(id(lastg), None)
            cells, lastg, hard = 0.0, None, False
    return {k: v for k, v in out.items() if v >= 0 or v == -1}


def special_glyphs(L):
    """ids of the glyphs in columns longer than 15 cells (staff rolls and other special
    layouts): their paragraphs are never re-wrapped."""
    out, col = set(), []
    for x in L:
        if x.op == 'G':
            col.append(x)
        elif x.op == 0x40 or x.op in PAGE_CLEAR:
            if sum(_cells(g.glyph) for g in col) > COL_CELLS + 1:
                out.update(map(id, col))
            col = []
    return out


def wrap_paragraph(seg, char_of, forced=None, special=None):
    """Re-break a paragraph's columns (4f … 40 … 40) the way the DS does: 14 cells, one
    hanging 、。」』, a 縦中横 pair kept together, and the DS's own early breaks
    (`forced`: glyph ids, from early_breaks() on the unedited script) kept."""
    if forced is None:
        forced = early_breaks(seg)
    if special is None:
        special = special_glyphs(seg)
    if any(id(x) in special for x in seg):                     # special layouts: leave alone
        return seg
    fg = next((i for i, x in enumerate(seg) if x.op == 'G'), None)
    if fg is None or not (seg[-1].op == 0x40 or seg[-1].op in PAGE_CLEAR):
        return seg
    lead = seg[:fg]                       # up to the first glyph: as the DS has it (blank columns)
    marks = [x for x in lead if x.op == 0x4f]
    if not marks:
        return seg
    final = seg[-1]
    rest = [x for x in seg[fg:-1] if x.op == 0x4f]
    body = [x for x in seg[fg:-1] if x.op not in (0x40, 0x4f)]
    out = list(lead)
    cells, hung, last = 0.0, False, marks[-1]
    pending = None                        # a forced break (+ blank columns) before the next glyph
    for x in body:
        if x.op == 'G':
            g = x.glyph
            w = 1 if (g < VIRTUAL and g & 0xFF00 == 0x2100) else _cells(g)
            ch = char_of(g)
            brk = False
            if cells + w > COL_CELLS:
                if ch in HANG and not hung and cells <= COL_CELLS:
                    hung = True
                else:
                    brk = True
            blanks = 0
            if pending is not None and cells > 0:
                if pending >= 0:
                    brk = True
                blanks = max(0, pending) if pending >= 0 else 0
            if pending == -1 and brk:
                pass
            pending = None
            if brk:
                # break after the previous glyph and its ruby / keyword ops
                j = len(out)
                while j > 0 and out[j - 1].op not in ('G', 0x44, 0x54, 0x4f):
                    j -= 1
                last = rest.pop(0) if rest else Instr(None, 0x4f, last.args)
                add = [Instr(None, 0x40, ()), last]
                for _ in range(blanks):
                    last = rest.pop(0) if rest else Instr(None, 0x4f, last.args)
                    add += [Instr(None, 0x40, ()), last]
                out[j:j] = add
                cells, hung = 0.0, False
            cells += _cells(g)
            pending = forced.get(id(x))
        out.append(x)
    # markers of columns no longer needed go before the closing click, so their
    # line ids still pass (cards, radar)
    k = len(out)
    while k > 0 and out[k - 1].op != 'G':
        k -= 1
    out[k:k] = rest
    out.append(final)
    return out


def reindex(prog):
    """Rebuild a patched Program's offset and line tables."""
    prog.index = {}
    prog.line_index = {}
    for i, ins in enumerate(prog.instrs):
        if ins.off is not None:
            prog.index[ins.off] = i
        if ins.op == 0x4f:
            prog.line_index.setdefault(ins.args[0], i)

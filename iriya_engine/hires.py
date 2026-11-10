"""Window-resolution vertical text, using a system Japanese TrueType font.

Layout (column positions, line pitch, ruby placement, highlights) is exactly the
same as the DS bitmap renderer in scene.py; only the glyphs are drawn at the
window's native resolution, so text stays sharp at any window size.
"""
import os
import math
import pygame

from .scene import Scene, PAGE_W, VIEW_H, TEXT_TOP, ROLL_X0
from .textcodec import ruby_char
from .correct import VIRTUAL, FALLBACK

WIN_FONTS = os.path.join(os.environ.get('WINDIR', 'C:/Windows'), 'Fonts')
FONT_CANDIDATES = [
    # Windows (Meiryo is the default)
    os.path.join(WIN_FONTS, 'meiryo.ttc'),
    os.path.join(WIN_FONTS, 'BIZ-UDGothicR.ttc'),
    os.path.join(WIN_FONTS, 'YuGothM.ttc'),
    os.path.join(WIN_FONTS, 'msgothic.ttc'),
    # macOS
    '/System/Library/Fonts/ヒラギノ角ゴシック W4.ttc',
    '/System/Library/Fonts/Hiragino Sans GB.ttc',
    # Linux
    '/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc',
    '/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc',
    '/usr/share/fonts/truetype/fonts-japanese-gothic.ttf',
]

# Friendly Japanese names for common font files (others use the font's own family name).
FONT_LABELS = {
    'meiryo.ttc': 'メイリオ', 'meiryob.ttc': 'メイリオ　太字',
    'yugothm.ttc': '游ゴシック　Ｍ', 'yugothr.ttc': '游ゴシック', 'yugothb.ttc': '游ゴシック　太字',
    'yugothl.ttc': '游ゴシック　Ｌ', 'yumin.ttf': '游明朝', 'yumindb.ttf': '游明朝　Ｄ', 'yuminl.ttf': '游明朝　Ｌ',
    'biz-udgothicr.ttc': 'ＢＩＺ　ＵＤゴシック', 'biz-udgothicb.ttc': 'ＢＩＺ　ＵＤゴシック太',
    'biz-udminchom.ttc': 'ＢＩＺ　ＵＤ明朝', 'msgothic.ttc': 'ＭＳ　ゴシック', 'msmincho.ttc': 'ＭＳ　明朝',
    'uddigikyokashon-r.ttc': 'ＵＤデジタル教科書体', 'uddigikyokashon-b.ttc': 'ＵＤデジタル教科書体太',
}
_PREFERRED = ['meiryo.ttc', 'biz-udgothicr.ttc', 'yugothm.ttc', 'yugothr.ttc', 'msgothic.ttc',
              'biz-udminchom.ttc', 'yumin.ttf', 'msmincho.ttc', 'uddigikyokashon-r.ttc']
_font_list = None


def font_label(path):
    if not path:
        return 'なし'
    base = os.path.basename(path).lower()
    if base in FONT_LABELS:
        return FONT_LABELS[base]
    try:
        import pygame.freetype as ft
        ft.init()
        return ft.Font(path).name or os.path.splitext(base)[0]
    except Exception:
        return os.path.splitext(base)[0]


def list_japanese_fonts():
    """Installed fonts that contain kana and kanji: [(label, path)], Meiryo first."""
    global _font_list
    if _font_list is not None:
        return _font_list
    import pygame.freetype as ft
    ft.init()
    paths = set(p for p in FONT_CANDIDATES if os.path.exists(p))
    for name in pygame.font.get_fonts():
        p = pygame.font.match_font(name)
        if p and p.lower().endswith(('.ttf', '.ttc', '.otf')):
            paths.add(p)
    for d in (WIN_FONTS, os.path.join(os.environ.get('LOCALAPPDATA', ''), 'Microsoft/Windows/Fonts')):
        if os.path.isdir(d):
            for f in os.listdir(d):
                if f.lower().endswith(('.ttf', '.ttc', '.otf')):
                    paths.add(os.path.join(d, f))
    seen = {}
    for p in paths:
        key = os.path.normcase(os.path.abspath(p))
        if key in seen:
            continue
        try:
            f = ft.Font(p, 12)
            if any(m is None for m in f.get_metrics('あア漢「')):
                continue
        except Exception:
            continue
        seen[key] = (font_label(p), p)
    def order(item):
        label, path = item
        base = os.path.basename(path).lower()
        if base in _PREFERRED:
            return (0, _PREFERRED.index(base), '')
        if base in FONT_LABELS:
            return (1, 0, label)
        jp = 'jp' in label.lower() or 'japan' in label.lower() or any(ord(c) > 0x3000 for c in label)
        return (2 if jp else 3, 0, label.lower())
    items = sorted(seen.values(), key=order)
    counts = {}
    for label, _ in items:
        counts[label] = counts.get(label, 0) + 1
    _font_list = [((f'{label} ({os.path.splitext(os.path.basename(p))[0]})' if counts[label] > 1 else label), p)
                  for label, p in items]
    return _font_list


# Characters drawn rotated 90° clockwise in vertical text.
ROTATE = set('ー―‐－～〜…‥「」『』（）［］【】〔〕｛｝〈〉《》＝：；＿｜—-~()[]{}<>=')
# Punctuation that sits in the upper-right of the cell in vertical text.
CORNER = set('、。，．､｡')
SMALL = set('ぁぃぅぇぉっゃゅょゎァィゥェォッャュョヮヵヶ')

WHITE = (255, 255, 255)
HILITE = (255, 214, 120)


def find_font(user_path=None):
    for p in ([user_path] if user_path else []) + FONT_CANDIDATES:
        if p and os.path.exists(p):
            return p
    for name in ('meiryo', 'bizudgothic', 'yugothic', 'msgothic', 'notosanscjkjp',
                 'notosanscjk', 'ipagothic', 'takaogothic', 'hiraginosans'):
        p = pygame.font.match_font(name)
        if p:
            return p
    return None


class HiresText:
    def __init__(self, font_path):
        self.path = font_path
        self._fonts = {}
        self._glyphs = {}
        self._cols = {}

    @property
    def ok(self):
        return self.path is not None

    def emphasis_mark(self):
        """﹅ (sesame dot) when the font has it, else ・."""
        m = getattr(self, '_emph', None)
        if m is None:
            m = '・'
            try:
                import pygame.freetype as ft
                ft.init()
                if ft.Font(self.path, 12).get_metrics('﹅')[0] is not None:
                    m = '﹅'
            except (OSError, TypeError, IndexError, pygame.error):
                pass
            self._emph = m
        return m

    def _font(self, px):
        f = self._fonts.get(px)
        if f is None:
            f = pygame.font.Font(self.path, px)
            self._fonts[px] = f
        return f

    # ----------------------------------------------------------- glyphs
    def has_char(self, ch):
        try:
            return self._font(12).metrics(ch)[0] is not None
        except (pygame.error, IndexError, TypeError):
            return False

    def glyph(self, ch, cell, color, outline, upright=False):
        """Outlined glyph image for a `cell`-pixel square, vertical-text oriented.

        Returns (surface, offset) where offset is where the cell's top-left is
        inside the surface.
        """
        key = (ch, cell, color, outline, upright)
        g = self._glyphs.get(key)
        if g is not None:
            return g
        font = self._font(max(4, cell))
        fg = font.render(ch, True, color)
        bg = font.render(ch, True, (0, 0, 0))
        # Crop the line box down to the em square (ideographic em top ≈ ascent - 0.88 em).
        top = max(0, min(fg.get_height() - 1, font.get_ascent() - round(cell * 0.88)))
        w = fg.get_width()
        h = min(cell, fg.get_height() - top)
        area = pygame.Rect(0, top, w, h)
        fg = fg.subsurface(area).copy()
        bg = bg.subsurface(area).copy()
        if ch in ROTATE and not upright:
            fg = pygame.transform.rotate(fg, -90)
            bg = pygame.transform.rotate(bg, -90)
        # centre the em box in the cell
        ox = (cell - fg.get_width()) / 2
        oy = (cell - fg.get_height()) / 2
        if upright:                       # horizontal text (the staff roll): as the font has it
            pass
        elif ch in CORNER:
            ox += cell * 0.55
            oy -= cell * 0.55
        elif ch in SMALL:
            ox += cell * 0.1
            oy -= cell * 0.1
        r = outline
        surf = pygame.Surface((fg.get_width() + 2 * r, fg.get_height() + 2 * r), pygame.SRCALPHA)
        if r:
            for dy in range(-r, r + 1):
                for dx in range(-r, r + 1):
                    if dx * dx + dy * dy <= r * r + r:
                        surf.blit(bg, (r + dx, r + dy))
        surf.blit(fg, (r, r))
        g = (surf, (r - ox, r - oy))
        if len(self._glyphs) > 6000:
            self._glyphs.clear()
        self._glyphs[key] = g
        return g

    def pair_glyph(self, text, cell, color, outline):
        """Two half-width letters side by side, turned 90° clockwise like Latin text
        in vertical Japanese: the DS font draws English this way in a few symbol slots."""
        key = ('pair', text, cell, color, outline)
        g = self._glyphs.get(key)
        if g is not None:
            return g
        c0 = cell // 2 + outline
        surf = pygame.Surface((cell + 2 * c0, cell + 2 * c0), pygame.SRCALPHA)
        for c, dx in zip(text, (-cell / 4, cell / 4)):
            if c != ' ':
                img, (ox, oy) = self.glyph(c, cell, color, outline)
                surf.blit(img, (round(c0 + dx - ox), round(c0 - oy)))
        g = (pygame.transform.rotate(surf, -90), (c0, c0))
        self._glyphs[key] = g
        return g

    # ----------------------------------------------------------- columns
    def column_surface(self, game, col, k, color_for=None):
        """A column rendered at scale k; local x origin is the column x - 2."""
        owned = tuple(color_for(m[2]) for m in col.marks if len(m) > 2) if color_for else ()
        sig = (k, len(col.glyphs), len(col.ruby), len(col.marks), owned)
        hit = self._cols.get(id(col))
        if hit is not None and hit[0] == sig and hit[1] is col:
            return hit[2]
        glyphs, ruby = Scene.layout_column(col)
        surf = pygame.Surface((math.ceil(26 * k), math.ceil(VIEW_H * k)), pygame.SRCALPHA)
        cell = max(4, round(12 * k))
        rcell = max(3, round(8 * k))
        outline = max(1, round(k * 0.45))
        for gi, y, kid, xo in glyphs:
            half = 0x2000 <= gi < 0x2300
            ch = chr(gi & 0xFF) if half else game.font.char(gi)
            if ch in ('\ufffd', '　', ' ', ''):
                continue
            color = color_for(kid) if color_for else (WHITE if kid is None else HILITE)
            if gi >= VIRTUAL and not self.has_char(ch):    # corrected text: a form the font lacks
                ch = FALLBACK.get(ch, ch)
            if len(ch) == 2:                  # a slot the DS font redraws as two sideways letters
                img, (ox, oy) = self.pair_glyph(ch, cell, color, outline)
            else:
                img, (ox, oy) = self.glyph(ch, cell, color, outline)
            # half-width glyphs render centred in a full cell; move them to their half
            dx = (xo - 3) if half else 0
            surf.blit(img, (round((2 + dx) * k - ox), round(y * k - oy)))
        for code, dx, y in ruby:
            ch = ruby_char(code, game.id)
            if ch == '‐':
                ch = 'ー'
            elif ch == '＼':                  # the DS's 傍点 stroke: a sesame mark in a real font
                ch = self.emphasis_mark()
            img, (ox, oy) = self.glyph(ch, rcell, WHITE, max(1, outline - 1))
            surf.blit(img, (round((2 + dx) * k - ox), round(y * k - oy)))
        if len(self._cols) > 400:
            self._cols.clear()
        self._cols[id(col)] = (sig, col, surf)
        return surf

    def row_surface(self, game, col, k):
        """A staff-roll line as a horizontal row at scale k; local origin is the page's x 0, row y - 2."""
        sig = ('row', k, len(col.glyphs))
        hit = self._cols.get(id(col))
        if hit is not None and hit[0] == sig and hit[1] is col:
            return hit[2]
        glyphs, _ = Scene.layout_column(col)
        surf = pygame.Surface((math.ceil(PAGE_W * k), math.ceil(18 * k)), pygame.SRCALPHA)
        cell = max(4, round(12 * k))
        outline = max(1, round(k * 0.45))
        for gi, y, kid, xo in glyphs:
            half = 0x2000 <= gi < 0x2300
            ch = chr(gi & 0xFF) if half else game.font.char(gi)
            if ch in ('�', '　', ' ', ''):
                continue
            if gi >= VIRTUAL and not self.has_char(ch):
                ch = FALLBACK.get(ch, ch)
            img, (ox, oy) = self.glyph(ch, cell, WHITE, outline, upright=True)
            if half:                          # rendered centred in a full cell; its half is the left one
                ox += 3 * k
            surf.blit(img, (round((ROLL_X0 + y - TEXT_TOP) * k - ox), round(2 * k - oy)))
        if len(self._cols) > 400:
            self._cols.clear()
        self._cols[id(col)] = (sig, col, surf)
        return surf

    def draw_page(self, window, scene, columns, origin, k, alpha=255):
        """Draw a page of columns with the page's top-left at window pixel `origin`."""
        if alpha <= 0:
            return
        ox, oy = origin
        clip = window.get_clip()
        page = pygame.Rect(round(ox), round(oy), round(PAGE_W * k), round(VIEW_H * k))
        window.set_clip(page.clip(clip))
        try:
            roll = scene.scroll_mode and columns is scene.columns
            for x, col in scene.placed_columns(columns):
                if roll:                      # x is the row's y
                    s = self.row_surface(scene.game, col, k)
                    pos = (round(ox), round(oy + (x - 2) * k))
                else:
                    s = self.column_surface(scene.game, col, k, scene.keyword_color)
                    pos = (round(ox + (x - 2) * k), round(oy))
                if alpha < 255:
                    # fade through a copy: surface alpha on per-pixel-alpha
                    # surfaces is unreliable on some SDL builds
                    t = s.copy()
                    t.fill((255, 255, 255, int(alpha)), special_flags=pygame.BLEND_RGBA_MULT)
                    window.blit(t, pos)
                else:
                    window.blit(s, pos)
        finally:
            window.set_clip(clip)

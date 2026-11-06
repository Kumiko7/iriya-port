"""In-game HUD reproduced from the DS layout.

Left page (DS top screen), bottom: the game's small logo, the chapter name
plate, "Page n / N" and the keyword radar orb. Right page (touch screen),
bottom: four icon buttons (backlog, card list, hide text, menu) and the round
"next" / AUTO button. All art comes from com/ and title/ in the ROM.
"""
import numpy as np
import pygame

from . import nnt
from .assets import _surface

PAGE_W, PAGE_H = 192, 256

# positions measured against the DS (DeSmuME captures, book view)
ICON_Y = 224
ICON_X = (20, 50, 80, 110)
ICON_ACTIONS = ('log', 'cards', 'hide', 'menu')    # as on the DS: backlog, card list, hide the text, menu
ORB_POS = (146, 218)
RADAR_POS = (171, 235)
LOGO_POS = (5, 204)
PAGE_WORD_X = 112          # 'Page' stays put; the numbers are right-aligned at PAGE_NUM_END
PAGE_NUM_END = 186
PAGE_SLASH_X = 160
PAGE_Y = 219
# radar pop when its level goes up: orb width per frame (the cell's orb is 12px wide),
# measured on DeSmuME; it is centred on the orb and the new level shows at once
RADAR_POP = (16, 17, 17, 17, 19, 19, 21, 21, 23, 24, 23, 21, 21, 19, 19, 17, 17, 17)


def _iconnew(game):
    """Cut com/iconnew.nnt: 4 icons x (disabled, enabled) + round buttons."""
    d = game.rom.read('com/iconnew.nnt')
    h = nnt.header(d)
    pal = nnt.palette(d, h['poff'], h['psize'])
    raw = np.frombuffer(d, np.uint8, h['isize'], h['ioff'])
    px = nnt._unpack4(raw) if h['isize'] * 8 // (h['tw'] * h['th']) == 4 else raw
    tiles = px.reshape(-1, 8, 8)

    def rgba(idx):
        return np.dstack([pal[idx], np.where(idx == 0, 0, 255).astype(np.uint8)])

    # top 64 storage rows: eight 16x16 OBJs, two per row
    top = np.zeros((64, 32), np.uint8)
    for k in range(8):
        obj = tiles[k * 4:(k + 1) * 4].reshape(2, 2, 8, 8).transpose(0, 2, 1, 3).reshape(16, 16)
        top[(k // 2) * 16:(k // 2) * 16 + 16, (k % 2) * 16:(k % 2) * 16 + 16] = obj
    top_img = np.rot90(rgba(top), 1)           # 64 x 32 (w x h), upright
    icons_off = [_surface(np.ascontiguousarray(top_img[0:16, i * 16:i * 16 + 16])) for i in range(4)]
    icons_on = [_surface(np.ascontiguousarray(top_img[16:32, i * 16:i * 16 + 16])) for i in range(4)]
    full = game.image('com/iconnew.nnt', rotate=True)  # 256 x 32
    orbs = [full.subsurface((64 + 32 * i, 0, 32, 32)) for i in range(6)]
    # orbs: 0 next (grey), 1 next, 2 auto off, 3..5 auto animation
    return icons_off, icons_on, orbs


def _strip_glyphs(surf):
    """Split a horizontal glyph strip into [(x0, x1)] runs of opaque columns."""
    w, h = surf.get_size()
    alpha = pygame.surfarray.pixels_alpha(surf)
    cols = alpha.max(axis=1) > 0
    del alpha
    runs, start = [], None
    for x in range(w):
        if cols[x] and start is None:
            start = x
        elif not cols[x] and start is not None:
            runs.append((start, x))
            start = None
    if start is not None:
        runs.append((start, w))
    return runs


class Hud:
    def __init__(self, game):
        self.game = game
        self.icons_off, self.icons_on, self.orbs = _iconnew(game)
        self.radar = [game.image('com/radar.nnt', rotate=True).subsurface((16 * i, 0, 16, 16)) for i in range(4)]
        self.page_strip = game.image('com/page.nnt', rotate=True)
        runs = _strip_glyphs(self.page_strip)
        self.digits = runs[-10:] if len(runs) >= 12 else []
        self.slash = runs[-11] if len(runs) >= 12 else None
        self.page_word = (runs[0][0], runs[-12][1]) if len(runs) >= 12 else None
        self.logo = game.image('title/name.nnt', rotate=True) if game.has('title/name.nnt') else None
        self.kira = game.image('com/kira.nnt', rotate=True) if game.has('com/kira.nnt') else None
        self.new = game.image('com/new.nnt', rotate=True) if game.has('com/new.nnt') else None
        self._plates = {}
        self.t = 0
        self.radar_shown = None
        self.radar_pop = 0

    def plate(self, chap):
        if chap not in self._plates:
            p = f'title/name{int(chap, 36)}.nnt'
            if self.game.has(p):
                self._plates[chap] = self.game.image(p, rotate=True)
            elif chap in getattr(self.game, 'extra_titles', {}):
                self._plates[chap] = self._text_plate(self.game.extra_titles[chap])
            else:
                self._plates[chap] = None
        return self._plates[chap]

    def _text_plate(self, title):
        """Name plate for chapters with no plate art (the bonus chapters)."""
        from .scene import Scene
        idxs = [i for i in self.game.text_glyphs(title) if i is not None]
        surf = pygame.Surface((12 * len(idxs) + 4, 15), pygame.SRCALPHA)
        for k, gi in enumerate(idxs):
            if gi != 614:
                Scene._outlined(surf, self.game.glyph(gi, vertical=False), 2 + 12 * k, 1,
                                (255, 255, 255), (0, 0, 0))
        return surf

    # ------------------------------------------------------------ drawing
    def _number(self, surf, n, x, y):
        """Digits right-aligned in 6px cells ending at x, as on the DS (a narrow
        digit such as 1 sits centred in a 5px slot); returns the left x."""
        s = self.page_strip
        for k, ch in enumerate(reversed(str(n))):
            a, b = self.digits[int(ch)]
            cell = x - 6 * (k + 1)
            surf.blit(s, (cell + max(0, (5 - (b - a)) // 2), y), (a, 0, b - a, s.get_height()))
        return x - 6 * len(str(n))

    def draw_left(self, page, chap, cur_page, max_page, radar_level):
        if self.logo is not None:
            page.blit(self.logo, LOGO_POS)
        pl = self.plate(chap)
        if pl is not None:
            page.blit(pl, (10, PAGE_H - pl.get_height() - 5))
        if self.digits and max_page:
            s = self.page_strip
            y = PAGE_Y
            self._number(page, max_page, PAGE_NUM_END, y)
            a, b = self.slash
            page.blit(s, (PAGE_SLASH_X, y), (a, 0, b - a, s.get_height()))
            self._number(page, max(1, cur_page), PAGE_SLASH_X - 2, y)
            a, b = self.page_word
            page.blit(s, (PAGE_WORD_X, y), (a, 0, b - a, s.get_height()))
        if radar_level is None:               # Game II has no keyword radar
            return
        if self.radar_shown is not None and radar_level > self.radar_shown:
            self.radar_pop = len(RADAR_POP)
        self.radar_shown = radar_level
        img = self.radar[radar_level]
        if self.radar_pop > 0:
            w = RADAR_POP[len(RADAR_POP) - self.radar_pop]
            self.radar_pop -= 1
            size = round(16 * w / 12)
            big = pygame.transform.scale(img, (size, size))
            cx, cy = RADAR_POS[0] + 8, RADAR_POS[1] + 8
            page.blit(big, (cx - size // 2, cy - size // 2))
        else:
            page.blit(img, RADAR_POS)

    def draw_right(self, page, waiting, auto, enabled=True):
        self.t += 1
        icons = self.icons_on if enabled else self.icons_off
        if not auto:                  # the DS hides the four buttons in auto mode
            for x, ic in zip(ICON_X, icons):
                page.blit(ic, (x, ICON_Y))
        if auto:
            page.blit(self.orbs[3 + (self.t // 8) % 3], ORB_POS)
        elif waiting:
            page.blit(self.orbs[1], ORB_POS)

    # ------------------------------------------------------------ input
    @staticmethod
    def hit_right(pos):
        x, y = pos
        for ix, act in zip(ICON_X, ICON_ACTIONS):
            if ix - 2 <= x < ix + 18 and ICON_Y - 2 <= y < ICON_Y + 18:
                return act
        ox, oy = ORB_POS
        if (x - ox - 16) ** 2 + (y - oy - 16) ** 2 <= 16 * 16:
            return 'orb'
        return None

    @staticmethod
    def hit_left(pos):
        x, y = pos
        rx, ry = RADAR_POS
        if rx - 4 <= x < rx + 20 and ry - 4 <= y < ry + 20:
            return 'radar'
        return None

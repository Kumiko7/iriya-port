"""Scene state + rendering for one chapter.

Coordinates: the DS is held sideways like a book. Everything here is in
"display space": a 464x256 strip = left page (DS top screen, 192 wide),
80px hidden hinge gap, right page (DS touch screen, 192 wide). Background
art is stored rotated in the ROM and is turned upright on load.
"""
import math
import random
import logging
import numpy as np
import pygame

from . import nnt
from .vm import Program

log = logging.getLogger('scene')

VIEW_W, VIEW_H = 464, 256
# The DS draws a face image 2px right of and 7px above the 8b position inside its
# window (measured on DeSmuME against every window and sheet in chapters I-0, II-0).
FACE_IMG_DX, FACE_IMG_DY = 2, -7
PAGE_W = 192
RIGHT_X = 272

# text layout on the right page
COLS_PER_PAGE = 8            # the DS shows 8 columns (9-slot ring: 8 + the one scrolling out)
COL_PITCH = 21               # 0x1500 in 8.8 fixed point (0x200e1cc)
COL_X0 = 164                 # 0xb9 - 21: x of the first (rightmost) column (0x201fdac)
CHAR_PITCH = 12
TEXT_TOP = 12
TEXT_BOTTOM = TEXT_TOP + 180
LOAD_BYTES_PER_FRAME = 9000  # DS image loading speed (see op 70)
TEXT_CLIP_X = 184            # nothing of the text is drawn right of this (DeSmuME captures)  # ruby is kept inside [TEXT_TOP, TEXT_BOTTOM] (0x2020070)

# DS timings (frames at 60 fps), and the quicker ones the port used before.
CHAR_FRAMES = 4              # default per-character delay (0x200e40c); op a1 picks 0/2/8/32
SCROLL_FRAMES_DS, SCROLL_FRAMES_FAST = 20, 16    # column scroll (0x200dd18)
DISSOLVE_FRAMES_DS, DISSOLVE_FRAMES_FAST = 32, 20  # 4a page dissolve: alpha 255 -> 0 by 8 (0x200e930)
SLIDE_FRAMES = 20            # 49 page slide (0x200eea4)

FACE_W, FACE_H = 176, 56


def _reset_alpha(surf):
    """Undo a set_alpha(n). set_alpha(None) would also switch off per-pixel
    alpha blending on a surface that has its own transparency (pygame 2)."""
    surf.set_alpha(255 if surf.get_flags() & pygame.SRCALPHA else None)


class Tween:
    __slots__ = ('v', 'a', 'b', 't', 'dur')

    def __init__(self, v=0.0):
        self.v = self.a = self.b = float(v)
        self.t = self.dur = 0

    def to(self, target, frames):
        if frames <= 0:
            self.v = self.a = self.b = float(target)
            self.dur = 0
        else:
            self.a, self.b, self.t, self.dur = self.v, float(target), 0, frames

    def update(self, fast=False):
        if self.dur:
            self.t += 1
            if fast or self.t >= self.dur:
                self.v = self.b
                self.dur = 0
            else:
                self.v = self.a + (self.b - self.a) * self.t / self.dur

    @property
    def busy(self):
        return self.dur > 0


class Layer:
    def __init__(self, prio):
        self.img = None
        self.name = ''
        self.x = Tween(8)
        self.y = Tween(8)
        self.alpha = Tween(255)
        self.prio = prio
        self.shake = (0, 0, 0)  # axis (1=x 2=y 3=both), amplitude, frames (-1 = forever)
        self.shake_t = 0
        self.shake_off = (0, 0)
        self.loading = 0        # frames until the image op 70 asked for is in (DS loads asynchronously)


class Face:
    def __init__(self, fid):
        self.id = fid
        self.name = None
        self.sheet = None
        self.expr = None
        self.x, self.y = Tween(8), Tween(8)
        self.w, self.h = FACE_W, FACE_H
        # face image offset inside the window (op 8b); default centred
        self.ix = Tween((FACE_W - 128) // 2)
        self.iy = Tween((FACE_H - 64) // 2)
        self.prev_expr = None       # expression being cross-faded out (op e2)
        self.pending = None         # expression queued by e1 while the window is open
        self.xfade = Tween(1.0)
        self.tint = (0, 0, 0, 64)   # window darkening (op 8e: r, g, b, amount)
        # window shake (ops 86 / 8f): axis 1=x 2=y 3=both, amplitude, frames left (-1 forever), period
        self.shake = None
        self.shake_off = (0, 0)
        self.prio = 0
        self.alpha = Tween(0)
        self.order = 0


class Column:
    __slots__ = ('glyphs', 'ruby', 'marks', 'born', 'line', 'page')

    def __init__(self, line=0, page=0):
        self.glyphs = []   # font indices
        self.ruby = []     # (end_char, n_base, [ruby codes])
        self.marks = []    # (start, length, keyword id) highlights
        self.born = 0      # scroll-mode birth frame
        self.line = line   # script line id (op 4f arg 0)
        self.page = page   # book page number (op 4f arg 1)


class Scene:
    def __init__(self, game, audio, settings):
        self.game = game
        self.audio = audio
        self.settings = settings
        self.layers = [Layer(p) for p in (3, 4, 2, 5)]
        self.faces = {}
        self.face_counter = 0
        self.fade_color = (0, 0, 0)
        self.fade = Tween(0)
        self.fade_mode = 2
        self.columns = []
        self.page = None
        self.line_id = 0
        self.page_line = 0          # line id of the oldest column on screen (bookmarks)
        self.page_break_pending = False
        self.scroll = Tween(0)      # column scroll animation (1 -> 0) when a 9th column starts
        self.clear_fx = None        # ('slide' | 'fade', Tween) page-clear transition
        self.keywords = []          # keyword ids seen on the current screen
        self.collected = set()      # card ids the player already owns (shared with the app)
        self.instant = False        # reveal rest of text up to next wait
        self.waiting = None         # text wait opcode being shown
        self.history = []           # finished columns for the backlog
        self.history_breaks = []    # history indices where the page was cleared
        self.fast = False
        self.click_glyph_t = 0
        self._text_cache = None
        self._text_dirty = True
        self.text_alpha = Tween(255)
        self.speed_mode = 0         # script-controlled reveal speed (op a1)
        self._char_acc = 0.0
        self.hide_text = False      # user toggle (view background)
        self.bgm = None             # (strm id) requested by the script
        self.bgm_on = False
        self.bgm_ctx = None         # (game, chapter, n): which op 96 of the chapter, so the
        self.bgm_seq = 0            # alternative soundtrack picks the same track on every replay
        self.chapter = None
        self._bgm_plan = None
        self.bgm_state = 0          # 0 none, 1 prepared by 96, 2 faded in by 90
        self.bgm_master = 1.0
        self.mute_audio = False     # true while fast-forwarding to a bookmark
        self.scroll_mode = False    # staff roll: columns stream across the page
        self.scroll_frames = 23     # frames per column in scroll mode
        self.scroll_t = 0
        self.hires = False          # text drawn by the window-resolution renderer instead
        # where each message (text between two clicks) started: (line id, clicks
        # already released inside that line) -- used to step back one message
        self.seg_starts = []
        self._seg_mark = True
        self.clicks_in_line = 0

    # =============================================================== text
    @property
    def fast_fx(self):
        return bool(self.settings.get('fast_fx', False))

    @property
    def SCROLL_FRAMES(self):
        return SCROLL_FRAMES_FAST if self.fast_fx else SCROLL_FRAMES_DS

    def begin_line(self, line_id, page, link):
        """Op 4f: the script's line marker (line id, book page). Returns frames to wait.

        On the DS this only records the line and page (bookmarks, the page
        counter); columns are started by op 40. Scripts start a page with a 4f
        and no 40, so an empty page gets its first column here.
        """
        if line_id != self.line_id:
            self.clicks_in_line = 0
        self.line_id = line_id
        self.page = page
        if self.scroll_mode:
            self.page_line = line_id
            col = Column(line_id, page)
            col.born = self.scroll_t
            self.columns.append(col)
            self.columns = [c for c in self.columns if self._scroll_x(c) < PAGE_W + 4]
            self._text_dirty = True
            return 0
        if not self.columns:
            self.page_line = line_id
            self.columns.append(Column(line_id, page))
            self._text_dirty = True
        elif not self.columns[-1].glyphs:
            col = self.columns[-1]
            col.line, col.page = line_id, page
            if len(self.columns) <= COLS_PER_PAGE:
                self.page_line = self.columns[0].line
            else:
                self.page_line = self.columns[-COLS_PER_PAGE].line
        return 0

    def new_column(self):
        """Op 40 (word 0x4001): start the next column. Returns frames the script waits.

        Like the DS (0x200dd48), once 8 columns are on the page the text scrolls
        over by one column. The script keeps typing while it scrolls; it only
        waits when a new column starts while an earlier scroll is still running.
        """
        if self.scroll_mode:
            return 0
        self.columns.append(Column(self.line_id, self.page or 0))
        self._text_dirty = True
        if len(self.columns) <= COLS_PER_PAGE:
            if self.text_alpha.busy and not self.fast:
                return self.text_alpha.dur - self.text_alpha.t
            return 0
        if len(self.columns) > 64:          # keep the on-screen list small
            self.history.extend(self.columns[:-COLS_PER_PAGE - 1])
            del self.columns[:-COLS_PER_PAGE - 1]
            self._trim_history()
        if self.fast or self.instant_scroll():
            self.scroll.to(0, 0)
            return 0
        remaining = self.scroll.dur - self.scroll.t if self.scroll.busy else 0
        if self.text_alpha.busy:
            remaining = max(remaining, self.text_alpha.dur - self.text_alpha.t)
        start = self.scroll.v + 1.0 if self.scroll.busy else 1.0
        self.scroll.to(start, 0)
        self.scroll.to(0.0, remaining + self.SCROLL_FRAMES)
        # measured on DeSmuME: a break that starts a scroll holds the script one
        # frame longer than a plain column break
        return remaining if remaining else 2

    def instant_scroll(self):
        return self.char_frames() == 0 and self.speed_mode == 0

    def char_frames(self):
        return float(self.settings.get('char_frames', CHAR_FRAMES))

    def _scroll_x(self, col):
        return -14 + (self.scroll_t - getattr(col, 'born', 0)) * COL_PITCH / max(1, self.scroll_frames)

    def _has_text(self):
        return any(c.glyphs and any(g != 614 for g in c.glyphs) for c in self.columns)

    def add_glyph(self, idx):
        if not self.columns:
            self.columns.append(Column())
        self.columns[-1].glyphs.append(idx)
        self._text_dirty = True
        if self._seg_mark and idx != 614:
            self._seg_mark = False
            self.seg_starts.append((self.line_id, self.clicks_in_line))
            if len(self.seg_starts) > 4000:
                del self.seg_starts[:1000]
        if self.instant or self.fast or idx & 0xFF00 == 0x2100:   # 縦中横 left half: no delay
            return 0
        # op a1 modes, relative to the DS default of 4 frames: 1 = 0, 2 = 2, 3 = 8, 4 = 32
        base = self.char_frames()
        mult = {0: 1.0, 1: 0.0, 2: 0.5, 3: 2.0, 4: 8.0}.get(self.speed_mode, 1.0)
        if self.speed_mode and base == 0:
            base = float(CHAR_FRAMES)
        self._char_acc += base * mult
        d = int(self._char_acc)
        self._char_acc -= d
        if d and self.text_alpha.busy:       # a2 fade: typing waits for it (58f state 3)
            d = max(d, self.text_alpha.dur - self.text_alpha.t)
        return d

    def add_ruby(self, code, chars):
        if not self.columns:
            return
        col = self.columns[-1]
        n_base = max(1, (code >> 8) // 2)
        col.ruby.append((len(col.glyphs), n_base, list(chars)))
        self._text_dirty = True

    def keyword(self, length, kid):
        """Highlight the last `length` glyphs (may span earlier columns) as card keyword `kid`."""
        rest = length
        for col in reversed(self.columns[-3:]):
            n = len(col.glyphs)
            take = min(rest, n)
            if take:
                col.marks.append((n - take, take, kid))
            rest -= take
            if rest <= 0:
                break
        if kid not in self.keywords:
            self.keywords.append(kid)
        self._text_dirty = True

    def text_wait(self, op):
        self.waiting = op
        self.instant = False
        self.click_glyph_t = 0

    def after_click(self, op, fast=False):
        """Called when a click releases a text wait. Returns frames to wait (transition)."""
        self.waiting = None
        self.instant = False
        self.page_break_pending = False
        self._seg_mark = True
        self.clicks_in_line += 1
        if op == 0x48 or (op in (0x49, 0x4a) and (fast or self.fast)):
            self._flush_page()
        elif op in (0x49, 0x4a):
            # 49: the page slides away; 4a: the page dissolves (DS transition type 2)
            n = SLIDE_FRAMES if op == 0x49 else (DISSOLVE_FRAMES_FAST if self.fast_fx else DISSOLVE_FRAMES_DS)
            t = Tween(0.0)
            t.to(1.0, n)
            self.clear_fx = ('slide' if op == 0x49 else 'fade', t)
            return n
        return 0

    def skip_reveal(self):
        self.instant = True

    def _flush_page(self):
        if self._has_text() and not self.scroll_mode:
            self.history.extend(self.columns)
            self._trim_history()
        self.history_breaks.append(len(self.history))
        self.columns = []
        self.keywords = []
        self.clear_fx = None
        self.scroll.to(0, 0)
        self._text_dirty = True

    def _trim_history(self):
        if len(self.history) > 3000:
            cut = len(self.history) - 2400
            del self.history[:cut]
            self.history_breaks = [b - cut for b in self.history_breaks if b > cut]

    def backlog_pages(self):
        """History + current screen split into book pages of up to 8 columns."""
        cols = self.history + ([] if self.scroll_mode else self.columns)
        breaks = sorted(set(b for b in self.history_breaks if 0 < b < len(cols)))
        pages, start = [], 0
        for b in breaks + [len(cols)]:
            seg = cols[start:b]
            for k in range(0, len(seg), COLS_PER_PAGE):
                pages.append(seg[k:k + COLS_PER_PAGE])
            start = b
        return [p for p in pages if p]

    # =============================================================== ops
    def op(self, op, a, vm, fast):
        L = self.layers
        v = vm.val
        if op == 0x70:  # load image into layer
            li = a[0] & 3
            name = Program.string_arg(a, 4)
            lay = L[li]
            lay.x.to(v(a[2]), 0)
            lay.y.to(v(a[3]), 0)
            lay.shake = (0, 0, 0)
            if (li != 0 and lay.img is not None and lay.alpha.b >= 255 and (lay.alpha.busy or lay.alpha.v > 0)
                    and vm.swap_pending()):
                # a crossfade is still bringing this layer in (read faster than the DS
                # pace, the script loads the next picture before the swap): let the new
                # picture take over the fade instead of being swapped in invisible
                pass
            else:
                lay.alpha.to(0, 0)    # a loaded layer starts transparent; 73 shows it (0x205e888)
            if name:
                lay.img = self.game.bg(name)
                lay.name = name
                if lay.img is None:
                    log.warning('missing image %s', name)
                # DS: op 70 queues an asynchronous card read + decode (0x201022c); the
                # layer shows, fades and moves only once it is in, while the script
                # runs on. Measured on DeSmuME at about 9 KB of .nnt per frame.
                lay.loading = 0 if fast else self.game.bg_bytes(name) // LOAD_BYTES_PER_FRAME + 1
            else:
                lay.img, lay.name = None, ''
        elif op == 0x71:
            L[a[0] & 3].prio = v(a[1])
        elif op == 0x73:
            L[a[0] & 3].alpha.to(max(0, min(255, v(a[1]))), 0 if fast else vm.fx_frames(a[2]))
        elif op == 0x74:
            lay = L[a[0] & 3]
            n = 0 if fast else vm.fx_frames(a[3])
            lay.x.to(v(a[1]), n)
            lay.y.to(v(a[2]), n)
        elif op == 0x75:  # (layer, axis, amp, frames) as 0x2010808; 0xFFFF = until stopped
            axis, amp = v(a[1]), v(a[2])
            n = -1 if a[3] >= 0x8000 else vm.fx_frames(a[3])
            if axis not in (1, 2, 3) or amp < 0:
                n = 0
            lay = L[a[0] & 3]
            lay.shake = (axis, amp, n) if n else (0, 0, 0)
            lay.shake_t = 0
            lay.shake_off = (0, 0)
        elif op == 0x78:
            lay = L[a[0] & 3]
            lay.img, lay.name = None, ''
            lay.alpha.to(0, 0)
        elif op == 0x7d:
            # (a, b): layer b becomes layer a, with its opacity and priority; the
            # old layer a is released (0x2010970)
            i, j = a[0] & 3, a[1] & 3
            li, lj = L[i], L[j]
            if lj.img is None and li.img is not None:
                # nothing to move in: two crossfade threads overlapped (the player
                # read on before the first finished) and the first already did the
                # swap. The DS would release layer a and go black; keep it instead.
                return
            for attr in ('img', 'name', 'x', 'y', 'alpha', 'shake', 'shake_t', 'shake_off', 'prio', 'loading'):
                av, bv = getattr(li, attr), getattr(lj, attr)
                setattr(li, attr, bv)
                setattr(lj, attr, av)
            lj.img, lj.name = None, ''
            lj.alpha.to(0, 0)
            lj.shake = (0, 0, 0)
        # ------------------------------------------------ screen fades
        elif op == 0x66:
            # fade colour (0x201bfd8): loaded into the OBJ palette the fade and wipe
            # sprites draw with, so it holds until the next 66
            self.fade_color = (a[0] & 255, a[1] & 255, a[2] & 255)
        elif op == 0x67:
            # fade depth (0x201c020): every reader only tests it against 0 (OBJ priority
            # 1 or 2), and the scripts only use 1 and 2, so it changes nothing here: the
            # fades always stay under the text and the HUD
            pass
        elif op in (0x62, 0x63):
            # (type, frames): 63 fades out from clear, 62 in from fully covered (both
            # restart, so 66 + 62 alone is a flash). Type 1 is the diagonal sprite wipe,
            # other types a 16-step fade; 0 frames means 60 (0x201be3c / 0x201beb8).
            self.fade_mode = v(a[0])
            n = vm.fx_frames(a[1]) if vm.frames(a[1]) else vm.fx_frames(60)
            self.fade.to(0.0 if op == 0x63 else 1.0, 0)
            self.fade.to(1.0 if op == 0x63 else 0.0, 0 if fast else n)
        # ------------------------------------------------ faces
        elif 0x80 <= op <= 0x8f or 0xe0 <= op <= 0xe4:
            self._face_op(op, a, vm, fast)
        # ------------------------------------------------ text attributes
        elif op == 0xa1:
            self.speed_mode = v(a[0])
        elif op in (0x5a, 0x5b):
            self.scroll_mode = bool(v(a[0]))
            self.columns = []
            self._text_dirty = True
        elif op == 0xa6:
            self.scroll_frames = max(1, v(a[0]))
        elif op == 0xa2:
            self.text_alpha.to(max(0, min(255, v(a[0]))), 0 if fast else vm.fx_frames(a[1]))
        # ------------------------------------------------ sound
        elif op == 0x96:  # prepare a track (silent until 90), master volume back to full
            self.bgm, self.bgm_on = a[0], True
            self.bgm_state, self.bgm_master = 1, 1.0
            self.bgm_seq += 1
            self.bgm_ctx = (self.game.id, self.chapter, self.bgm_seq)
            if not self.mute_audio:
                self.audio.bgm_prepare(a[0], self.bgm_ctx)
                if self.audio.alt_on:
                    if self._bgm_plan is None:      # the chapter's op 96 tracks, in order
                        self._bgm_plan = [i.args[0] for i in vm.prog.instrs if i.op == 0x96 and i.args]
                    self.audio.prefetch(self._bgm_plan[self.bgm_seq:self.bgm_seq + 2], self.bgm_ctx)
        elif op == 0x90:  # fade the prepared track in from silence; under 1 frame = at once
            if self.bgm_state == 1:
                self.bgm_state = 2
                if not self.mute_audio:
                    self.audio.bgm_fade_in(0 if fast else vm.fx_frames(a[0]))
        elif op == 0x91:  # fade out and stop a playing track
            if self.bgm_state == 2:
                self.bgm_state = 0
                self.bgm_on = False
                if not self.mute_audio:
                    self.audio.bgm_fade_out(0 if fast else vm.fx_frames(a[0]))
        elif op == 0x93:  # master volume (vol 0..127, frames)
            self.bgm_master = max(0.0, min(1.0, v(a[0]) / 127.0))
            if not self.mute_audio:
                self.audio.master_to(self.bgm_master, 0 if fast else vm.fx_frames(a[1]))
        elif op == 0x9e:
            if not self.mute_audio:
                self.audio.load_se(a[0], a[1])
        elif op == 0x98:
            if not fast and not self.mute_audio:
                self.audio.play_se(a[0], vm.fx_frames(a[1]))
        elif op == 0x99:
            if not self.mute_audio:
                self.audio.stop_se(a[0], 0 if fast else vm.fx_frames(a[1]))
        elif op == 0x9b:
            if not self.mute_audio:
                self.audio.se_volume_to(a[0], min(1.0, v(a[1]) / 127.0), 0 if fast else vm.fx_frames(a[2]))
        elif op in (0x55, 0x5c, 0x9d, 0x9f, 0xbc, 0x7f):
            pass  # flags / minor sprite tweaks not reproduced
        else:
            vm.unknown[op] = vm.unknown.get(op, 0) + 1

    FACE_SLOT_Y = {1: 80, 2: 150}   # op 80's second argument (0x2017400); otherwise y 8

    def _face_op(self, op, a, vm, fast):
        """Face windows as the DS runs them (0x2017008..0x2018a74).

        Up to 3 windows. 80 creates one (or re-skins it) at its default place,
        88 / e0 release them, and a window's opacity only changes through 87 / e4:
        a new window is invisible until faded in. e1 loads an expression and e2
        shows it, cross-fading over n frames when the window is visible.
        """
        v = vm.val
        if op == 0xe0:                       # release every window
            self.faces.clear()
            return
        if op == 0xe4:                       # (_, alpha, frames): 87 on every window
            n = 0 if fast else vm.fx_frames(a[2])
            target = max(0, min(255, v(a[1])))
            for f in self.faces.values():
                f.alpha.to(target, n)
            return
        fid = a[0]
        if op == 0x80:                       # (id, place, sheet name)
            f = self.faces.get(fid)
            if f is None:
                if len(self.faces) >= 3:
                    return
                f = Face(fid)
                self.face_counter += 1
                f.order = self.face_counter
                self.faces[fid] = f
            name = Program.string_arg(a, 2)
            if name:
                f.name = name
                f.sheet = self.game.face(name)
            f.w, f.h = 176, 54
            f.x.to(8, 0)
            f.y.to(self.FACE_SLOT_Y.get(v(a[1]), 8), 0)
            f.expr = f.pending = f.prev_expr = None
            f.xfade.to(1.0, 0)
            return
        f = self.faces.get(fid)
        if f is None:
            return
        if op == 0x88:                       # release this window (its opacity goes with it)
            del self.faces[fid]
        elif op == 0x81:
            f.w, f.h = max(8, v(a[1])), max(8, v(a[2]))
        elif op == 0x82:
            f.prio = v(a[1])
        elif op in (0x86, 0x8f):
            # 86 (id, axis, amp, frames) and 8f (id, axis, amp, period, frames), as
            # 0x2017d10 / 0x2016838: 86 uses period 1; 8f with frames 0 runs for
            # `period` frames at period 1. frames 0 stops, 0xFFFF (-1) runs until stopped.
            axis, amp = v(a[1]), v(a[2])
            if op == 0x86:
                period, frames = 1, (a[3] if len(a) > 3 else 0)
            else:
                period, frames = v(a[3]), (a[4] if len(a) > 4 else 0)
                if period == 0:
                    period = 1
                elif frames == 0:
                    frames, period = period, 1
            frames = frames - 0x10000 if frames >= 0x8000 else frames
            if frames == 0 or fast or axis not in (1, 2, 3):
                f.shake = None
            else:
                # DS (measured): the first offset shows on the op's frame and the
                # next, then the countdown runs through t = 0
                f.shake = [axis, amp, frames, max(1, period), True, frames < 0]
                f.shake_off = self._face_shake_value(f.shake, abs(frames))
        elif op == 0x8e:  # (id, r, g, b, amount, enable): tint/darken the window area
            if len(a) >= 6 and v(a[5]):
                f.tint = tuple(max(0, min(255, v(x))) for x in a[1:5])
            elif len(a) >= 6:
                f.tint = None
        elif op == 0x83:                     # (id, x, y[, frames, mode]): move the window
            n = 0 if fast or len(a) < 4 else vm.fx_frames(a[3])
            f.x.to(v(a[1]), n)
            f.y.to(v(a[2]), n)
        elif op == 0x8b:                     # (id, x, y[, frames, mode]): face image inside the window
            n = 0 if fast or len(a) < 4 else vm.fx_frames(a[3])
            f.ix.to(v(a[1]), n)
            f.iy.to(v(a[2]), n)
        elif op == 0xe1:                     # load an expression; e2 shows it
            f.pending = max(0, v(a[1]) - 1)
        elif op == 0xe2:                     # (id, frames): show the loaded expression
            if f.pending is not None:
                n = 0 if fast else max(0, vm.fx_frames(a[1]))
                if f.expr is not None and f.pending != f.expr and n > 1 and f.alpha.v > 0:
                    f.prev_expr = f.expr
                    f.xfade.to(0.0, 0)
                    f.xfade.to(1.0, n)
                else:
                    f.prev_expr = None
                    f.xfade.to(1.0, 0)
                f.expr, f.pending = f.pending, None
        elif op == 0x87:                     # (id, alpha, frames): fade the window
            n = 0 if fast else vm.fx_frames(a[2])
            f.alpha.to(max(0, min(255, v(a[1]))), n)
        else:
            vm.unknown[op] = vm.unknown.get(op, 0) + 1

    # =============================================================== frame
    def update(self):
        fast = self.fast
        for lay in self.layers:
            if lay.loading > 0:
                lay.loading = 0 if fast else lay.loading - 1
                continue
            lay.x.update(fast)
            lay.y.update(fast)
            lay.alpha.update(fast)
            axis, amp, n = lay.shake
            if n:
                lay.shake_off = self._layer_shake_step(lay)
                if n > 0:
                    lay.shake = (axis, amp, n - 1) if n > 1 else (0, 0, 0)
            else:
                lay.shake_off = (0, 0)
        for f in self.faces.values():
            f.alpha.update(fast)
            f.x.update(fast)
            f.y.update(fast)
            f.ix.update(fast)
            f.iy.update(fast)
            if f.shake is not None:
                if len(f.shake) > 4 and f.shake[4]:
                    f.shake[4] = False           # the op's own frame keeps its first offset
                else:
                    f.shake_off = self._face_shake_step(f.shake)
                    if not f.shake[5] and f.shake[2] < 0:     # shown t = 0: done
                        f.shake = None
            if f.xfade.busy:
                f.xfade.update(fast)
                if not f.xfade.busy:
                    f.prev_expr = None
        self.fade.update(fast)
        self.text_alpha.update(fast)
        if self.scroll.busy:
            self.scroll.update(fast)
            self._text_dirty = True
        if self.reveal_t > 0:
            self.reveal_t -= 1
            if self.reveal_t % 8 == 0:
                self._text_dirty = True
        if self.clear_fx is not None:
            self.clear_fx[1].update(fast)
            self._text_dirty = True
            if not self.clear_fx[1].busy:
                self._flush_page()
        self.click_glyph_t += 1
        if self.scroll_mode:
            self.scroll_t += 1
            self._text_dirty = True

    TRI = (-2, -1, 0, 1, 2, 1, 0, -1)    # 0x205e8b4

    @classmethod
    def _layer_shake_step(cls, lay):
        """One frame of the DS background shake (0x2011b54): one axis follows an
        8-frame triangle wave of ±amp; both axes jump to a new random offset
        every other pair of frames."""
        axis, amp, _n = lay.shake
        c = lay.shake_t
        lay.shake_t += 1
        if axis == 3:
            if c & 2:
                return (random.randint(0, 2 * amp) - amp, random.randint(0, 2 * amp) - amp)
            return lay.shake_off
        d = int(cls.TRI[c & 7] * amp / 2)
        return (d, 0) if axis == 1 else (0, d)

    @staticmethod
    def _face_shake_step(sh):
        """One frame of the DS face-window shake (0x2016710). Returns (dx, dy).

        Both axes: random jitter of ±amp. One axis: amp * sin(t * 90° / period),
        i.e. a full swing every 4 * period frames (period 1 = 0, +a, 0, -a).
        """
        n = sh[2]
        sh[2] = n - 1                  # "until stopped" (negative) keeps counting, |t| grows
        if sh[2] < -0x7FFF:
            sh[2] = -1
        return Scene._face_shake_value(sh, abs(n))

    @staticmethod
    def _face_shake_value(sh, t):
        axis, amp, period = sh[0], sh[1], sh[3]
        if axis == 3:
            return (random.randint(0, amp) - random.randint(0, amp),
                    random.randint(0, amp) - random.randint(0, amp))
        idx = (int(t * 16384 / period) & 0xFFFF) >> 4        # FX_SinCosTable_ index
        sv = round(math.sin(idx * 2 * math.pi / 4096) * 4096)     # the table's fx16 value
        # (sin * amp) >> 12 rounds down, so the window sits a pixel further left
        # (or up) for most of the negative half; measured on DeSmuME: per 120-frame
        # cycle at amp 2 it holds +2 1 frame, +1 40, 0 20, -1 20 and -2 40
        d = (sv * amp) >> 12
        return (d, 0) if axis == 1 else (0, d)

    def busy_effects(self):
        return self.fade.busy or any(l.alpha.busy or l.x.busy for l in self.layers)

    # =============================================================== render
    def render(self, out, hud=None):
        """Render into `out` (a 400x256 surface: two pages with a 16px spine).

        hud(left_page, right_page) draws the HUD before the screen fade.
        """
        view = pygame.Surface((VIEW_W, VIEW_H)).convert()
        view.fill((0, 0, 0))
        for lay in sorted(self.layers, key=lambda l: l.prio):
            if lay.img is None or lay.alpha.v <= 0 or lay.loading > 0:
                continue
            iw, ih = lay.img.get_size()
            sx, sy = lay.shake_off if lay.shake[2] else (0, 0)
            x, y = lay.x.v, lay.y.v
            # Keep full-size art covering both pages: a scroll past the image edge
            # (e.g. the shared impact effect on 480-wide art) would otherwise show black.
            if iw >= VIEW_W:
                x = min(max(x, 0), iw - VIEW_W)
            if ih >= VIEW_H:
                y = min(max(y, 0), ih - VIEW_H)
            px = -int(round(x)) + sx
            py = int(round(y)) + VIEW_H - ih + sy
            img = lay.img
            img.set_alpha(int(lay.alpha.v))
            view.blit(img, (px, py))
            _reset_alpha(img)
        left = view.subsurface((0, 0, PAGE_W, VIEW_H))
        right = view.subsurface((RIGHT_X, 0, PAGE_W, VIEW_H))
        self._render_faces(left)
        self._draw_fade(view)              # over the art and faces, under the text and HUD
        self._render_text(right)
        if hud is not None and not self.hide_text:
            hud(left, right)
        out.blit(view, (0, 0), (0, 0, PAGE_W, VIEW_H))
        out.blit(view, (PAGE_W + 16, 0), (RIGHT_X, 0, PAGE_W, VIEW_H))

    def render_hd(self, dst, origin, k, hud=None):
        """Draw the two pages straight into `dst` (the window) at scale k, with the
        upscaled art. origin is the window position of the logical canvas.

        Layers and faces use the high-res images; text (when not drawn by the
        high-res text renderer), the click marker and the HUD are drawn at DS size
        and enlarged pixel-sharp.
        """
        vw, vh = round(VIEW_W * k), round(VIEW_H * k)
        pw = round(PAGE_W * k)
        view = getattr(self, '_hd_view', None)
        if view is None or view.get_size() != (vw, vh):
            view = self._hd_view = pygame.Surface((vw, vh)).convert()
        view.fill((0, 0, 0))
        for lay in sorted(self.layers, key=lambda l: l.prio):
            if lay.img is None or lay.alpha.v <= 0 or lay.loading > 0:
                continue
            iw, ih = lay.img.get_size()
            sx, sy = lay.shake_off if lay.shake[2] else (0, 0)
            x, y = lay.x.v, lay.y.v
            if iw >= VIEW_W:
                x = min(max(x, 0), iw - VIEW_W)
            if ih >= VIEW_H:
                y = min(max(y, 0), ih - VIEW_H)
            img = self.game.bg_hd(lay.name, k) if lay.name else None
            if img is None:
                img = pygame.transform.smoothscale(lay.img, (round(iw * k), round(ih * k)))
            px = round((-round(x) + sx) * k)
            py = round((round(y) + VIEW_H - ih + sy) * k)
            if lay.alpha.v >= 255:
                view.blit(img, (px, py))
            else:
                img.set_alpha(int(lay.alpha.v))
                view.blit(img, (px, py))
                _reset_alpha(img)
        left = view.subsurface((0, 0, pw, vh))
        right = view.subsurface((round(RIGHT_X * k), 0, pw, vh))
        self._render_faces(left, k)
        self._draw_fade(view, k)
        lo = getattr(self, '_hd_lo', None)
        if lo is None:
            lo = self._hd_lo = (pygame.Surface((PAGE_W, VIEW_H), pygame.SRCALPHA),
                                pygame.Surface((PAGE_W, VIEW_H), pygame.SRCALPHA))
        lo_l, lo_r = lo
        lo_l.fill((0, 0, 0, 0))
        lo_r.fill((0, 0, 0, 0))
        if not self.hires and not self.hide_text and self.text_alpha.v > 0:
            # DS bitmap text: draw it at DS size, enlarge pixel-sharp
            hires, self.hires = self.hires, False
            mark, self.waiting = self.waiting, None
            self._render_text(lo_r)
            self.hires, self.waiting = hires, mark
        if hud is not None and not self.hide_text:
            hud(lo_l, lo_r)
        for lo_s, page in ((lo_l, left), (lo_r, right)):
            br = lo_s.get_bounding_rect()      # only enlarge the part that has pixels
            if br.w and br.h:
                x0, y0 = round(br.x * k), round(br.y * k)
                size = (round(br.right * k) - x0, round(br.bottom * k) - y0)
                page.blit(pygame.transform.scale(lo_s.subsurface(br), size), (x0, y0))
        self._draw_marker(right, k)
        ox, oy = origin
        dst.blit(view, (ox, oy), (0, 0, pw, vh))
        dst.blit(view, (ox + round((PAGE_W + 16) * k), oy), (round(RIGHT_X * k), 0, pw, vh))

    def _draw_fade(self, view, k=1):
        """Screen fade (62/63) over the art and the face windows. The DS draws it with
        OBJ priority 2 (0x201ad5c / 0x201a998), so the text and the HUD stay on top."""
        v = self.fade.v
        if v <= 0:
            return
        if self.fade_mode == 1:
            left, right = self.wipe_masks(int(v * 720 + 1e-6), k)
            view.blit(left, (0, 0))
            view.blit(right, (round(RIGHT_X * k), 0))
            return
        # 16-step fade: the DS alpha-blends with EVA = level, EVB = 15 - level
        # (out of 16), so even the first step dims to 14/16 and full level is 15/16
        level = int(v * 15 + 1e-6)
        if level <= 0:
            return
        keep = round(255 * (15 - level) / 16)
        view.fill((keep, keep, keep), special_flags=pygame.BLEND_RGB_MULT)
        add = tuple(round(c * level / 16) for c in self.fade_color)
        if any(add):
            view.fill(add, special_flags=pygame.BLEND_RGB_ADD)

    _WIPE_SPRITES = None

    @classmethod
    def _wipe_sprites(cls, game):
        """com/wipe.nnt: four 64x64 OBJs; #1 is the leading edge (a diagonal with a
        transparent corner), #3 the 1-in-2 column stripes. Pixel index 1 = fade colour."""
        if cls._WIPE_SPRITES is None:
            d = game.rom.read('com/wipe.nnt')
            h = nnt.header(d)
            px = nnt._unpack4(np.frombuffer(d, np.uint8, h['isize'], h['ioff'])).reshape(-1, 8, 8)
            spr = [px[s * 64:(s + 1) * 64].reshape(8, 8, 8, 8).transpose(0, 2, 1, 3).reshape(64, 64) == 1
                   for s in range(4)]
            cls._WIPE_SPRITES = (spr[1], spr[3])
        return cls._WIPE_SPRITES

    def _wipe_screen(self, p, top):
        """Coverage of one DS screen (192 rows x 256) at wipe step p (0..720), after
        0x201a998: four columns of stacked 64x64 sprites, the edge sprite leading and
        each column 32px behind the one to its left, slide over the screen; a second
        set from the other side, flipped (so its stripes fall in the gaps), follows and
        makes it solid. The top screen's first set starts at once and moves down, its
        second at step 272; the touch screen's moves up, its second set from 208."""
        edge, stripe = self._wipe_sprites(self.game)
        m = np.zeros((192, 256), bool)

        def put(spr, x, y, flip):
            if flip:
                spr = spr[::-1, ::-1]
            for dy in range(64):
                yy = (y + dy) & 0xff            # OAM y is 8 bits and wraps
                if yy < 192:
                    m[yy, x:x + 64] |= spr[dy]
        for i in range(4):
            x = (3 - i) * 64
            for j in range(4):
                spr = edge if j == 0 else stripe
                if top:
                    if p >= (3 - i) * 32 + 64 * j - 64:
                        put(spr, x, min(p - (3 - i) * 32, 192) - 64 * j, True)
                    if p >= 32 * i + 64 * j + 272:
                        put(spr, x, max(464 - p + 32 * i, -64) + 64 * j, False)
                else:
                    if p >= 32 * i + 64 * j:
                        put(spr, x, max(192 - p + 32 * i, -64) + 64 * j, False)
                    if p >= (3 - i) * 32 + 64 * j + 208:
                        put(spr, x, min(p - (3 - i) * 32 - 272, 192) - 64 * j, True)
        return np.rot90(m)                      # page view: 192 wide, 256 tall

    def wipe_masks(self, p, k=1):
        """(left page, right page) surfaces of the type-1 wipe at step p, scale k."""
        key = (p, k, self.fade_color)
        cache = getattr(self, '_wipe_cache', None)
        if cache is not None and cache[0] == key:
            return cache[1]
        out = []
        for top in (True, False):
            m = self._wipe_screen(p, top)
            surf = pygame.Surface((PAGE_W, VIEW_H), pygame.SRCALPHA)
            surf.fill(self.fade_color + (0,))
            a = pygame.surfarray.pixels_alpha(surf)
            a[:] = np.where(m.T, 255, 0)
            del a
            if k != 1:
                surf = pygame.transform.scale(surf, (round(PAGE_W * k), round(VIEW_H * k)))
            out.append(surf)
        self._wipe_cache = (key, tuple(out))
        return self._wipe_cache[1]

    def _render_faces(self, page, k=1):
        """Face windows: translucent navy panel with a white frame, face clipped inside.

        k > 1 draws at k times the DS size with the upscaled face sheets.
        """
        faces = [f for f in self.faces.values() if f.sheet is not None and f.alpha.v > 0]
        faces.sort(key=lambda f: (f.prio, f.order))
        for f in faces:
            sheet = f.sheet if k == 1 else (self.game.face_hd(f.name, k) or f.sheet)
            win = pygame.Surface((round(f.w * k), round(f.h * k)), pygame.SRCALPHA)
            if f.tint is not None:
                win.fill(f.tint)
            ix, iy = round((f.ix.v + FACE_IMG_DX) * k), round((f.iy.v + FACE_IMG_DY) * k)
            if f.expr is None:
                pass                         # no expression shown yet (e2 not reached)
            elif f.prev_expr is not None and f.xfade.v < 1:
                old = sheet.frame(f.prev_expr)
                old.set_alpha(int(255 * (1 - f.xfade.v)))
                win.blit(old, (ix, iy))
                _reset_alpha(old)
                img = sheet.frame(f.expr)
                img.set_alpha(int(255 * f.xfade.v))
                win.blit(img, (ix, iy))
                _reset_alpha(img)
            else:
                win.blit(sheet.frame(f.expr), (ix, iy))
            r = win.get_rect()
            lw = max(1, round(k))
            pygame.draw.rect(win, (255, 255, 255), r, lw)     # DS: a single 1px white frame
            win.set_alpha(int(f.alpha.v))
            ox, oy = f.shake_off if f.shake is not None else (0, 0)
            page.blit(win, (round((f.x.v + ox) * k), round((f.y.v + oy) * k)))

    def _render_text(self, page):
        if self.hide_text or self.text_alpha.v <= 0:
            return
        if not self.hires:
            if self._text_dirty or self._text_cache is None:
                self._text_cache = self.render_columns(self.columns)
                self._text_dirty = False
            a = self.text_alpha.v * self.text_fx_alpha()
            if a < 255:
                self._text_cache.set_alpha(int(a))
                page.blit(self._text_cache, (0, 0))
                _reset_alpha(self._text_cache)
            else:
                page.blit(self._text_cache, (0, 0))
        self._draw_marker(page)

    def _marker_sprite(self):
        """The DS click marker: the ▼ cell of com/icon.nnt, cropped, with the
        offset of its yellow interior (what the position rule below refers to)."""
        m = getattr(self, '_marker', None)
        if m is None:
            try:
                sheet = self.game.image('com/icon.nnt', rotate=True)
                cell = sheet.subsurface((0, 48, 16, 16)).copy()
                r = cell.get_bounding_rect()
                spr = cell.subsurface(r).copy()
                import numpy as np
                rgb = pygame.surfarray.array3d(spr).astype(int)
                al = pygame.surfarray.array_alpha(spr)
                inner = (np.abs(rgb - np.array([181, 173, 90])).sum(axis=2) < 40) & (al > 0)
                xs, ys = np.nonzero(inner)
                off = (int(xs.min()), int(ys.min())) if len(xs) else (2, 2)
                m = (spr, off)
            except Exception:           # no ROM sprite: a drawn triangle
                spr = pygame.Surface((9, 6), pygame.SRCALPHA)
                pygame.draw.polygon(spr, (0, 0, 0), [(0, 0), (8, 0), (4, 5)])
                pygame.draw.polygon(spr, (181, 173, 90), [(2, 2), (6, 2), (4, 4)])
                m = (spr, (2, 2))
            self._marker = m
        return m

    def _draw_marker(self, page, k=1):
        """DS click marker (measured on DeSmuME): only at 41/42 waits and not in
        auto mode; its interior sits 4px right of the column and 5px below the
        last glyph, and it bobs down a pixel every 8 frames over a 32-frame cycle."""
        if self.hide_text or self.text_alpha.v <= 0 or getattr(self, 'auto_mode', False):
            return
        if self.waiting in (0x41, 0x42) and self.columns and not self.fast and self.clear_fx is None:
            ci = min(len(self.columns), COLS_PER_PAGE) - 1
            x = COL_X0 - COL_PITCH * ci + 4
            gl, _ = self.layout_column(self.columns[-1])
            end = gl[-1][1] + CHAR_PITCH if gl else TEXT_TOP
            y = end + 5 + (self.click_glyph_t // 8) % 4
            spr, (ox, oy) = self._marker_sprite()
            if k != 1:
                spr = pygame.transform.scale(spr, (round(spr.get_width() * k), round(spr.get_height() * k)))
            page.blit(spr, (round((x - ox) * k), round((y - oy) * k)))

    # ------------------------------------------------------------ layout
    def placed_columns(self, columns):
        """Yield (x, column) for the columns that are on the page."""
        if self.scroll_mode and columns is self.columns:
            for col in columns:
                x = self._scroll_x(col)
                if -14 <= x <= PAGE_W:
                    yield x, col
            return
        live = columns is self.columns
        n = len(columns)
        b = max(0, n - COLS_PER_PAGE)
        sc = self.scroll.v if live else 0.0
        shift = 0.0
        if live and self.clear_fx is not None and self.clear_fx[0] == 'slide':
            shift = self.clear_fx[1].v * (PAGE_W + COL_PITCH)
        first = max(0, b - 1) if (live and sc > 0) else b   # the outgoing column only while scrolling
        for j in range(first, n):
            x = COL_X0 - COL_PITCH * (j - b + sc) + shift
            if -14 <= x <= PAGE_W:
                yield x, columns[j]

    KEY_NEW = (255, 214, 120)
    KEY_OWNED = (160, 210, 255)

    reveal_t = 0                # >0 while the radar hint flashes hidden keywords

    def keyword_color(self, kid):
        """Hidden TIPS words look like normal text until found (as on the DS)."""
        if kid is None:
            return (255, 255, 255)
        if kid in self.collected:
            return self.KEY_OWNED
        if self.reveal_t > 0 and (self.reveal_t // 8) % 2 == 0:
            return self.KEY_NEW
        return (255, 255, 255)

    def keyword_hits(self):
        """[(pygame.Rect in right-page pixels, card id)] for keywords on screen."""
        out = []
        for x, col in self.placed_columns(self.columns):
            glyphs, _ = self.layout_column(col)
            for gi, y, kid, _xo in glyphs:
                if kid is not None and kid >= 0:
                    out.append((pygame.Rect(int(x) - 2, int(y) - 1, 16, CHAR_PITCH + 2), kid))
        return out

    def visible_new_keywords(self):
        return sorted(set(k for _, k in self.keyword_hits() if k not in self.collected))

    def text_fx_alpha(self):
        """Extra opacity factor for page-clear transitions."""
        if self.clear_fx is not None and self.clear_fx[0] == 'fade':
            tw = self.clear_fx[1]
            if self.fast_fx:
                return 1.0 - tw.v
            # measured on DeSmuME: the page drops to ~80% at once, fades
            # linearly and is gone 30 frames in (the script still waits 32)
            t = tw.v * DISSOLVE_FRAMES_DS
            return 0.0 if t >= 30 else max(0.0, min(1.0, (33.0 - t) / 39.0))
        return 1.0

    @staticmethod
    def layout_column(col):
        """Glyph and ruby placements of one column, in page pixels relative to its x.

        Returns ([(glyph_index, y, keyword_id or None, x_off)], [(ruby_code, x_off, y)]).

        Half-width codes: 0x20XX is a lone half-width character (0x2020 is a
        half-height space); 0x21XX + 0x22XX are a 縦中横 pair set side by side in
        one cell (the engine does not advance after the 0x21XX half).
        """
        marked = {}
        for m in col.marks:
            for j in range(m[0], m[0] + m[1]):
                marked[j] = m[2] if len(m) > 2 else -1
        glyphs, ys = [], []
        y = TEXT_TOP
        for j, gi in enumerate(col.glyphs):
            ys.append(y)
            hi = gi & 0xFF00
            if gi == 0x2020:      # half-width space
                y += CHAR_PITCH // 2
                continue
            if hi == 0x2100:      # left half of a 縦中横 pair: no advance
                glyphs.append((gi, y, marked.get(j), 0))
                continue
            y += CHAR_PITCH
            if hi == 0x2200:      # right half
                glyphs.append((gi, ys[-1], marked.get(j), 6))
            elif hi == 0x2000:    # lone half-width character, centred
                glyphs.append((gi, ys[-1], marked.get(j), 3))
            elif gi != 614:
                glyphs.append((gi, ys[-1], marked.get(j), 0))
        ys.append(y)
        ruby = []
        for end, nb, codes in col.ruby:
            start = max(0, end - nb)
            if end >= len(ys) or not codes:
                continue
            # DS placement (0x2020070): 8px per ruby character centred on the base.
            # A reading longer than its base runs past it on both sides (never
            # squeezed); a shorter one is spread evenly over the base.
            span = int(ys[end] - ys[start])
            n = len(codes)
            run = 8 * n
            y0 = ys[start] + span // 2 - run // 2
            pitch = 8
            if span > run:
                extra = int((span - run) / n)
                pitch += extra
                y0 -= int(extra * (n - 1) / 2)
            if y0 < TEXT_TOP:
                y0 = TEXT_TOP
            elif y0 + run > TEXT_BOTTOM and run <= TEXT_BOTTOM - TEXT_TOP:
                y0 = TEXT_BOTTOM - run
            for k, c in enumerate(codes):
                ruby.append((c, 12, y0 + k * pitch))   # 1px left of where it was: matches DS captures
        return glyphs, ruby

    def render_columns(self, columns, dim=False):
        """The text layer as the DS builds it: every glyph's pixels go into one
        4bpp layer and the outline stamps are OR-ed over the whole page, so a rim
        never covers a neighbouring stroke (see _stamped)."""
        import numpy as np
        g = self.game
        body = np.zeros((PAGE_W, VIEW_H), bool)
        cols = np.full((PAGE_W, VIEW_H, 3), 255, np.uint8)

        def put(glyph, x, y, color):
            m = pygame.surfarray.array_alpha(glyph) > 0
            w, h = m.shape
            x0, y0 = max(0, x), max(0, y)
            x1, y1 = min(PAGE_W, x + w), min(VIEW_H, y + h)
            if x1 <= x0 or y1 <= y0:
                return
            sub = m[x0 - x:x1 - x, y0 - y:y1 - y]
            body[x0:x1, y0:y1] |= sub
            if color != (255, 255, 255):
                cols[x0:x1, y0:y1][sub] = color
        for x, col in self.placed_columns(columns):
            x = int(x)
            glyphs, ruby = self.layout_column(col)
            for gi, y, kid, xo in glyphs:
                put(g.glyph(gi), x + xo, y, self.keyword_color(kid))
            for c, dx, y in ruby:
                put(g.ruby_glyph(c), x + dx, int(y), (255, 255, 255))
        return self.stamp_layer(body, cols)

    @classmethod
    def stamp_layer(cls, body, colors):
        import numpy as np
        table = cls.STAMP_SHADOW if cls.stamp_style == 'shadow' else cls.STAMP_RIM
        W, H = body.shape
        idx = np.zeros((W + 5, H + 4), np.uint8)              # room for offsets -2..+3, -2..+2
        for r, row in enumerate(table):
            for c, v in enumerate(row):
                if v:
                    idx[c:c + W, r:r + H][body] |= v
        idx = idx[2:2 + W, 2:2 + H]
        rgb = np.zeros((W, H, 3), np.uint8)
        alpha = np.zeros((W, H), np.uint8)
        for v, col in cls.TEXT_PAL.items():
            sel = idx == v
            rgb[sel] = col
            alpha[sel] = 255
        alpha[(idx >= 4) & (idx < 8)] = 255                  # black
        sel = idx >= 8
        rgb[sel] = colors[sel]
        alpha[sel] = 255
        alpha[TEXT_CLIP_X:, :] = 0      # the DS text window ends there (a scrolling column is cut off)
        surf = pygame.Surface((W, H), pygame.SRCALPHA)
        pygame.surfarray.pixels3d(surf)[:] = rgb
        pygame.surfarray.pixels_alpha(surf)[:] = alpha
        return surf

    _tint_cache = {}

    @classmethod
    def _tinted(cls, glyph, color):
        key = (id(glyph), color)
        s = cls._tint_cache.get(key)
        if s is None:
            s = glyph.copy()
            s.fill(color, special_flags=pygame.BLEND_RGB_MULT)
            cls._tint_cache[key] = s
        return s

    # DS glyph rasteriser (0x201fb24): the 1-bit glyph is stamped at every offset
    # of a 5x6 table, OR-ing that cell's palette index into the 4bpp text layer;
    # the text palette (0x2059020) then maps 1 -> grey, 2/3 -> dark grey, 4..7 ->
    # black, 15 -> the glyph colour. Settings byte 4 picks the table: the default
    # (0x2058ff4) adds a black drop shadow right and below; the other (0x2058fd4)
    # is a plain grey rim. Rows run down the page, columns across, centred on (2, 2).
    STAMP_SHADOW = ((0, 1, 1, 1, 1, 0), (1, 2, 2, 2, 2, 1), (1, 2, 15, 4, 4, 1),
                    (1, 2, 4, 4, 4, 1), (0, 1, 1, 1, 1, 0))
    STAMP_RIM = ((0, 1, 1, 1, 0, 0), (1, 2, 2, 2, 1, 0), (1, 2, 15, 2, 1, 0),
                 (1, 2, 2, 2, 1, 0), (0, 1, 1, 1, 0, 0))
    TEXT_PAL = {1: (165, 165, 165), 2: (99, 99, 99), 3: (99, 99, 99)}
    stamp_style = 'shadow'
    _stamp_cache = {}

    @classmethod
    def _stamped(cls, glyph, color):
        key = (id(glyph), color, cls.stamp_style)
        s = cls._stamp_cache.get(key)
        if s is not None:
            return s
        import numpy as np
        table = cls.STAMP_SHADOW if cls.stamp_style == 'shadow' else cls.STAMP_RIM
        m = pygame.surfarray.array_alpha(glyph) > 0          # [x, y]
        w, h = m.shape
        idx = np.zeros((w + 5, h + 4), np.uint8)
        for r, row in enumerate(table):
            for c, v in enumerate(row):
                if v:
                    idx[c:c + w, r:r + h][m] |= v
        rgb = np.zeros((w + 5, h + 4, 3), np.uint8)
        alpha = np.zeros((w + 5, h + 4), np.uint8)
        for v, col in cls.TEXT_PAL.items():
            sel = idx == v
            rgb[sel] = col
            alpha[sel] = 255
        sel = (idx >= 4) & (idx < 8)
        alpha[sel] = 255                                     # black
        sel = idx >= 8
        rgb[sel] = color
        alpha[sel] = 255
        s = pygame.Surface((w + 5, h + 4), pygame.SRCALPHA)
        pygame.surfarray.pixels3d(s)[:] = rgb
        pygame.surfarray.pixels_alpha(s)[:] = alpha
        if len(cls._stamp_cache) > 6000:
            cls._stamp_cache.clear()
        cls._stamp_cache[key] = s
        return s

    @classmethod
    def _outlined(cls, dst, glyph, x, y, color, shadow):
        dst.blit(cls._stamped(glyph, color), (x - 2, y - 2))

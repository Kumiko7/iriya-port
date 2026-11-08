"""Iriya no Sora, UFO no Natsu I + II — PC port shell (pygame).

Both DS games are read straight from the user's ROM files at runtime; no game
data is stored in this program.
"""
import json
import os
import sys
import time
import logging
import pygame

from . import paths
from .rom import find_roms
from .assets import GameData, GAME_TITLES
from .audio import Audio
from .scene import Scene, PAGE_W, VIEW_H, COL_X0, COL_PITCH
from .vm import VM, Program
from .hires import HiresText, find_font
from .hud import Hud

log = logging.getLogger('app')

LOGICAL = (PAGE_W * 2 + 16, VIEW_H)   # two pages + spine
RIGHT = PAGE_W + 16                   # x of the right page in logical space
FPS = 60
SKIP_DWELL = 2                        # frames each message stays up while skipping
APP_NAME = 'イリヤの空、ＵＦＯの夏　Ｉ＋ＩＩ'

DEFAULT_SETTINGS = {
    'char_frames': 2.0,     # frames per character: 文字速度 速い (4 = the DS's own pace, 0 = instant)
    'fast_fx': True,        # 演出速度 速い: quicker column scroll / page dissolve than the DS
    'card_popup': True,     # show the 「カードゲット！」 box when a card is collected
    'auto_frames': 30,      # auto mode: frames after a message (DS: 10 / 30 / 50, a new save starts at 30); -1 = by length
    'bgm_volume': 0.8,
    'se_volume': 0.9,
    'scale': 3,
    'fullscreen': False,
    'smooth': False,
    'hires_text': True,     # draw story text at window resolution
    'bgm_set': 'ds',        # 'ds' = the games' own music, 'himawari' = the alternative soundtrack
    'art': 'hd',            # 'hd' = upscaled art (when built with tools/upscale_assets.py), 'original'
}
TEXT_SPEEDS = [('瞬間', 0.0), ('とても速い', 1.0), ('速い', 2.0), ('原作', 4.0), ('遅い', 6.0)]
AUTO_SPEEDS = [('速い', 10), ('普通', 30), ('遅い', 50), ('文字数', -1)]

WHITE = (255, 255, 255)
GOLD = (255, 214, 120)
GREY = (150, 160, 175)
SKY = (168, 200, 236)


def data_dir():
    return paths.user_dir()


# =========================================================================
class App:
    def __init__(self, rom_dirs=()):
        rom_dirs = paths.search_dirs(rom_dirs)      # given folders first, then data/
        pygame.init()
        pygame.display.set_caption('Iriya no Sora, UFO no Natsu I+II')
        self.ddir = data_dir()
        self.settings = dict(DEFAULT_SETTINGS)
        self.settings.update(self._load_json('settings.json', {}))
        self.window = None
        self._apply_video()
        self.canvas = pygame.Surface(LOGICAL).convert()
        self.clock = pygame.time.Clock()
        self.hires = HiresText(find_font(self.settings.get('hires_font')))
        self.audio = Audio()
        self._apply_volume()

        roms = find_roms(rom_dirs)
        self.games = {}
        for gid in ('iriya1', 'iriya2'):
            if gid in roms:
                self.games[gid] = GameData(gid, roms[gid])
        if not self.games:
            raise SystemExit('No ROMs found. Put the two .nds files in data/roms/ or pass their folder.')
        hd_root = paths.upscaled_dir()
        try:
            with open(os.path.join(hd_root, 'manifest.json'), encoding='utf-8') as f:
                manifest = json.load(f)
        except (OSError, ValueError):
            manifest = {}
        for gid, g in self.games.items():
            d = os.path.join(hd_root, gid)
            g.hd_dir = d if os.path.isdir(d) else None
            g.hd_upright = bool(manifest.get('upright'))
        self.hd_ready = any(g.hd_dir for g in self.games.values())
        from .bonus import install as install_bonus
        self.bonus_chapters = install_bonus(self.games, rom_dirs)
        from .alt_bgm import find as find_alt_bgm
        self.audio.alt = find_alt_bgm(list(rom_dirs) + [self.ddir])
        self.audio.set_alt(self.settings.get('bgm_set') == 'himawari')
        self.order = [(gid, c) for gid in ('iriya1', 'iriya2') if gid in self.games
                      for c, _ in self.games[gid].chapters]
        self.ui_game = self.games.get('iriya1') or self.games.get('iriya2')
        self.audio.set_game(self.ui_game)
        try:
            icon = self.ui_game.face('w_iriya_03_r')
            if icon:
                pygame.display.set_icon(pygame.transform.smoothscale(icon.frame(0).subsurface((32, 0, 64, 64)), (32, 32)))
        except (pygame.error, ValueError):
            pass

        self.saves = self._load_json('saves.json', {})
        prog = self._load_json('progress.json', {})
        self.minigame = prog.get('minigame', {})   # per mini-game persistent data
        self.read_chapters = set(prog.get('seen', []))
        self.cards = {g: set(v) for g, v in prog.get('cards', {}).items()}
        self.unlocks = set(prog.get('unlocks', []))
        self._huds = {}
        self.state = None
        self.session = None      # active Session (story playback)
        self.goto(TitleState(self))

    # --------------------------------------------------------------- persistence
    def _load_json(self, name, default):
        try:
            with open(os.path.join(self.ddir, name), encoding='utf-8') as f:
                return json.load(f)
        except (OSError, ValueError):
            return default

    def _save_json(self, name, data):
        p = os.path.join(self.ddir, name)
        tmp = p + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, p)

    def save_settings(self):
        self._save_json('settings.json', self.settings)

    def save_progress(self):
        self._save_json('progress.json', {
            'seen': sorted(self.read_chapters),
            'cards': {g: sorted(v) for g, v in self.cards.items()},
            'unlocks': sorted(self.unlocks),
            'minigame': self.minigame})

    def mark_seen(self, gid, chap):
        key = f'{gid}:{chap}'
        if key not in self.read_chapters:
            self.read_chapters.add(key)
            self.save_progress()

    def owned_cards(self, gid):
        return self.cards.setdefault(gid, set())

    def give_card(self, gid, cid):
        """Add a card; returns True if it is new."""
        owned = self.owned_cards(gid)
        if cid in owned or not (0 <= cid < len(self.games[gid].cards)):
            return False
        owned.add(cid)
        if gid == 'iriya1' and 2 not in owned:          # 電撃カード大戦 unlock
            battle = [c for c in self.games[gid].cards if c.is_battle and c.index in owned]
            if len(battle) >= 20:
                owned.add(2)
        self.save_progress()
        return True

    def hud(self, gid):
        h = self._huds.get(gid)
        if h is None:
            h = Hud(self.games[gid])
            self._huds[gid] = h
        return h

    # --------------------------------------------------------------- video
    def _apply_video(self):
        flags = pygame.RESIZABLE
        if self.settings['fullscreen']:
            self.window = pygame.display.set_mode((0, 0), pygame.FULLSCREEN)
        else:
            s = int(self.settings['scale'])
            self.window = pygame.display.set_mode((LOGICAL[0] * s, LOGICAL[1] * s), flags)

    def _apply_volume(self):
        if self.audio.ok:
            self.audio.bgm_volume = self.settings['bgm_volume']
            self.audio.se_volume = self.settings['se_volume']

    def toggle_fullscreen(self):
        self.settings['fullscreen'] = not self.settings['fullscreen']
        self._apply_video()
        self.save_settings()

    def target(self):
        """The canvas the current state draws on (mini-games use their own size)."""
        return getattr(self.state, 'canvas', None) or self.canvas

    def view_rect(self):
        lw, lh = self.target().get_size()
        ww, wh = self.window.get_size()
        k = min(ww / lw, wh / lh)
        if not self.settings.get('smooth') and k >= 1 and int(k) >= k * 0.8:
            k = int(k)          # crisp integer scaling unless it wastes a lot of the window
        w, h = int(lw * k), int(lh * k)
        return pygame.Rect((ww - w) // 2, (wh - h) // 2, w, h)

    def to_logical(self, pos):
        r = self.view_rect()
        lw, lh = self.target().get_size()
        return ((pos[0] - r.x) * lw / r.w, (pos[1] - r.y) * lh / r.h)

    def hires_active(self):
        return bool(self.settings.get('hires_text')) and self.hires.ok

    def art_hd(self):
        """Draw story scenes with the upscaled art at window resolution."""
        return self.hd_ready and self.settings.get('art', 'hd') == 'hd'

    def present(self):
        # Always draw to the live display surface (it can change on resize or
        # fullscreen toggles) with no leftover clip, so stale frames never show.
        surf = pygame.display.get_surface()
        if surf is not None:
            self.window = surf
        self.window.set_clip(None)
        r = self.view_rect()
        self.window.fill((0, 0, 0))
        canvas = self.target()
        k = r.w / canvas.get_width()
        art = getattr(self.state, 'draw_art', None)
        art_on = art is not None and self.art_hd() and art(self.window, r, k)
        if art_on:
            # the pages came from draw_art; only the spine is taken from the canvas
            x0, x1 = round(PAGE_W * k), round((PAGE_W + 16) * k)
            self.window.blit(pygame.transform.scale(canvas.subsurface((PAGE_W, 0, 16, VIEW_H)),
                                                    (x1 - x0, r.h)), (r.x + x0, r.y))
        else:
            if self.settings.get('smooth'):
                frame = pygame.transform.smoothscale(canvas, r.size)
            else:
                frame = pygame.transform.scale(canvas, r.size)
            self.window.blit(frame, r.topleft)
        hook = getattr(self.state, 'draw_hires', None)
        if hook is not None and (self.hires_active() or getattr(self.state, 'hires_always', False)):
            hook(self.window, r, k)
        if art_on:                            # popups, toasts and flags go over the art
            ov = getattr(self.state, 'overlay', None)
            br = ov.get_bounding_rect() if ov is not None else None
            if br and br.w and br.h:
                x0, y0 = round(br.x * k), round(br.y * k)
                size = (round(br.right * k) - x0, round(br.bottom * k) - y0)
                self.window.blit(pygame.transform.scale(ov.subsurface(br), size), (r.x + x0, r.y + y0))
        pygame.display.flip()

    # --------------------------------------------------------------- flow
    def goto(self, state):
        self.state = state

    def run(self):
        while True:
            for ev in pygame.event.get():
                if ev.type == pygame.QUIT:
                    self.quit()
                if ev.type == pygame.KEYDOWN and (ev.key == pygame.K_F11 or
                                                  (ev.key == pygame.K_RETURN and ev.mod & pygame.KMOD_ALT)):
                    self.toggle_fullscreen()
                    continue
                if ev.type in (pygame.MOUSEBUTTONDOWN, pygame.MOUSEBUTTONUP, pygame.MOUSEMOTION):
                    ev.lpos = self.to_logical(ev.pos)
                self.state.event(ev)
            self.state.update()
            self.audio.update()
            canvas = self.target()
            canvas.fill((0, 0, 0))
            self.state.draw(canvas)
            self.present()
            self.clock.tick(FPS)

    def quit(self):
        if self.session:
            self.session.autosave()
        pygame.quit()
        sys.exit(0)

    def start_chapter(self, gid, chap, seek_line=None, skip_clicks=0, keep_text=False):
        self.session = Session(self, gid, chap, seek_line, skip_clicks, keep_text)
        self.goto(PlayState(self))

    def next_chapter(self, gid, chap):
        i = self.order.index((gid, chap))
        if i + 1 < len(self.order):
            return self.order[i + 1]
        return None

    def ui_sound(self, kind):
        self.audio.set_game(self.ui_game)
        self.audio.ui_sound({'move': 0, 'cancel': 1, 'enter': 2}[kind])
        if self.session:
            self.audio.set_game(self.session.game)

    # --------------------------------------------------------------- UI text
    def text(self, surf, s, x, y, color=WHITE, align='left', game=None):
        """Draw horizontal Japanese UI text with the game's own font."""
        g = game or self.ui_game
        idxs = g.text_glyphs(s)
        w = 12 * len(idxs)
        if align == 'center':
            x -= w // 2
        elif align == 'right':
            x -= w
        for i, gi in enumerate(idxs):
            if gi is None or gi == 614:
                continue
            Scene._outlined(surf, g.glyph(gi, vertical=False), x + 12 * i, y, color, (0, 0, 0))
        return w

    def vtext(self, surf, s, x, y, color=WHITE, pitch=14, game=None):
        """Draw vertical text (top to bottom) — used for chapter title cards."""
        g = game or self.ui_game
        for i, gi in enumerate(g.text_glyphs(s)):
            if gi is None or gi == 614:
                continue
            Scene._outlined(surf, g.glyph(gi, vertical=True), x, y + pitch * i, color, (0, 0, 0))


# =========================================================================
class Session:
    """A running chapter: VM + scene + autosave/bookmark bookkeeping."""

    def __init__(self, app, gid, chap, seek_line=None, skip_clicks=0, keep_text=False):
        self.app = app
        self.gid, self.chap = gid, chap
        self.game = app.games[gid]
        app.audio.set_game(self.game)
        if keep_text and app.audio.ok:       # stepping back: keep the music going
            for ch in range(len(app.audio.se)):
                app.audio.stop_se(ch)
        else:
            app.audio.stop_all()
        self.scene = Scene(self.game, app.audio, app.settings)
        self.scene.chapter = chap
        self.prog = Program(self.game.script(chap))
        self.vm = VM(self.prog, self.scene)
        self.scene.collected = app.owned_cards(gid)
        self.hud = app.hud(gid)
        self.popups = []            # [card, frames left] card-get notifications
        self._awarded_line = -1
        self.auto = False
        self.skip = False
        self.skip_dwell = 0
        self.wait_t = 0
        self.title_card = 0
        app.mark_seen(gid, chap)
        if seek_line is not None:
            self._seek(seek_line, skip_clicks, keep_text)

    def _seek(self, line, skip_clicks=0, keep_text=False):
        """Fast-forward to the start of text line `line`, then past `skip_clicks`
        click waits inside it. keep_text leaves the page's text on screen."""
        sc, vm = self.scene, self.vm
        sc.fast = True
        sc.mute_audio = True
        vm.seek_line = line
        n = 0
        while not vm.seek_done and not vm.finished and n < 100000:
            vm.step_frame(fast=True)
            sc.update()
            if vm.waiting_click():
                vm.click()
            n += 1
        # inside the line: run without collapsing click waits so they can be counted
        n = 0
        while skip_clicks > 0 and not vm.finished and n < 20000:
            vm.step_frame(fast=False)
            sc.update()
            if vm.waiting_click():
                vm.click()
                skip_clicks -= 1
            n += 1
        sc.fast = False
        sc.mute_audio = False
        if not keep_text:
            sc.columns = []
            sc.keywords = []
        sc.clear_fx = None
        sc.scroll.to(0, 0)
        sc.waiting = None
        sc._text_dirty = True
        for lay in sc.layers:
            lay.shake = (0, 0, 0) if lay.shake[2] != -1 else lay.shake
        au = self.app.audio
        if sc.bgm is not None and sc.bgm_state == 2:
            au.play_bgm(sc.bgm, 30, sc.bgm_ctx)
            au.master_to(sc.bgm_master, 0)
        elif sc.bgm is not None and sc.bgm_state == 1:
            au.bgm_prepare(sc.bgm, sc.bgm_ctx)
            au.master_to(sc.bgm_master, 0)
        elif keep_text:
            au.bgm_fade(0.0, 30, stop=True)
            au.bgm_state = 0

    # ------------------------------------------------------------ cards
    def collect(self, cid, popup=True):
        if self.app.give_card(self.gid, cid):
            self.scene._text_dirty = True
            if popup:
                if self.app.settings.get('card_popup', True):
                    self.popups.append([self.game.cards[cid], 150])
                self.app.audio.set_game(self.game)
                self.app.audio.ui_sound(5)          # SE_CARDGET
            return True
        return False

    def _award_lines(self, final=False):
        """Cards granted for reaching a line (Game II chapter rewards, non-keyword cards)."""
        line = self.scene.line_id
        if line == self._awarded_line and not final:
            return
        self._awarded_line = line
        for ln, cids in self.prog.card_lines.items():
            if ln <= line or final:
                for cid in cids:
                    if cid not in self.prog.keyword_cards:
                        self.collect(cid)

    def radar_level(self):
        """Keyword radar, as measured on the DS: it warns of keywords ahead in the
        script. Cell 1 (dim) normally, 2 from 16 text lines before a keyword, 3 from
        6 lines before until 16 lines after it. Game II shows no radar (None)."""
        if self.gid == 'iriya2':
            return None
        kw = getattr(self, '_kw_lines', None)
        if kw is None:
            kw, line = [], 0
            for ins in self.prog.instrs:
                if ins.op == 0x4f and ins.args:
                    line = ins.args[0]
                elif ins.op == 0x54:
                    kw.append(line)
            self._kw_lines = kw
        cur = self.scene.line_id
        nxt = min((k - cur for k in kw if k >= cur), default=None)
        prv = min((cur - k for k in kw if k <= cur), default=None)
        if (prv is not None and prv <= 16) or (nxt is not None and nxt <= 6):
            return 3
        if nxt is not None and nxt <= 16:
            return 2
        return 1

    def click_text(self, pos):
        """Left-click on the right page: collect a keyword card if one was hit."""
        for rect, kid in self.scene.keyword_hits():
            if rect.collidepoint(pos) and kid not in self.scene.collected:
                return self.collect(kid)
        return False

    @property
    def title(self):
        for c, t in self.game.chapters:
            if c == self.chap:
                return t
        return self.chap

    def snapshot(self):
        sc = self.scene
        preview = ''
        cols = sc.columns[-8:] or sc.history[-8:]
        for c in cols[:2]:
            preview += ''.join(chr(g & 0xFF) if 0x2000 <= g < 0x2300 else self.game.font.char(g)
                               for g in c.glyphs if g not in (614, 0x2020))
        return {'game': self.gid, 'chap': self.chap, 'line': sc.page_line,
                'title': self.title, 'time': time.strftime('%Y/%m/%d %H:%M'),
                'preview': preview[:20]}

    def autosave(self):
        if self.vm.finished:
            return
        self.app.saves['auto'] = self.snapshot()
        self.app._save_json('saves.json', self.app.saves)

    def go_back(self):
        """Step back one message (Backspace). Returns False at the chapter start."""
        starts = self.scene.seg_starts
        if len(starts) < 2:
            return False
        line, clicks = starts[-2]
        self.app.start_chapter(self.gid, self.chap, line, clicks, keep_text=True)
        return True

    def click(self):
        sc, vm = self.scene, self.vm
        if vm.waiting_click():
            kind = vm.click()
            self.wait_t = 0
            if kind in (0x48, 0x49, 0x4a, 0x4f):
                self.autosave_counter = getattr(self, 'autosave_counter', 0) + 1
                if self.autosave_counter % 5 == 0:
                    self.autosave()
        else:
            vm.click()               # cuts an op 59 wait short, as a tap does on the DS
            sc.skip_reveal()

    def update(self):
        sc, vm = self.scene, self.vm
        if self.title_card > 0:
            self.title_card -= 1
            return
        sc.auto_mode = self.auto
        vm.stop_at_clicks = self.skip
        # skip: scrolls, fades and page clears finish at once, as on load; a skip
        # ends with the page exactly as normal reading would have left it
        sc.fast = self.skip
        if self.skip:
            # skip: text and effects at once, each message on screen for
            # SKIP_DWELL frames, so the pages go by quickly but visibly
            vm.step_frame(fast=True)
            sc.update()
            if vm.waiting_click():
                self.skip_dwell += 1
                if self.skip_dwell >= SKIP_DWELL:
                    self.skip_dwell = 0
                    self.click()
            if self.vm.finished:
                self._award_lines(final=True)
            for p in self.popups:
                p[1] -= 1
            self.popups = [p for p in self.popups if p[1] > 0]
            return
        for _ in range(1):
            vm.step_frame(fast=False)
            sc.update()
            if vm.waiting_click():
                if self.auto:
                    # DS (0x200d294 / 0x200cad8): a fixed countdown from the moment the
                    # message is done. Waits that end a page (48/49/4a, 0x200ec70) count
                    # three times as long as ones inside a page (41/42, 0x200eb4c):
                    # 30/90/150 against 10/30/50. '文字数' is this port's own
                    # length-based wait.
                    self.wait_t += 1
                    af = self.app.settings.get('auto_frames', 30)
                    if af < 0:
                        chars = sum(len(c.glyphs) for c in sc.columns[-2:])
                        need = 45 + chars * 3
                    elif vm.main.wait_arg in (0x48, 0x49, 0x4a):
                        need = af * 3
                    else:
                        need = af
                    if af >= 0:
                        need += 1    # the DS counts down before the script runs each frame
                    if self.wait_t >= need:
                        self.click()
            if vm.finished:
                break
        if not self.skip or self.vm.finished:
            self._award_lines(final=self.vm.finished)
        for p in self.popups:
            p[1] -= 1
        self.popups = [p for p in self.popups if p[1] > 0]

    def _draw_hud(self, left, right):
        h = self.hud
        h.draw_left(left, self.chap, self.scene.page or 1, self.prog.max_page, self.radar_level())
        h.draw_right(right, self.vm.waiting_click(), self.auto)

    def draw_popups(self, surf):
        if not self.popups:
            return
        card, t = self.popups[0]
        a = min(1.0, t / 20.0, (150 - t) / 12.0)
        box = pygame.Surface((170, 64), pygame.SRCALPHA)
        box.fill((10, 20, 50, 215))
        pygame.draw.rect(box, GOLD, box.get_rect(), 1)
        img = self.game.card_image(card, 'b') or self.game.card_image(card, 's')
        if img is not None:
            box.blit(img, (6, (64 - img.get_height()) // 2))
        self.app.text(box, 'カードゲット！', 52, 10, GOLD, game=self.game)
        name = card.name if len(card.name) <= 9 else card.name[:8] + '…'
        self.app.text(box, name, 52, 30, WHITE, game=self.game)
        if self.hud.new is not None:
            box.blit(self.hud.new, (170 - self.hud.new.get_width() - 4, 46))
        for i in range(3):                       # little sparkles
            ph = (t + i * 17) % 40
            if ph < 20:
                cx, cy, rr = 10 + i * 14, 8 + (i * 23) % 44, 1 + ph // 6
                pygame.draw.line(box, (255, 255, 220), (cx - rr, cy), (cx + rr, cy))
                pygame.draw.line(box, (255, 255, 220), (cx, cy - rr), (cx, cy + rr))
        box.set_alpha(int(255 * max(0.0, a)))
        surf.blit(box, (11, 96))

    def draw_art(self, window, r, k):
        """High-res scene (upscaled art). Returns False when the 1x frame is used."""
        if self.title_card > 0:
            return False
        self.scene.hires = self.app.hires_active()
        self.scene.render_hd(window, r.topleft, k, hud=self._draw_hud)
        return True

    def draw_hires(self, window, r, k):
        sc = self.scene
        if self.title_card > 0 or sc.hide_text:
            return
        alpha = sc.text_alpha.v * sc.text_fx_alpha()      # screen fades stay under the text
        from .scene import TEXT_CLIP_X
        old = window.get_clip()
        window.set_clip(pygame.Rect(r.x + RIGHT * k, r.y, round(TEXT_CLIP_X * k), round(VIEW_H * k)).clip(old))
        self.app.hires.draw_page(window, sc, sc.columns, (r.x + RIGHT * k, r.y), k, alpha)
        window.set_clip(old)

    def draw(self, surf, hires=False):
        self.scene.hires = hires
        if self.title_card > 0:
            surf.fill((0, 0, 0))
            a = min(1.0, self.title_card / 30.0, (150 - self.title_card) / 30.0)
            tmp = pygame.Surface(LOGICAL, pygame.SRCALPHA)
            gt = GAME_TITLES[self.gid]
            self.app.vtext(tmp, gt, RIGHT + COL_X0, 30, GREY, game=self.game)
            self.app.vtext(tmp, self.title, RIGHT + COL_X0 - 2 * COL_PITCH, 50, WHITE, game=self.game)
            tmp.set_alpha(int(255 * max(0.0, a)))
            surf.blit(tmp, (0, 0))
            return
        self.scene.render(surf, hud=self._draw_hud)


# =========================================================================
class State:
    def __init__(self, app):
        self.app = app

    def event(self, ev):
        pass

    def update(self):
        pass

    def draw(self, surf):
        pass


def draw_spine(surf):
    pygame.draw.rect(surf, (12, 12, 16), (PAGE_W, 0, 16, VIEW_H))
    for i in range(16):
        c = 20 + int(20 * (1 - abs(i - 7.5) / 7.5))
        pygame.draw.line(surf, (c, c, c + 6), (PAGE_W + i, 0), (PAGE_W + i, VIEW_H))


class Menu:
    """Simple vertical list of horizontal text items positioned in logical space."""

    def __init__(self, app, items, x, y, pitch=18, align='left', width=170):
        self.app = app
        self.items = items        # list of (label, value, enabled)
        self.x, self.y, self.pitch, self.align, self.width = x, y, pitch, align, width
        self.sel = 0
        while self.sel < len(items) and not items[self.sel][2]:
            self.sel += 1

    def rect(self, i):
        x = self.x - (self.width // 2 if self.align == 'center' else 0)
        return pygame.Rect(x - 4, self.y + i * self.pitch - 3, self.width + 8, self.pitch)

    def hit(self, pos):
        for i in range(len(self.items)):
            if self.items[i][2] and self.rect(i).collidepoint(pos):
                return i
        return None

    def move(self, d):
        n = len(self.items)
        for _ in range(n):
            self.sel = (self.sel + d) % n
            if self.items[self.sel][2]:
                self.app.ui_sound('move')
                return

    def event(self, ev):
        """Returns selected value or None."""
        if ev.type == pygame.MOUSEMOTION:
            i = self.hit(ev.lpos)
            if i is not None and i != self.sel:
                self.sel = i
        elif ev.type == pygame.MOUSEBUTTONDOWN and ev.button == 1:
            i = self.hit(ev.lpos)
            if i is not None:
                self.sel = i
                self.app.ui_sound('enter')
                return self.items[i][1]
        elif ev.type == pygame.KEYDOWN:
            if ev.key in (pygame.K_UP, pygame.K_w):
                self.move(-1)
            elif ev.key in (pygame.K_DOWN, pygame.K_s):
                self.move(1)
            elif ev.key in (pygame.K_RETURN, pygame.K_SPACE, pygame.K_z, pygame.K_KP_ENTER):
                if self.items and self.items[self.sel][2]:
                    self.app.ui_sound('enter')
                    return self.items[self.sel][1]
        return None

    def draw(self, surf):
        for i, (label, _v, en) in enumerate(self.items):
            r = self.rect(i)
            if i == self.sel and en:
                hl = pygame.Surface(r.size, pygame.SRCALPHA)
                hl.fill((40, 70, 120, 170))
                surf.blit(hl, r.topleft)
                pygame.draw.rect(surf, SKY, r, 1)
            col = (WHITE if i != self.sel else GOLD) if en else (100, 100, 110)
            self.app.text(surf, label, self.x, self.y + i * self.pitch, col, self.align)


def panel(surf, rect, alpha=190):
    p = pygame.Surface(rect.size, pygame.SRCALPHA)
    p.fill((10, 18, 40, alpha))
    surf.blit(p, rect.topleft)
    pygame.draw.rect(surf, SKY, rect, 1)


def is_back(ev):
    return ((ev.type == pygame.KEYDOWN and ev.key in (pygame.K_ESCAPE, pygame.K_BACKSPACE, pygame.K_x)) or
            (ev.type == pygame.MOUSEBUTTONDOWN and ev.button == 3))


# =========================================================================
class TitleState(State):
    def __init__(self, app):
        super().__init__(app)
        self.app.session = None
        app.audio.set_game(app.ui_game)
        app.audio.stop_all()
        g1 = app.games.get('iriya1') or app.ui_game
        self.bg_l = g1.image('title/titleL1.nnt', rotate=True)
        self.bg_r = g1.image('title/titleR1.nnt', rotate=True)
        self.logo = g1.image('title/titlelogo.nnt', rotate=True)
        g2 = app.games.get('iriya2')
        self.logo2 = g2.image('title/titlelogo.nnt', rotate=True) if g2 else None
        has_auto = 'auto' in app.saves
        items = []
        if has_auto:
            items.append(('つづきから読む', 'continue', True))
        if 'iriya1' in app.games:
            items.append(('Ｉ　はじめから読む', ('new', 'iriya1'), True))
        if 'iriya2' in app.games:
            items.append(('ＩＩ　はじめから読む', ('new', 'iriya2'), True))
        items += [('しおりから読む', 'load', bool(app.saves)),
                  ('もくじ', 'chapters', True),
                  ('カードリスト', 'cards', True),
                  ('ミニゲーム', 'minigames', True),
                  ('オプション', 'options', True),
                  ('おわる', 'quit', True)]
        self.menu = Menu(app, items, RIGHT + PAGE_W // 2, 72, 19, 'center', 150)
        self.t = 0
        bgm = 32 if 'iriya1' in app.games else None  # BGM_IRIYA
        if bgm is not None and app.audio.ok:
            app.audio.play_bgm(bgm, 60)

    def event(self, ev):
        v = self.menu.event(ev)
        if v is None:
            if ev.type == pygame.KEYDOWN and ev.key == pygame.K_ESCAPE:
                self.app.quit()
            return
        app = self.app
        if v == 'continue':
            s = app.saves['auto']
            app.start_chapter(s['game'], s['chap'], s['line'])
        elif isinstance(v, tuple) and v[0] == 'new':
            gid = v[1]
            app.start_chapter(gid, app.games[gid].chapters[0][0])
        elif v == 'load':
            app.goto(SaveLoadState(app, 'load', self))
        elif v == 'chapters':
            app.goto(ChapterState(app, self))
        elif v == 'cards':
            from .cardlist import CardListState
            app.goto(CardListState(app, self))
        elif v == 'minigames':
            app.goto(MiniMenuState(app, self))
        elif v == 'options':
            app.goto(OptionsState(app, self))
        elif v == 'quit':
            app.quit()

    def update(self):
        self.t += 1

    def draw(self, surf):
        surf.blit(self.bg_l, (0, 0), (0, 0, PAGE_W, VIEW_H))
        rw = self.bg_r.get_width()
        surf.blit(self.bg_r, (RIGHT, 0), ((rw - PAGE_W) // 2, 0, PAGE_W, VIEW_H))
        if self.logo2 is not None and 'iriya2' in self.app.games:
            for i, lg in enumerate((self.logo, self.logo2)):
                w, h = lg.get_size()
                sm = pygame.transform.smoothscale(lg, (w * 3 // 5, h * 3 // 5))
                surf.blit(sm, (RIGHT + 2 + i * (PAGE_W // 2), 30 - sm.get_height() // 2 + 10))
        else:
            surf.blit(self.logo, (RIGHT + (PAGE_W - self.logo.get_width()) // 2, 8))
        draw_spine(surf)
        panel(surf, pygame.Rect(RIGHT + 14, 65, PAGE_W - 28, len(self.menu.items) * 19 + 6), 170)
        self.menu.draw(surf)
        self.app.text(surf, 'Ｆ１１：全画面', RIGHT + PAGE_W - 6, VIEW_H - 16, GREY, 'right')


# =========================================================================
class MiniMenuState(State):
    """Pick one of the mini-games from either game."""

    def __init__(self, app, back):
        super().__init__(app)
        self.back = back
        items = []
        if 'iriya1' in app.games:
            items += [('イリヤ電撃クイズ', 'quiz', True),
                      ('電撃カード大戦', 'cards', True),
                      ('ＢＡＲＣＡＰ－Ｓ０６', 'barcap', True)]
        if 'iriya2' in app.games:
            items.append(('ブラックマンタ（ＩＩ）', 'manta', True))
        items.append(('もどる', 'back', True))
        self.menu = Menu(app, items, RIGHT + PAGE_W // 2, 60, 22, 'center', 170)

    def event(self, ev):
        app = self.app
        if is_back(ev):
            app.ui_sound('cancel')
            app.goto(self.back)
            return
        v = self.menu.event(ev)
        if v == 'back':
            app.goto(self.back)
        elif v == 'quiz':
            from .minigames.quiz import QuizState
            app.goto(QuizState(app, self.back))
        elif v == 'cards':
            from .minigames.cardbattle import CardBattleState
            app.goto(CardBattleState(app, self.back))
        elif v == 'barcap':
            from .minigames.barcap import BarcapState
            app.goto(BarcapState(app, self.back))
        elif v == 'manta':
            from .minigames.manta import MantaState
            app.goto(MantaState(app, self.back))

    def draw(self, surf):
        surf.fill((14, 22, 46))
        draw_spine(surf)
        self.app.text(surf, 'ミニゲーム', RIGHT + 8, 10, GOLD)
        panel(surf, pygame.Rect(RIGHT + 8, 50, PAGE_W - 16, len(self.menu.items) * 22 + 8), 170)
        self.menu.draw(surf)


# =========================================================================
class ChapterState(State):
    def __init__(self, app, back):
        super().__init__(app)
        self.back = back
        self.tab = 'iriya1' if 'iriya1' in app.games else 'iriya2'
        self._build()

    def _build(self):
        app = self.app
        g = app.games[self.tab]
        items = []
        for c, t in g.chapters:
            seen = f'{self.tab}:{c}' in app.read_chapters
            items.append((t, c, True))
        self.items = items
        self.sel = 0
        self.scroll = 0

    def _rows(self):
        return 13

    def _rect(self, vis_i):
        page_right = vis_i < self._rows()
        k = vis_i if page_right else vis_i - self._rows()
        x = RIGHT + 6 if page_right else 6
        return pygame.Rect(x, 36 + k * 16, PAGE_W - 12, 15)

    def event(self, ev):
        app = self.app
        if is_back(ev):
            app.ui_sound('cancel')
            app.goto(self.back)
            return
        n = len(self.items)
        if ev.type == pygame.KEYDOWN:
            if ev.key in (pygame.K_DOWN, pygame.K_s):
                self.sel = (self.sel + 1) % n
                app.ui_sound('move')
            elif ev.key in (pygame.K_UP, pygame.K_w):
                self.sel = (self.sel - 1) % n
                app.ui_sound('move')
            elif ev.key in (pygame.K_LEFT, pygame.K_RIGHT, pygame.K_TAB, pygame.K_a, pygame.K_d):
                self._switch()
            elif ev.key in (pygame.K_RETURN, pygame.K_SPACE, pygame.K_z):
                self._start(self.sel)
        elif ev.type == pygame.MOUSEMOTION:
            i = self._hit(ev.lpos)
            if i is not None:
                self.sel = i
        elif ev.type == pygame.MOUSEBUTTONDOWN and ev.button == 1:
            if self._tab_rect().collidepoint(ev.lpos):
                self._switch()
                return
            i = self._hit(ev.lpos)
            if i is not None:
                self._start(i)

    def _tab_rect(self):
        return pygame.Rect(6, VIEW_H - 24, PAGE_W - 12, 18)

    def _switch(self):
        other = 'iriya2' if self.tab == 'iriya1' else 'iriya1'
        if other in self.app.games:
            self.tab = other
            self._build()
            self.app.ui_sound('move')

    def _hit(self, pos):
        for i in range(len(self.items)):
            if self._rect(i).collidepoint(pos):
                return i
        return None

    def _start(self, i):
        self.app.ui_sound('enter')
        self.app.start_chapter(self.tab, self.items[i][1])

    def draw(self, surf):
        surf.fill((14, 22, 46))
        for x0 in (0, RIGHT):
            for y in range(0, VIEW_H, 4):
                c = 14 + y // 24
                pygame.draw.rect(surf, (c, c + 8, 46 + y // 12), (x0, y, PAGE_W, 4))
        draw_spine(surf)
        app = self.app
        app.text(surf, 'もくじ　' + GAME_TITLES[self.tab], RIGHT + 8, 10, GOLD, game=app.games[self.tab])
        for i, (label, c, en) in enumerate(self.items):
            r = self._rect(i)
            if i == self.sel:
                hl = pygame.Surface(r.size, pygame.SRCALPHA)
                hl.fill((50, 90, 150, 170))
                surf.blit(hl, r.topleft)
                pygame.draw.rect(surf, SKY, r, 1)
            seen = f'{self.tab}:{c}' in app.read_chapters
            col = GOLD if i == self.sel else (WHITE if seen else (200, 205, 215))
            app.text(surf, label, r.x + 3, r.y + 2, col, game=app.games[self.tab])
        tr = self._tab_rect()
        panel(surf, tr, 150)
        other = 'ＩＩ' if self.tab == 'iriya1' else 'Ｉ'
        if len(app.games) > 1:
            app.text(surf, f'←→　{other}へ切り替え', tr.centerx, tr.y + 3, WHITE, 'center')
        app.text(surf, '右クリック：戻る', PAGE_W - 8, 10, GREY, 'right')


# =========================================================================
class SaveLoadState(State):
    SLOTS = 12

    def __init__(self, app, mode, back):
        super().__init__(app)
        self.mode, self.back = mode, back
        self.keys = ['auto', 'quick'] + [f's{i}' for i in range(1, self.SLOTS + 1)]
        if mode == 'save':
            self.keys = [k for k in self.keys if k != 'auto']
        self.sel = 0
        self.confirm = None

    def _rect(self, i):
        per = 7
        page_right = i < per
        k = i if page_right else i - per
        x = RIGHT + 4 if page_right else 4
        return pygame.Rect(x, 28 + k * 32, PAGE_W - 8, 30)

    def _label(self, k):
        if k == 'auto':
            return 'オート'
        if k == 'quick':
            return 'クイック'
        return 'しおり' + k[1:].translate(str.maketrans('0123456789', '０１２３４５６７８９'))

    def event(self, ev):
        app = self.app
        if is_back(ev):
            app.ui_sound('cancel')
            app.goto(self.back)
            return
        n = len(self.keys)
        pick = None
        if ev.type == pygame.KEYDOWN:
            if ev.key in (pygame.K_DOWN, pygame.K_s):
                self.sel = (self.sel + 1) % n
            elif ev.key in (pygame.K_UP, pygame.K_w):
                self.sel = (self.sel - 1) % n
            elif ev.key in (pygame.K_RETURN, pygame.K_SPACE, pygame.K_z):
                pick = self.sel
            elif ev.key == pygame.K_DELETE:
                k = self.keys[self.sel]
                if k in app.saves:
                    del app.saves[k]
                    app._save_json('saves.json', app.saves)
        elif ev.type == pygame.MOUSEMOTION:
            for i in range(n):
                if self._rect(i).collidepoint(ev.lpos):
                    self.sel = i
        elif ev.type == pygame.MOUSEBUTTONDOWN and ev.button == 1:
            for i in range(n):
                if self._rect(i).collidepoint(ev.lpos):
                    pick = i
        if pick is None:
            return
        k = self.keys[pick]
        if self.mode == 'save':
            app.saves[k] = app.session.snapshot()
            app._save_json('saves.json', app.saves)
            app.ui_sound('enter')
            app.goto(self.back)
        else:
            s = app.saves.get(k)
            if s and s['game'] in app.games:
                app.ui_sound('enter')
                app.start_chapter(s['game'], s['chap'], s['line'])

    def draw(self, surf):
        app = self.app
        if app.session:
            app.session.draw(surf)
            dim = pygame.Surface(LOGICAL, pygame.SRCALPHA)
            dim.fill((0, 0, 20, 180))
            surf.blit(dim, (0, 0))
        else:
            surf.fill((14, 22, 46))
        draw_spine(surf)
        app.text(surf, 'しおりをはさむ' if self.mode == 'save' else 'しおりから読む', RIGHT + 8, 8, GOLD)
        app.text(surf, 'Ｄｅｌ：削除　右クリック：戻る', PAGE_W - 6, 8, GREY, 'right')
        for i, k in enumerate(self.keys):
            r = self._rect(i)
            panel(surf, r, 200 if i == self.sel else 130)
            if i == self.sel:
                pygame.draw.rect(surf, GOLD, r, 1)
            s = app.saves.get(k)
            app.text(surf, self._label(k), r.x + 4, r.y + 3, GOLD if i == self.sel else SKY)
            if s:
                g = app.games.get(s['game'])
                app.text(surf, s.get('time', '')[5:], r.right - 4, r.y + 3, GREY, 'right')
                t = ('Ｉ ' if s['game'] == 'iriya1' else 'ＩＩ ') + s.get('title', '')
                app.text(surf, t[:15], r.x + 4, r.y + 16, WHITE, game=g)
            else:
                app.text(surf, '－－－', r.x + 70, r.y + 16, (110, 110, 120))


from .options import OptionsState  # noqa: E402  (uses names defined above)


# =========================================================================
class PlayState(State):
    AUTO_HOLD = 60                  # DS: holding the ▼ orb for about a second turns auto on

    def __init__(self, app):
        super().__init__(app)
        self.ctrl = False
        self.toast = None
        self.toast_t = 0
        self.orb_hold = None        # frames the ▼ orb has been held down

    @property
    def ses(self):
        return self.app.session

    def notify(self, msg):
        self.toast, self.toast_t = msg, 120

    def event(self, ev):
        app, ses = self.app, self.ses
        if ev.type == pygame.KEYDOWN:
            k = ev.key
            if ses.scene.hide_text:
                ses.scene.hide_text = False
                return
            if k in (pygame.K_RETURN, pygame.K_SPACE, pygame.K_z, pygame.K_DOWN, pygame.K_KP_ENTER, pygame.K_PAGEDOWN):
                ses.auto = False if ses.auto and k != pygame.K_DOWN else ses.auto
                ses.click()
            elif k in (pygame.K_ESCAPE, pygame.K_x):
                app.ui_sound('cancel')
                app.goto(PauseState(app, self))
            elif k == pygame.K_BACKSPACE:
                if ses.go_back():
                    app.state.notify('一行戻りました')
                else:
                    self.notify('これ以上戻れません')
            elif k in (pygame.K_LCTRL, pygame.K_RCTRL):
                ses.skip = True
                self.ctrl = True
            elif k == pygame.K_TAB:
                ses.skip = not ses.skip
                self.notify('スキップ　オン' if ses.skip else 'スキップ　オフ')
            elif k == pygame.K_a:
                ses.auto = not ses.auto
                self.notify('オート　オン' if ses.auto else 'オート　オフ')
            elif k == pygame.K_h:
                ses.scene.hide_text = True
            elif k in (pygame.K_UP, pygame.K_PAGEUP):
                app.goto(BacklogState(app, self))
            elif k == pygame.K_F5:
                app.saves['quick'] = ses.snapshot()
                app._save_json('saves.json', app.saves)
                self.notify('クイックセーブしました')
            elif k == pygame.K_F9:
                s = app.saves.get('quick')
                if s:
                    app.start_chapter(s['game'], s['chap'], s['line'])
                    app.state.notify('クイックロードしました')
        elif ev.type == pygame.KEYUP:
            if ev.key in (pygame.K_LCTRL, pygame.K_RCTRL) and self.ctrl:
                ses.skip = False
                self.ctrl = False
        elif ev.type == pygame.MOUSEBUTTONDOWN:
            if ses.scene.hide_text:
                ses.scene.hide_text = False
                return
            if ev.button == 1:
                if self._click(ev.lpos):
                    return
                ses.auto = False
                ses.click()
            elif ev.button == 3:
                app.ui_sound('cancel')
                app.goto(PauseState(app, self))
            elif ev.button == 4:
                app.goto(BacklogState(app, self))
            elif ev.button == 5:
                ses.click()

    def _click(self, pos):
        """HUD buttons and keyword taps. Returns True if the click was consumed."""
        app, ses = self.app, self.ses
        x, y = pos
        if x >= RIGHT:
            act = ses.hud.hit_right((x - RIGHT, y))
            if act == 'orb':
                if ses.auto:
                    ses.auto = False
                    self.notify('オート　オフ')
                    return True
                self.orb_hold = 0           # a tap goes on to the next line; a long hold turns auto on
                return False
            if act:
                app.ui_sound('enter')
                if act == 'log':
                    app.goto(BacklogState(app, self))
                elif act == 'cards':
                    from .cardlist import CardListState
                    app.goto(CardListState(app, self, ses.gid))
                elif act == 'hide':
                    ses.scene.hide_text = True
                elif act == 'menu':
                    app.goto(PauseState(app, self))
                return True
            if ses.click_text((x - RIGHT, y)):
                return True
        elif x < PAGE_W and ses.hud.hit_left((x, y)) == 'radar':
            # hint: briefly flash the hidden TIPS words on this page
            if ses.scene.visible_new_keywords():
                ses.scene.reveal_t = 48
                ses.scene._text_dirty = True
            return True
        return False

    def update(self):
        ses = self.ses
        if not (pygame.key.get_mods() & pygame.KMOD_CTRL) and self.ctrl:
            ses.skip = False
            self.ctrl = False
        if self.orb_hold is not None:
            if pygame.mouse.get_pressed()[0] and not ses.auto:
                self.orb_hold += 1
                if self.orb_hold >= self.AUTO_HOLD:
                    ses.auto = True
                    self.orb_hold = None
                    self.notify('オート　オン')
            else:
                self.orb_hold = None
        ses.update()
        if self.toast_t:
            self.toast_t -= 1
        if ses.vm.finished:
            nxt = self.app.next_chapter(ses.gid, ses.chap)
            if nxt is None:
                self.app.saves.pop('auto', None)
                self.app._save_json('saves.json', self.app.saves)
                self.app.goto(TitleState(self.app))
            else:
                skip, auto = ses.skip, ses.auto
                self.app.start_chapter(*nxt)
                self.app.session.skip = skip and self.ctrl
                self.app.session.auto = auto
                self.app.session.autosave()

    def draw_hires(self, window, r, k):
        self.ses.draw_hires(window, r, k)

    def draw_art(self, window, r, k):
        return self.ses.draw_art(window, r, k)

    def draw(self, surf):
        ses = self.ses
        art = self.app.art_hd() and ses.title_card <= 0
        if art:
            # the pages are drawn at window resolution by draw_art; everything
            # laid over them goes on a transparent overlay drawn after
            if getattr(self, 'overlay', None) is None:
                self.overlay = pygame.Surface(LOGICAL, pygame.SRCALPHA)
            self.overlay.fill((0, 0, 0, 0))
            draw_spine(surf)
            surf = self.overlay
        else:
            ses.draw(surf, hires=self.app.hires_active())
            draw_spine(surf)
        flags = []
        if ses.skip:
            flags.append('ＳＫＩＰ')
        if ses.auto:
            flags.append('ＡＵＴＯ')
        if flags:
            self.app.text(surf, '　'.join(flags), RIGHT + PAGE_W - 4, 4, GOLD, 'right')
        ses.draw_popups(surf)
        if self.toast_t:
            r = pygame.Rect(0, 0, 150, 20)
            r.center = (LOGICAL[0] // 2, 20)
            panel(surf, r, 200)
            self.app.text(surf, self.toast, r.centerx, r.y + 4, WHITE, 'center')


class PauseState(State):
    def __init__(self, app, play):
        super().__init__(app)
        self.play = play
        items = [('つづける', 'resume', True), ('しおりをはさむ', 'save', True),
                 ('しおりから読む', 'load', True), ('履歴', 'log', True),
                 ('オート', 'auto', True), ('カードリスト', 'cards', True),
                 ('オプション', 'options', True),
                 ('もくじ', 'chapters', True), ('タイトルへ', 'title', True)]
        self.menu = Menu(app, items, RIGHT + PAGE_W // 2, 40, 22, 'center', 140)

    def event(self, ev):
        app = self.app
        if is_back(ev):
            app.ui_sound('cancel')
            app.goto(self.play)
            return
        v = self.menu.event(ev)
        if v == 'resume':
            app.goto(self.play)
        elif v == 'save':
            app.goto(SaveLoadState(app, 'save', self))
        elif v == 'load':
            app.goto(SaveLoadState(app, 'load', self))
        elif v == 'log':
            app.goto(BacklogState(app, self.play))
        elif v == 'auto':
            app.session.auto = True
            app.goto(self.play)
        elif v == 'options':
            app.goto(OptionsState(app, self))
        elif v == 'cards':
            from .cardlist import CardListState
            app.goto(CardListState(app, self, app.session.gid))
        elif v == 'chapters':
            app.session.autosave()
            app.goto(ChapterState(app, self))
        elif v == 'title':
            app.session.autosave()
            app.goto(TitleState(app))

    def draw(self, surf):
        self.app.session.draw(surf)
        dim = pygame.Surface(LOGICAL, pygame.SRCALPHA)
        dim.fill((0, 0, 20, 150))
        surf.blit(dim, (0, 0))
        draw_spine(surf)
        ses = self.app.session
        panel(surf, pygame.Rect(RIGHT + 18, 30, PAGE_W - 36, len(self.menu.items) * 22 + 10))
        self.menu.draw(surf)
        self.app.vtext(surf, GAME_TITLES[ses.gid], COL_X0, 16, GREY, game=ses.game)
        self.app.vtext(surf, ses.title, COL_X0 - COL_PITCH, 30, WHITE, game=ses.game)


class BacklogState(State):
    def __init__(self, app, play):
        super().__init__(app)
        self.play = play
        sc = app.session.scene
        self.pages = sc.backlog_pages()
        self.i = max(0, len(self.pages) - 1)
        self._cache = {}

    def event(self, ev):
        app = self.app
        n = len(self.pages)
        if is_back(ev) or (ev.type == pygame.KEYDOWN and ev.key in (pygame.K_RETURN, pygame.K_SPACE)):
            app.goto(self.play)
            return
        if ev.type == pygame.KEYDOWN:
            if ev.key in (pygame.K_UP, pygame.K_PAGEUP, pygame.K_RIGHT):
                self.i = max(0, self.i - 1)
            elif ev.key in (pygame.K_DOWN, pygame.K_PAGEDOWN, pygame.K_LEFT):
                if self.i >= n - 1:
                    app.goto(self.play)
                self.i = min(n - 1, self.i + 1)
        elif ev.type == pygame.MOUSEBUTTONDOWN:
            if ev.button == 4:
                self.i = max(0, self.i - 1)
            elif ev.button == 5:
                if self.i >= n - 1:
                    app.goto(self.play)
                self.i = min(n - 1, self.i + 1)
            elif ev.button == 1:
                app.goto(self.play)

    def draw(self, surf):
        ses = self.app.session
        surf.fill((10, 16, 34))
        draw_spine(surf)
        if not self.pages:
            return
        # right page = selected page, left page = the following page (book order)
        for slot, idx in ((RIGHT, self.i - 1), (0, self.i)):
            if 0 <= idx < len(self.pages) and not self.app.hires_active():
                s = self._cache.get(idx)
                if s is None:
                    s = ses.scene.render_columns(self.pages[idx])
                    self._cache[idx] = s
                surf.blit(s, (slot, 0))
        self.app.text(surf, f'履歴　{self.i + 1}／{len(self.pages)}', PAGE_W - 6, VIEW_H - 15, GREY, 'right')

    def draw_hires(self, window, r, k):
        sc = self.app.session.scene
        for slot, idx in ((RIGHT, self.i - 1), (0, self.i)):
            if 0 <= idx < len(self.pages):
                self.app.hires.draw_page(window, sc, self.pages[idx], (r.x + slot * k, r.y), k)


def main(argv=None):
    logging.basicConfig(level=logging.INFO if os.environ.get('IRIYA_DEBUG') else logging.WARNING)
    argv = argv if argv is not None else sys.argv[1:]
    App(argv).run()

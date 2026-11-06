"""Game II mini-game: the dual-screen Black Manta / Iriya shooter (mini/s2).

From the ROM data and the game's built-in help pages (bg/help1-2):
  * Top screen: the Black Manta flies the sky stage. ✚ move, A shot (hold to
    charge), X transform (go to the bottom screen), Y bomb.
  * Bottom screen: Iriya on foot in the ground stage. ✚ move / aim, A shot,
    B jump, X transform (go to the top screen), Y bomb.
  * Transforming needs energy; up to 3 stocks. A bomb clears every enemy on
    both screens and is limited.
  * Items: S speed up, P shot power up, E energy, B bomb. Some enemies drop
    the よかったマーク; each one collected adds a bonus at stage clear.
  * Stages: stage/skyNN.fmf + grdNN.fmf (FMF maps, 2400x12 / 2400x10 chips of
    16x16, 3 layers). Layer 2 places enemies: value = unit*10 + pattern, where
    unit indexes the stage's unit list (from the ARM9 table); 251 marks solid
    ground; 252/253 drop items; 254 marks checkpoints. Then the boss stage
    (b_skyNN/b_grdNN) with bossNN.
Enemy movement patterns are not stored as data in the ROM; the patterns here
are written for this port from the placements and sprites.
"""
import math
import random
import struct
import numpy as np
import pygame

from ..assets import _surface
from . import MiniState, PauseMenu, SCREEN_W, TOP_H, BOTTOM_Y

ROOT = 'mini/s2/'
UNITS = {
    1: (['sky1_ufo1_n', 'sky1_ufo1_r', 'sky1_ufo2', 'sky1_ramen', 'sky1_cannon', 'sky1_gyoza'],
        ['grd1_bowling', 'grd1_card', 'grd1_ouen', 'grd1_ramen', 'grd1_gyoza']),
    2: (['sky2_v_kinoko', 'sky2_kinoko_r', 'sky2_kinoko_b', 'sky2_bat', 'sky2_k_cannon'],
        ['grd2_mata_r', 'grd2_mata_b', 'grd2_k_cannon', 'grd2_rock', 'grd2_v_kinoko', 'grd2_bat']),
    3: (['sky3_ufo1', 'sky3_ufo2', 'sky3_chusya', 'sky3_drug_r', 'sky3_drug_b'],
        ['grd3_grey', 'grd3_hohei', 'grd3_tate', 'grd3_mib', 'grd3_para']),
}
SCROLL = 1.5
MAP_H_SKY, MAP_H_GRD = 12, 10
LIVES, BOMBS = 3, 3
ENERGY_PER_STOCK = 100
SE_MOVE, SE_CANCEL, SE_ENTER, SE_CARDGET = 0, 1, 2, 5
SE_HIT = (8, 9, 10, 11)
WHITE = (255, 255, 255)
CYAN = (110, 190, 255)
ITEM_P, ITEM_S, ITEM_B, ITEM_E, ITEM_YOKATTA = range(5)


def load_pal(rom, path):
    d = rom.read(path)
    v = np.frombuffer(d, '<u2').astype(np.uint32)
    return np.stack([(v & 31) * 255 // 31, ((v >> 5) & 31) * 255 // 31, ((v >> 10) & 31) * 255 // 31], 1).astype(np.uint8)


def frames(rom, path, pal, w, h, skip_blank=True):
    d = np.frombuffer(rom.read(path), np.uint8)
    n = len(d) // (w * h)
    out = []
    for i in range(n):
        idx = d[i * w * h:(i + 1) * w * h].reshape(h // 8, w // 8, 8, 8).transpose(0, 2, 1, 3).reshape(h, w)
        if skip_blank and not idx.any():
            continue
        rgba = np.dstack([pal[np.minimum(idx, len(pal) - 1)], np.where(idx == 0, 0, 255).astype(np.uint8)])
        out.append(_surface(rgba))
    return out


def chipset(rom, path, pal):
    """256 16x16 map chips cut from a 256x256 8bpp sheet stored as 8x8 tiles, row-major."""
    d = np.frombuffer(rom.read(path), np.uint8)
    tiles = d.reshape(-1, 8, 8)
    tw = 32
    th = len(tiles) // tw
    img = tiles[:tw * th].reshape(th, tw, 8, 8).transpose(0, 2, 1, 3).reshape(th * 8, tw * 8)
    out = []
    for k in range((th * 8 // 16) * 16):
        r, c = divmod(k, 16)
        idx = img[r * 16:r * 16 + 16, c * 16:c * 16 + 16]
        rgba = np.dstack([pal[idx], np.where(idx == 0, 0, 255).astype(np.uint8)])
        out.append(_surface(np.ascontiguousarray(rgba)))
    return out


def screen4bpp(rom, name, w=32):
    """bg/NAME.nbfc + .nbfp + .nbfs (4bpp BG with map) -> Surface."""
    pal = load_pal(rom, ROOT + f'bg/{name}.nbfp')
    ch = np.frombuffer(rom.read(ROOT + f'bg/{name}.nbfc'), np.uint8)
    t4 = np.empty(ch.size * 2, np.uint8)
    t4[0::2], t4[1::2] = ch & 15, ch >> 4
    tiles = t4.reshape(-1, 8, 8)
    m = np.frombuffer(rom.read(ROOT + f'bg/{name}.nbfs'), '<u2')
    h = len(m) // w
    img = np.zeros((h * 8, w * 8), np.uint8)
    for i, e in enumerate(m):
        t = tiles[(e & 0x3ff) % len(tiles)]
        if e & 0x400:
            t = t[:, ::-1]
        if e & 0x800:
            t = t[::-1]
        img[(i // w) * 8:(i // w) * 8 + 8, (i % w) * 8:(i % w) * 8 + 8] = t
    rgba = np.dstack([pal[img], np.full(img.shape, 255, np.uint8)])
    return _surface(rgba)


def linear4bpp(rom, name, w=256):
    pal = load_pal(rom, ROOT + f'bg/{name}.nbfp')
    ch = np.frombuffer(rom.read(ROOT + f'bg/{name}.nbfc'), np.uint8)
    t4 = np.empty(ch.size * 2, np.uint8)
    t4[0::2], t4[1::2] = ch & 15, ch >> 4
    tiles = t4.reshape(-1, 8, 8)
    tw = w // 8
    th = len(tiles) // tw
    img = tiles[:tw * th].reshape(th, tw, 8, 8).transpose(0, 2, 1, 3).reshape(th * 8, tw * 8)
    rgba = np.dstack([pal[img], np.full(img.shape, 255, np.uint8)])
    return _surface(rgba)


class FMF:
    def __init__(self, data):
        _, _, self.w, self.h, self.cw, self.ch, self.layers, bits = struct.unpack_from('<4sIIIBBBB', data, 0)
        n = self.w * self.h
        self.layer = [np.frombuffer(data, np.uint8, n, 20 + k * n).reshape(self.h, self.w)
                      for k in range(self.layers)]


class Layer:
    """One screen's stage: map, chip set, units, bullets."""

    def __init__(self, rom, stage, sky, boss=False):
        tag = ('sky' if sky else 'grd') + f'{stage:02d}'
        self.sky = sky
        self.map = FMF(rom.read(ROOT + f'stage/{"b_" if boss else ""}{tag}.fmf'))
        cs = tag + ('_b' if boss and stage == 3 else '')
        pal = load_pal(rom, ROOT + f'bg/256/{cs}.nbfp')
        self.chips = chipset(rom, ROOT + f'bg/256/{cs}.nbfc', pal)
        upal = load_pal(rom, ROOT + f'unit/{tag}.nbfp')
        names = UNITS[stage][0 if sky else 1]
        self.units = []
        for n in names:
            size = len(rom.read(ROOT + f'unit/{n}.nbfc'))
            dim = 64 if size >= 16384 else 32
            fr = frames(rom, ROOT + f'unit/{n}.nbfc', upal, dim, dim)
            self.units.append(fr[2:] if len(fr) > 3 else fr)
        bpal = load_pal(rom, ROOT + f'bullet/{tag}.nbfp')
        self.bullets = []
        for k in range(1, 6):
            p = ROOT + f'bullet/{tag}_{k:02d}.nbfc'
            if p in rom.files:
                self.bullets += frames(rom, p, bpal, 16, 16)
        self.spawned_col = -1

    def solid(self, col, row):
        """Ground collision: the road (bottom map row) and cells marked 251 in layer 2."""
        if row >= self.map.h - 1:
            return True
        if row < 0:
            return False
        col %= self.map.w
        return self.map.layer[2][row, col] == 251

    def draw(self, surf, scroll, y0=0):
        c0 = int(scroll // 16)
        ox = -int(scroll % 16)
        for c in range(18):
            col = (c0 + c) % self.map.w
            for r in range(self.map.h):
                for L in (0, 1):
                    v = self.map.layer[L][r, col]
                    if v != 255 and v < len(self.chips):
                        surf.blit(self.chips[v], (ox + c * 16, y0 + r * 16))


class Actor:
    __slots__ = ('x', 'y', 'vx', 'vy', 'hp', 'kind', 'pat', 'frames', 't', 'alive', 'sky', 'fire_t',
                 'drop', 'size', 'base_y')

    def __init__(self, x, y, frames, sky, kind=0, pat=1, hp=2):
        self.x, self.y, self.vx, self.vy = float(x), float(y), 0.0, 0.0
        self.frames, self.sky, self.kind, self.pat, self.hp = frames, sky, kind, pat, hp
        self.t = 0
        self.alive = True
        self.fire_t = random.randint(60, 150)
        self.drop = None
        self.size = frames[0].get_width() if frames else 32
        self.base_y = float(y)


class MantaState(MiniState):
    def __init__(self, app, back):
        super().__init__(app, back)
        self.game = g = app.games['iriya2']
        rom = g.rom
        self.rom = rom
        bmpal = load_pal(rom, ROOT + 'unit/bm.nbfp')
        bm = frames(rom, ROOT + 'unit/bm.nbfc', bmpal, 64, 32)
        self.manta_fly = bm[0:4]
        self.manta_boom = bm[4:7]
        ipal = load_pal(rom, ROOT + 'unit/iriya.nbfp')
        self.iriya = frames(rom, ROOT + 'unit/iriya.nbfc', ipal, 64, 64)
        ppal = load_pal(rom, ROOT + 'bullet/bm_p1.nbfp')
        self.shot = frames(rom, ROOT + 'bullet/bm01.nbfc', ppal, 16, 16)
        self.shot2 = frames(rom, ROOT + 'bullet/bm02.nbfc', load_pal(rom, ROOT + 'bullet/bm_p2.nbfp'), 16, 16)
        self.charge = frames(rom, ROOT + 'bullet/bm03.nbfc', load_pal(rom, ROOT + 'bullet/bm_p3.nbfp'), 64, 32)
        self.boom = frames(rom, ROOT + 'bullet/explode.nbfc', load_pal(rom, ROOT + 'bullet/explode.nbfp'), 32, 32)
        self.items = frames(rom, ROOT + 'item/item.nbfc', load_pal(rom, ROOT + 'item/item.nbfp'), 16, 16)[:5]
        self.help = [screen4bpp(rom, 'help1'), screen4bpp(rom, 'help2')]
        try:
            self.title_top = linear4bpp(rom, 'title01')
            self.title_bot = linear4bpp(rom, 'title02')
        except Exception:
            self.title_top = self.title_bot = None
        self.data = app.minigame.setdefault('manta', {'ranking': [50000, 40000, 30000, 20000, 10000]})
        self.phase = 'title'
        self.menu_sel = 0
        self.help_page = 0
        self.t = 0
        app.audio.set_game(g)
        app.audio.play_bgm(self._bgm('BGM_KESSEN', 6), 30)

    def _bgm(self, name, default):
        names = self.game.sdat.names
        return names.index(name) if name in names else default

    # ------------------------------------------------------------ game flow
    def start(self):
        self.stage = 1
        self.score = 0
        self.lives = LIVES
        self.bombs = BOMBS
        self.energy = 0
        self.stocks = 1
        self.power = 1
        self.speed = 2.0
        self.begin_stage()

    def begin_stage(self, boss=False):
        rom = self.rom
        self.boss_phase = boss
        self.skyL = Layer(rom, self.stage, True, boss)
        self.grdL = Layer(rom, self.stage, False, boss)
        self.scroll = 0.0
        self.mode = 'sky'
        self.mx, self.my = 40.0, 90.0          # manta (top screen)
        self.ix, self.iy, self.ivy = 40.0, 40.0, 0.0   # iriya (bottom screen)
        self.facing = 1
        self.enemies, self.ebullets, self.shots, self.booms, self.pickups = [], [], [], [], []
        self.yokatta = 0 if not boss else getattr(self, 'yokatta', 0)
        self.charge_t = 0
        self.invuln = 90
        self.flash = 0
        self.phase = 'play'
        self.t = 0
        self.boss = None
        if boss:
            self.spawn_boss()
        self.app.audio.play_bgm(self._bgm('BGM_IKARIBAKUHATU' if boss else 'BGM_KESSEN', 7), 30)

    def spawn_boss(self):
        rom = self.rom
        s = self.stage
        names = {1: ('boss01', 'boss01'), 2: ('boss02', 'boss02'), 3: ('boss03_face', 'boss03s')}[s]
        pal = load_pal(rom, ROOT + f'unit/{names[1]}.nbfp')
        size = len(rom.read(ROOT + f'unit/{names[0]}.nbfc'))
        fr = frames(rom, ROOT + f'unit/{names[0]}.nbfc', pal, 64, 64)
        b = Actor(SCREEN_W - 70, 60, fr or self.manta_fly, True, kind=-1, pat=0, hp=120 + 60 * s)
        b.fire_t = 60
        self.boss = b
        self.enemies.append(b)

    # ------------------------------------------------------------ spawning
    def spawn_columns(self, L):
        col = int((self.scroll + SCREEN_W) // 16)
        while L.spawned_col < col:
            L.spawned_col += 1
            c = L.spawned_col
            if c >= L.map.w or self.boss_phase:
                break
            for r in range(L.map.h):
                v = int(L.map.layer[2][r, c])
                if v == 255 or v == 251 or v == 254:
                    continue
                x = c * 16 - self.scroll
                y = r * 16
                if v in (252, 253) or v >= 200:
                    item = ITEM_YOKATTA if v == 253 else random.choice((ITEM_P, ITEM_S, ITEM_B, ITEM_E))
                    self.pickups.append([x, y, L.sky, item])
                    continue
                u, p = v // 10 - 1, v % 10
                if u < 0:
                    self.pickups.append([x, y, L.sky, v % 4])
                    continue
                fr = L.units[u % len(L.units)]
                e = Actor(x, y, fr, L.sky, kind=u, pat=p, hp=1 + (u % 3))
                if random.random() < 0.12:
                    e.drop = ITEM_YOKATTA if random.random() < 0.5 else random.randrange(4)
                self.enemies.append(e)

    # ------------------------------------------------------------ helpers
    @property
    def px(self):
        return self.mx if self.mode == 'sky' else self.ix

    @property
    def py(self):
        return self.my if self.mode == 'sky' else self.iy

    def player_rect(self):
        if self.mode == 'sky':
            return pygame.Rect(self.mx - 20, self.my - 8, 40, 16)
        return pygame.Rect(self.ix - 10, self.iy - 26, 20, 40)

    def fire_bullet(self, e, speed=1.6):
        if e.sky != (self.mode == 'sky'):
            return
        dx, dy = self.px - e.x, self.py - e.y
        d = math.hypot(dx, dy) or 1
        L = self.skyL if e.sky else self.grdL
        img = L.bullets[0] if L.bullets else None
        self.ebullets.append([e.x, e.y, dx / d * speed, dy / d * speed, e.sky, img])

    def kill(self, e, score=100):
        e.alive = False
        self.booms.append([e.x, e.y, 0, e.sky])
        self.score += score
        self.energy = min(ENERGY_PER_STOCK * 3, self.energy + 6)
        if self.energy >= ENERGY_PER_STOCK and self.stocks < 3:
            self.energy -= ENERGY_PER_STOCK
            self.stocks += 1
        if e.drop is not None:
            self.pickups.append([e.x, e.y, e.sky, e.drop])
        self.sfx(random.choice(SE_HIT), self.game)
        if e is self.boss:
            self.boss = None
            self.score += 10000 * self.stage
            self.phase = 'clear'
            self.t = 0
            self.app.audio.play_bgm(self._bgm('BGM_NITIJO', 25), 0)

    def hurt(self):
        if self.invuln:
            return
        self.booms.append([self.px, self.py, 0, self.mode == 'sky'])
        self.lives -= 1
        self.invuln = 120
        self.power = max(1, self.power - 1)
        self.sfx(SE_CANCEL, self.game)
        if self.lives < 0:
            self.phase = 'over'
            self.t = 0
            self.record()

    def bomb(self):
        if self.bombs <= 0:
            return
        self.bombs -= 1
        self.flash = 30
        for e in list(self.enemies):
            if e.alive:
                if e is self.boss:
                    e.hp -= 30
                    if e.hp <= 0:
                        self.kill(e, 0)
                else:
                    self.kill(e)
        self.ebullets = []

    def transform(self):
        if self.stocks <= 0:
            return
        self.stocks -= 1
        self.mode = 'ground' if self.mode == 'sky' else 'sky'
        self.invuln = max(self.invuln, 40)
        self.sfx(SE_ENTER, self.game)

    def record(self):
        rk = self.data.setdefault('ranking', [])
        rk.append(self.score)
        rk.sort(reverse=True)
        del rk[5:]
        self.app.save_progress()

    # ------------------------------------------------------------ input
    def event(self, ev):
        A = self.A
        key = ev.key if ev.type == pygame.KEYDOWN else None
        confirm = key in (pygame.K_RETURN, pygame.K_SPACE, pygame.K_z)
        click = ev.lpos if ev.type == pygame.MOUSEBUTTONDOWN and ev.button == 1 else None
        if self.phase == 'title':
            items = 4
            if key in (pygame.K_UP, pygame.K_w):
                self.menu_sel = (self.menu_sel - 1) % items
            elif key in (pygame.K_DOWN, pygame.K_s):
                self.menu_sel = (self.menu_sel + 1) % items
            elif confirm or click:
                if click:
                    p = self.bottom_pos(click)
                    if not p or not 0 <= (p[1] - 60) // 28 < items:
                        return
                    self.menu_sel = (p[1] - 60) // 28
                self.sfx(SE_ENTER, self.game)
                if self.menu_sel == 0:
                    self.start()
                elif self.menu_sel == 1:
                    self.phase, self.help_page = 'help', 0
                elif self.menu_sel == 2:
                    self.phase = 'ranking'
                else:
                    self.leave()
            elif A.is_back(ev):
                self.leave()
            return
        if self.phase == 'help':
            if confirm or click:
                self.help_page += 1
                if self.help_page >= len(self.help):
                    self.phase = 'title'
            elif A.is_back(ev):
                self.phase = 'title'
            return
        if self.phase == 'ranking':
            if confirm or click or A.is_back(ev):
                self.phase = 'title'
            return
        if self.phase in ('over', 'end') and self.t > 40 and (confirm or click or A.is_back(ev)):
            self.phase = 'title'
            self.app.audio.play_bgm(self._bgm('BGM_KESSEN', 6), 30)
            return
        if self.phase == 'clear' and self.t > 60 and (confirm or click):
            self.score += self.yokatta * 1000
            self.yokatta = 0
            if self.stage >= 3:
                self.phase = 'end'
                self.t = 0
                self.record()
            else:
                self.stage += 1
                self.begin_stage()
            return
        if self.phase == 'play':
            if key in (pygame.K_c, pygame.K_l):
                self.transform()
            elif key in (pygame.K_v, pygame.K_k):
                self.bomb()
            elif key in (pygame.K_x, pygame.K_i) and self.mode == 'ground' and self.on_ground():
                self.ivy = -5.2
            elif key in (pygame.K_ESCAPE, pygame.K_p):
                self.phase = 'pause'
                self.pause_menu = PauseMenu()
            elif click:
                bx = pygame.Rect(SCREEN_W - 40, BOTTOM_Y + TOP_H - 30, 36, 26)
                if bx.collidepoint(click):
                    self.bomb()
        elif self.phase == 'pause':
            act = self.pause_menu.event(ev, self.bottom_pos)
            if act == 'resume':
                self.phase = 'play'
            elif act == 'title':
                self.phase = 'title'
                self.app.audio.play_bgm(self._bgm('BGM_KESSEN', 6), 30)
            elif act == 'exit':
                self.leave()

    # ------------------------------------------------------------ update
    def on_ground(self):
        L = self.grdL
        col = int((self.scroll + self.ix) // 16)
        row = int((self.iy + 14) // 16)
        return L.solid(col, row) and self.ivy >= 0

    def update(self):
        self.t += 1
        if self.phase != 'play':
            return
        keys = pygame.key.get_pressed()
        kx = (keys[pygame.K_RIGHT] or keys[pygame.K_d]) - (keys[pygame.K_LEFT] or keys[pygame.K_a])
        ky = (keys[pygame.K_DOWN] or keys[pygame.K_s]) - (keys[pygame.K_UP] or keys[pygame.K_w])
        fire = keys[pygame.K_z] or keys[pygame.K_SPACE] or keys[pygame.K_j]
        if self.invuln:
            self.invuln -= 1
        if self.flash:
            self.flash -= 1
        # scrolling and spawning
        if not self.boss_phase:
            self.scroll += SCROLL
            for L in (self.skyL, self.grdL):
                self.spawn_columns(L)
            if self.scroll >= (self.skyL.map.w - 16) * 16:
                self.begin_stage(boss=True)
                return
        else:
            self.scroll += SCROLL * 0.5
        # player
        if self.mode == 'sky':
            self.mx = max(16, min(SCREEN_W - 24, self.mx + kx * self.speed))
            self.my = max(10, min(TOP_H - 10, self.my + ky * self.speed))
            if fire:
                self.charge_t += 1
                if self.charge_t % max(4, 10 - self.power * 2) == 1:
                    for k in range(min(self.power, 3)):
                        self.shots.append([self.mx + 24, self.my + (k - (min(self.power, 3) - 1) / 2) * 6,
                                           7.0, 0.0, True, 1, self.shot[0] if self.shot else None])
            else:
                if self.charge_t > 50 and self.charge:
                    self.shots.append([self.mx + 30, self.my, 6.0, 0.0, True, 8, self.charge[0]])
                self.charge_t = 0
        else:
            self.ix = max(12, min(SCREEN_W - 20, self.ix + kx * self.speed * 0.9))
            if kx:
                self.facing = kx
            self.ivy = min(6.0, self.ivy + 0.3)
            self.iy += self.ivy
            col = int((self.scroll + self.ix) // 16)
            row = int((self.iy + 14) // 16)
            if self.ivy >= 0 and self.grdL.solid(col, row):
                self.iy = row * 16 - 14
                self.ivy = 0.0
            if self.iy > MAP_H_GRD * 16 + 40:
                self.hurt()
                self.iy, self.ivy = 20.0, 0.0
            if fire and self.t % max(5, 12 - self.power * 2) == 0:
                dx, dy = (self.facing if not ky or kx else 0), (ky if ky < 0 else 0)
                if dx == 0 and dy == 0:
                    dx = self.facing
                n = math.hypot(dx, dy) or 1
                self.shots.append([self.ix + dx * 16, self.iy - 10 + dy * 16, 6 * dx / n, 6 * dy / n, False, 1,
                                   self.shot2[0] if self.shot2 else None])
        # enemies
        for e in self.enemies:
            if not e.alive:
                continue
            e.t += 1
            if e is self.boss:
                e.y = 60 + math.sin(e.t / 50) * 50
                e.sky = self.mode == 'sky'
                e.fire_t -= 1
                if e.fire_t <= 0:
                    for k in (-2, -1, 0, 1, 2):
                        ang = math.atan2(self.py - e.y, self.px - e.x) + k * 0.25
                        self.ebullets.append([e.x, e.y, math.cos(ang) * 1.6, math.sin(ang) * 1.6, e.sky,
                                              (self.skyL if e.sky else self.grdL).bullets[0]
                                              if (self.skyL if e.sky else self.grdL).bullets else None])
                    e.fire_t = 70
                continue
            self.move_enemy(e)
            e.fire_t -= 1
            if e.fire_t <= 0 and 0 < e.x < SCREEN_W:
                self.fire_bullet(e)
                e.fire_t = random.randint(90, 180)
            if e.x < -64 or e.y > 300 or e.y < -80:
                e.alive = False
        # shots
        for s in self.shots:
            s[0] += s[2]
            s[1] += s[3]
            for e in self.enemies:
                if e.alive and e.sky == s[4] and abs(e.x - s[0]) < e.size / 2 and abs(e.y - s[1]) < e.size / 2:
                    e.hp -= s[5] + self.power // 2
                    if s[5] < 8:
                        s[0] = 9999
                    if e.hp <= 0:
                        self.kill(e, 100 * (1 + e.kind) if e is not self.boss else 0)
                    break
        self.shots = [s for s in self.shots if -20 < s[0] < SCREEN_W + 40 and -20 < s[1] < TOP_H + 20]
        # enemy bullets and bodies vs player
        pr = self.player_rect()
        sky = self.mode == 'sky'
        for b in self.ebullets:
            b[0] += b[2]
            b[1] += b[3]
            if b[4] == sky and pr.collidepoint(b[0], b[1]):
                b[0] = -999
                self.hurt()
        self.ebullets = [b for b in self.ebullets if -16 < b[0] < SCREEN_W + 16 and -16 < b[1] < TOP_H + 16]
        for e in self.enemies:
            if e.alive and e.sky == sky and e is not self.boss and pr.colliderect(
                    pygame.Rect(e.x - e.size / 3, e.y - e.size / 3, e.size * 2 / 3, e.size * 2 / 3)):
                self.hurt()
        self.enemies = [e for e in self.enemies if e.alive]
        # pickups
        for p in self.pickups:
            p[0] -= SCROLL if not self.boss_phase else 0
            if p[2] == sky and pr.inflate(10, 10).collidepoint(p[0], p[1]):
                self.take(p[3])
                p[0] = -999
        self.pickups = [p for p in self.pickups if p[0] > -20]
        for bm in self.booms:
            bm[2] += 1
        self.booms = [b for b in self.booms if b[2] < 24]

    def move_enemy(self, e):
        p = e.pat
        sp = SCROLL
        if e.sky:
            if p == 1:
                e.x -= sp + 1.0
            elif p == 2:
                e.x -= sp + 0.6
                e.y = e.base_y + math.sin(e.t / 18) * 24
            elif p == 3:
                e.x -= sp + (2.0 if e.t < 40 else -0.3 if e.t < 120 else 2.5)
            elif p == 4:
                e.x -= sp + 0.8
                e.y += max(-1.2, min(1.2, (self.my - e.y) * 0.03))
            elif p == 5:
                e.x -= sp + 2.4
            else:
                e.x -= sp + 0.4
                e.y = e.base_y + math.sin(e.t / 30) * 40
        else:
            L = self.grdL
            e.x -= sp + (0.5 if p in (1, 3) else 0.0 if p == 2 else 0.3)
            e.vy = min(5.0, e.vy + 0.3)
            e.y += e.vy
            col = int((self.scroll + e.x) // 16)
            row = int((e.y + e.size / 2 - 2) // 16)
            if L.solid(col, row):
                e.y = row * 16 - e.size / 2 + 2
                e.vy = 0

    def take(self, item):
        if item == ITEM_P:
            self.power = min(4, self.power + 1)
        elif item == ITEM_S:
            self.speed = min(3.4, self.speed + 0.4)
        elif item == ITEM_B:
            self.bombs = min(9, self.bombs + 1)
        elif item == ITEM_E:
            self.stocks = min(3, self.stocks + 1)
        else:
            self.yokatta += 1
        self.score += 100
        self.sfx(SE_CARDGET if item == ITEM_YOKATTA else SE_ENTER, self.game)

    # ------------------------------------------------------------ drawing
    def text(self, surf, s, x, y, color=WHITE, align='left'):
        g = self.game
        idxs = g.text_glyphs(s)
        w = 12 * len(idxs)
        if align == 'center':
            x -= w // 2
        for i, gi in enumerate(idxs):
            if gi is None or gi == 614:
                continue
            self.A.Scene._outlined(surf, g.glyph(gi, vertical=False), x + 12 * i, y, color, (0, 0, 0))

    def draw(self, surf):
        surf.fill((0, 0, 0))
        top, bot = self.top, self.bottom
        if self.phase in ('title', 'help', 'ranking'):
            self.draw_title(top, bot)
            self.draw_gap(surf)
            return
        self.skyL.draw(top, self.scroll)
        bot.fill((10, 10, 20))
        self.grdL.draw(bot, self.scroll)
        for e in self.enemies:
            dst = top if e.sky else bot
            fr = e.frames[(e.t // 8) % len(e.frames)] if e.frames else None
            if fr is not None:
                dst.blit(fr, (e.x - fr.get_width() / 2, e.y - fr.get_height() / 2))
        for p in self.pickups:
            if p[3] < len(self.items):
                (top if p[2] else bot).blit(self.items[p[3]], (p[0] - 8, p[1] - 8))
        for s in self.shots:
            img = s[6]
            if img is not None:
                (top if s[4] else bot).blit(img, (s[0] - img.get_width() / 2, s[1] - img.get_height() / 2))
        for b in self.ebullets:
            img = b[5]
            dst = top if b[4] else bot
            if img is not None:
                dst.blit(img, (b[0] - 8, b[1] - 8))
            else:
                pygame.draw.circle(dst, (255, 120, 120), (int(b[0]), int(b[1])), 3)
        if (self.invuln // 4) % 2 == 0 and self.phase != 'over':
            if self.mode == 'sky':
                fr = self.manta_fly[(self.t // 6) % len(self.manta_fly)]
                top.blit(fr, (self.mx - 32, self.my - 16))
            else:
                run = self.iriya[(self.t // 6) % 4] if self.iriya else None
                if run is not None:
                    img = run if self.facing > 0 else pygame.transform.flip(run, True, False)
                    bot.blit(img, (self.ix - 32, self.iy - 46))
        for bm in self.booms:
            if self.boom:
                (top if bm[3] else bot).blit(self.boom[min(len(self.boom) - 1, bm[2] // 3)], (bm[0] - 16, bm[1] - 16))
        if self.flash:
            fl = pygame.Surface((SCREEN_W, TOP_H), pygame.SRCALPHA)
            fl.fill((255, 255, 255, self.flash * 6))
            top.blit(fl, (0, 0))
            bot.blit(fl, (0, 0))
        # inactive screen dims a little
        dim = pygame.Surface((SCREEN_W, TOP_H), pygame.SRCALPHA)
        dim.fill((0, 0, 0, 70))
        (bot if self.mode == 'sky' else top).blit(dim, (0, 0))
        # HUD
        pygame.draw.rect(bot, (0, 0, 0), (0, TOP_H - 30, SCREEN_W, 30))
        self.text(bot, f'ＳＣＯＲＥ{self.score:07d}', 4, TOP_H - 28)
        self.text(bot, f'残{max(0, self.lives)}　ボム{self.bombs}', 4, TOP_H - 14, CYAN)
        gauge = pygame.Rect(150, TOP_H - 12, 60, 8)
        pygame.draw.rect(bot, (60, 60, 80), gauge)
        pygame.draw.rect(bot, (90, 220, 120), (gauge.x, gauge.y, int(60 * self.energy / ENERGY_PER_STOCK), 8))
        self.text(bot, '変' + '●' * self.stocks + '○' * (3 - self.stocks), 140, TOP_H - 28, (255, 214, 120))
        if self.items:
            bot.blit(self.items[ITEM_YOKATTA], (214, TOP_H - 28))
            self.text(bot, str(self.yokatta), 232, TOP_H - 28)
        pygame.draw.rect(bot, (200, 60, 60), (SCREEN_W - 40, TOP_H - 30, 36, 26), 1)
        msg = {'clear': 'ＳＴＡＧＥ　ＣＬＥＡＲ', 'over': 'ＧＡＭＥ　ＯＶＥＲ', 'pause': 'ＰＡＵＳＥ',
               'end': 'ＡＬＬ　ＣＬＥＡＲ'}.get(self.phase)
        if self.phase == 'play' and self.t < 90:
            msg = f'ＳＴＡＧＥ　{self.stage}' + ('　ＢＯＳＳ' if self.boss_phase else '')
        if msg:
            self.text(top, msg, SCREEN_W // 2, 80, WHITE, 'center')
            if self.phase == 'clear':
                self.text(top, f'よかったマーク×{self.yokatta}　＋{self.yokatta * 1000}', SCREEN_W // 2, 100,
                          (255, 214, 120), 'center')
            if self.phase in ('over', 'end'):
                self.text(top, f'ＳＣＯＲＥ　{self.score}', SCREEN_W // 2, 100, WHITE, 'center')
        if self.phase == 'pause' and getattr(self, 'pause_menu', None):
            self.pause_menu.draw(bot, self.text)
        self.draw_gap(surf)

    def draw_title(self, top, bot):
        if self.phase == 'help':
            img = self.help[min(self.help_page, len(self.help) - 1)]
            top.fill((0, 0, 0))
            top.blit(img, (0, 0))
            bot.fill((0, 0, 0))
            lines = ['ＰＣの操作', '移動：矢印キー／ＷＡＳＤ', 'Ａ（ショット）：Ｚ／Space（押し続けでタメ）',
                     'Ｂ（ジャンプ）：Ｘ', 'Ｘ（変形）：Ｃ', 'Ｙ（ボム）：Ｖ／右下のボタン',
                     'ポーズ：Ｅｓｃ']
            for i, l in enumerate(lines):
                self.text(bot, l, 10, 14 + 22 * i, (255, 214, 120) if i == 0 else WHITE)
            return
        if self.title_top is not None:
            top.blit(self.title_top, (0, 0))
        if self.title_bot is not None:
            bot.blit(self.title_bot, (0, 0))
        else:
            bot.fill((0, 0, 0))
        if self.phase == 'ranking':
            sh = pygame.Surface((SCREEN_W, TOP_H), pygame.SRCALPHA)
            sh.fill((0, 0, 0, 180))
            bot.blit(sh, (0, 0))
            self.text(bot, 'ＲＡＮＫＩＮＧ', SCREEN_W // 2, 20, CYAN, 'center')
            for i, s in enumerate(self.data.get('ranking', [])):
                self.text(bot, f'{i + 1}．　{s:07d}', 64, 50 + 24 * i)
            return
        for i, s in enumerate(['ＳＴＡＲＴ', 'ＨＥＬＰ', 'ＲＡＮＫＩＮＧ', 'ＥＸＩＴ']):
            c = (255, 214, 120) if i == self.menu_sel else CYAN
            self.text(bot, s, SCREEN_W // 2, 64 + 28 * i, c, 'center')

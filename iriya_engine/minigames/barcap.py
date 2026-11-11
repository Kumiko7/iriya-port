"""BARCAP-S06 (Game I shooting game: the arcade game Iriya plays).

Rebuilt from mini/shoot/* and the game code (mini_iriya.c, ARM9 0x02051c24-0x02056b40).
The DS plays it held sideways: the top screen (Iriya and Asaba, the status
panel) is the left page and the bottom screen (the field) the right page.
Both screens are drawn here in DS hardware orientation, as the code does,
and turned upright for display.

  * The field is drawn with the 3D engine: the sky is a plane of 1-unit cloud
    tiles (sora.nnt) at height -682 that scrolls 0x20 per frame; ships,
    bullets, missiles, explosions and lock marks are flat textured quads on
    the plane y=0. Camera at (0, 1.5, 0.125) looking at (0, 0, -0.75), up
    (-1, 0, 0), 60 deg field of view (0x2051c8c). Stage x / y are world x / z.
  * Stage file: u16 spawn time limit, u16 script count, u16 formation count,
    u32 word offsets; scripts 0..n-1 are enemy programs (u16 type, u16 length,
    commands), the rest formations (u16 delay, u16 count, count x (x, y,
    script)). Missions 1-3 spawn the formations in order, later ones at
    random; each is followed by (2*delay/3 + rand(delay)) * 16 / difficulty
    frames, cut short when the field is empty (0x2052e54).
  * Enemy commands: 0100 speed, 0110 turn rate, 0200 n straight, 0300 x y
    steer to a waypoint, 0400 heading (deg), 0600 spread count interval
    (aimed fan), 0700 n hover, 0800 stop turning, 1000 chase the player,
    11xx/12xx keep flying. Speeds scale with difficulty / 16, difficulty =
    16 * (mission/3 + 3) / 3.
  * Touch enemies to lock on (8 at most, one per enemy); Right (here Up, W or
    right-click) fires a homing missile at each. Kills from one volley score
    100, 110, 120, 130, 150, 170, 200, 240, 300 (0x2061dc0).
  * 10 shield bars; every hit takes one and gives 120 frames of cover; a hit
    with none left ends the game. The 6-place ranking starts at 50000..0;
    a new 1st place gives the 「あとがき」 card.
"""
import math
import random
import struct
import numpy as np
import pygame

from .. import nnt
from . import MiniState

PAGE_W, PAGE_H, GAP = 192, 256, 16
CANVAS = (PAGE_W * 2 + GAP, PAGE_H)
HW_W, HW_H = 256, 192

KILL_SCORES = (100, 110, 120, 130, 150, 170, 200, 240, 300, 300, 300, 300)
HIT_R2 = 116281                 # 341^2: bullets and enemy bodies vs the player
MAX_LOCK = 8
SHIELD_MAX = 10
DEFAULT_RANKING = [50000, 40000, 30000, 20000, 10000, 0]
OLD_DEFAULT = [30000, 25000, 20000, 15000, 10000]
BGM_TITLE, BGM_GAME = 53, 44
SE_LOCK, SE_HIT, SE_FIRE, SE_CARD = 0x1b, 0x1c, 0x1d, 5
CARD_AFTERWORD = 4

# letter.nnt words (tile numbers as the code writes them)
W_SCORE, W_NEWCARD, W_GAMEOVER, W_NEXT, W_MISSION, W_COMPLETE = 5, 7, 0xb, 0xd, 0xf, 0x10f
W_EXIT, W_RANKING, W_HELP, W_START, W_MENU, W_PAUSE, W_QUIT, W_CONTINUE = \
    0x11, 0x13, 0x15, 0x17, 0x19, 0x119, 0x1b, 0x1d
W_DOT, W_CURSOR, W_BLANK = 0x149, 0x1e9, 0x20
W_GAUGE_ON, W_GAUGE_OFF = 0x3f, 0x5f
DIGITS = (0x9, 0x29, 0x49, 0x69, 0x89, 0xa9, 0xc9, 0xe9, 0x109, 0x129)
ONE_CELL = {0x9, 0x29, 0x3f, 0x49, 0x5f, 0x69, 0x89, 0xa9, 0xc9, 0xe9, 0x109, 0x129, 0x149,
            0x169, 0x189, 0x1c9, 0x1e9}
MENU_ITEMS = ((W_START, 'game'), (W_HELP, 'help'), (W_RANKING, 'ranking'), (W_EXIT, 'exit'))

# quads: (half size, ((x, z, s, t) x 4)) from the DS model data
def _quad(half, s0, t0, size=32):
    s1, t1 = s0 + size - 1, t0 + size - 1
    return half, ((half, half, s1, t1), (-half, half, s0, t1), (-half, -half, s0, t0), (half, -half, s1, t0))


PLAYER_Q = {b: _quad(512, s, 128) for b, s in ((-4, 0), (-3, 0), (-2, 32), (-1, 32), (0, 64),
                                                 (1, 96), (2, 96), (3, 128), (4, 128))}
ENEMY_Q = {
    1: [_quad(512, 32 * k, 0) for k in range(5)],
    2: [_quad(512, 32 * k, 32) for k in range(5)],
    3: [_quad(512, s, t) for s, t in ((160, 96), (192, 96), (160, 128), (192, 128), (224, 128))],
    11: [_quad(512, s, 64) for s in (128, 96, 64, 32, 0)],
    12: [_quad(512, s, 96) for s in (128, 96, 64, 32, 0)],
    13: [_quad(512, s, t) for s, t in ((224, 64), (192, 64), (160, 64), (224, 32), (192, 32))],
}
BULLET_Q = (_quad(341, 192, 0), _quad(341, 224, 0))
MISSILE_Q = _quad(341, 160, 32)
TRAIL_Q = _quad(292, 160, 0)
BOOM_HALF = 819


def heading_bin(h):
    """0x2052bd8: sprite for a heading."""
    if h < 0x800:
        return 2
    if h < 0x1800:
        return 1
    if h < 0x8000:
        return 0
    if h < 0xe800:
        return 4
    if h < 0xf800:
        return 3
    return 2


def angle_to(dx, dz):
    """Heading (0..65535) pointing along (dx, dz); 0 is +z."""
    return int(math.atan2(dx, dz) / (2 * math.pi) * 65536) & 0xFFFF


def sin_cos(h):
    r = (h & 0xFFFF) / 65536 * 2 * math.pi
    return math.sin(r), math.cos(r)


class Camera:
    """The DS projection (0x2051c8c) in hardware screen pixels."""
    def __init__(self):
        e = np.array([0, 1.5, 0.125])
        f = np.array([0, 0, -0.75]) - e
        f /= np.linalg.norm(f)
        up = np.array([-1.0, 0, 0])
        s = np.cross(f, up)
        s /= np.linalg.norm(s)
        u = np.cross(s, f)
        self.e, self.f, self.s, self.u = e, f, s, u
        self.t = (0x800 / 4096) / (0xddb / 4096)
        self.asp = 0x1555 / 4096
        self._planes = {}
        self._v = tuple(tuple(float(c) for c in v) for v in (e, s, u, f))

    def project(self, x, z, y=0.0):
        e, s, u, f = self._v
        px, py, pz = x / 4096 - e[0], y / 4096 - e[1], z / 4096 - e[2]
        xc = s[0] * px + s[1] * py + s[2] * pz
        yc = u[0] * px + u[1] * py + u[2] * pz
        w = f[0] * px + f[1] * py + f[2] * pz
        if w <= 1e-6:
            return None
        return (1 + xc / (self.t * self.asp * w)) * 128, (1 - yc / (self.t * w)) * 96

    def plane(self, h):
        """World (x, z) seen at every hardware pixel on the plane at height h."""
        pl = self._planes.get(h)
        if pl is None:
            X, Y = np.meshgrid(np.arange(HW_W) + 0.5, np.arange(HW_H) + 0.5)
            nx = X / 128 - 1
            ny = 1 - Y / 96
            d = (self.f[None, None, :] + self.s[None, None, :] * (nx * self.t * self.asp)[..., None]
                 + self.u[None, None, :] * (ny * self.t)[..., None])
            lam = (h / 4096 - self.e[1]) / d[..., 1]
            px = (self.e[0] + lam * d[..., 0]) * 4096
            pz = (self.e[2] + lam * d[..., 2]) * 4096
            pl = (px.astype(np.float32), pz.astype(np.float32))
            if h in (0, 0.0, -682):
                self._planes[h] = pl
        return pl


class Stage:
    def __init__(self, data):
        self.limit, self.nscript, self.nform = struct.unpack_from('<IHH', data, 0)
        offs = struct.unpack_from('<%dI' % (self.nscript + self.nform), data, 8)
        self.scripts = []
        for i in range(self.nscript):
            o = offs[i] * 2
            typ, ln = struct.unpack_from('<HH', data, o)
            words = struct.unpack_from('<%dh' % ln, data, o + 4)
            self.scripts.append((typ, words))
        self.forms = []
        for i in range(self.nscript, self.nscript + self.nform):
            o = offs[i] * 2
            delay, cnt = struct.unpack_from('<hh', data, o)
            trip = [struct.unpack_from('<hhH', data, o + 4 + 6 * k) for k in range(cnt)]
            self.forms.append((delay, [(x, y if y < 20000 else y - 65536, s) for x, y, s in trip]))


class Enemy:
    __slots__ = ('typ', 'code', 'pc', 'x', 'y', 'h', 'speed', 'base_speed', 'turn', 'steer', 'dist',
                 'wait', 'fire', 'fire_t', 'alive', 'locks', 'lock_t', 'anim')

    def __init__(self, typ, code, x, y):
        self.typ, self.code, self.pc = typ, code, 0
        self.x, self.y, self.h = float(x), float(y), 0
        self.speed = self.base_speed = 0.0
        self.turn = 0
        self.steer = False
        self.dist = 0.0
        self.wait = 0
        self.fire = None
        self.fire_t = 0
        self.alive = True
        self.locks = 0
        self.lock_t = 0
        self.anim = 0


class Missile:
    __slots__ = ('x', 'y', 'h', 'speed', 'target', 'alive', 'trail')

    def __init__(self, x, target):
        self.x, self.y, self.h, self.speed = float(x), 0.0, 0x8000, 200
        self.target = target
        self.alive = True
        self.trail = []


def _tiles4(game, path):
    d = game.rom.read(path)
    hd = nnt.header(d)
    raw = np.frombuffer(d, np.uint8, hd['isize'], hd['ioff'])
    pal = nnt.palette(d, hd['poff'], hd['psize'])
    return nnt._unpack4(raw).reshape(-1, 8, 8), np.asarray(pal, np.uint8)[:, :3]


def _texture(game, path):
    """A linear texture as (index array, palette)."""
    d = game.rom.read(path)
    hd = nnt.header(d)
    raw = np.frombuffer(d, np.uint8, hd['isize'], hd['ioff'])
    pal = np.asarray(nnt.palette(d, hd['poff'], hd['psize']), np.uint8)[:, :3]
    px = nnt._unpack4(raw) if hd['psize'] <= 32 else raw
    return px[:hd['tw'] * hd['th']].reshape(hd['th'], hd['tw']), pal


def _bg(game, path):
    idx, pal, hd = nnt.decode_indexed(game.rom.read(path))
    return np.asarray(pal, np.uint8)[:, :3][idx]


class BarcapState(MiniState):
    def __init__(self, app, back):
        super().__init__(app, back)
        self.canvas = pygame.Surface(CANVAS).convert()
        self.game = g = app.games['iriya1']
        rd = lambda n: g.rom.read(f'mini/shoot/{n}')
        self.stages = [Stage(rd(f'barcap0{i}.dat')) for i in (1, 2, 3)]
        self.cam = Camera()
        self.letters, self.letter_pal = _tiles4(g, 'mini/shoot/letter.nnt')
        self.stat_tiles, self.stat_pal = _tiles4(g, 'mini/shoot/status.nnt')
        self.tex_obj = _texture(g, 'mini/shoot/obj.nnt')
        self.tex_sky = _texture(g, 'mini/shoot/sora.nnt')
        self.tex_green = _texture(g, 'mini/shoot/green.nnt')
        self.tex_eff = _texture(g, 'mini/shoot/eff2.nnt')
        self.bg_title = _bg(g, 'mini/shoot/title.nnt')[:HW_H]
        self.bg_titleL = _bg(g, 'mini/shoot/titleL.nnt')
        self.bg_help = _bg(g, 'mini/shoot/help2.nnt')
        self.data = app.minigame.setdefault('barcap', {})
        rk = self.data.get('ranking')
        if not rk or len(rk) != 6:
            keep = [s for s in (rk or []) if s not in OLD_DEFAULT]
            self.data['ranking'] = sorted(keep + DEFAULT_RANKING, reverse=True)[:6]
        self.map = np.zeros((32, 32), np.int32)          # main BG letters, [row][col]
        self.smap = np.zeros((32, 32), np.int32)         # status panel on the top screen
        self.fade = 0                                    # 0..16 darkness
        self.fade_dir = 0
        self.after_fade = None
        self.t = self.frame = 0
        self.score = 0
        self.mission = 0
        self.shield = SHIELD_MAX
        self.reset_field()
        app.audio.set_game(g)
        self.enter_intro()

    # ------------------------------------------------------------ BG tile maps
    @staticmethod
    def word_len(t):
        if t in ONE_CELL:
            return 1
        return 12 if t == W_NEWCARD else 8

    def put(self, t, ux, uy):
        """0x20563d0: a word at upright tile (ux, uy)."""
        for i in range(self.word_len(t)):
            r = ux + i
            if 0 <= r < 32:
                self.map[r, 31 - uy] = t + 0x20 * i
                self.map[r, 30 - uy] = t + 0x20 * i - 1

    def clear(self, ux, uy, w, h):
        """0x2056368: blank (black) tiles."""
        for c in range(w):
            for k in range(h):
                if 0 <= ux + c < 32 and 0 <= 31 - uy - k < 32:
                    self.map[ux + c, 31 - uy - k] = W_BLANK

    def number(self, v, digits, ux, uy):
        """0x2056540."""
        pos = ux + digits - 1
        n = 0
        while n < digits:
            self.put(DIGITS[v % 10], pos, uy)
            pos -= 1
            n += 1
            v //= 10
            if v == 0:
                break
        while n < digits:
            self.clear(pos, uy, 1, 2)
            pos -= 1
            n += 1

    def status_number(self, v, digits, row0, col):
        """0x205662c: status digits, two tiles wide."""
        row = row0 + digits - 1
        n = 0
        while n < digits:
            d = v % 10
            self.smap[row, col], self.smap[row, col + 1] = d * 16 + 0xe, d * 16 + 0xf
            row -= 1
            n += 1
            v //= 10
            if v == 0:
                break
        while n < digits:
            self.smap[row, col] = self.smap[row, col + 1] = 0x11e
            row -= 1
            n += 1

    # ------------------------------------------------------------ flow
    def go_fade(self, after):
        self.fade_dir = 1
        self.after_fade = after

    def enter_intro(self):
        self.phase = 'intro'
        self.t = 0
        self.map[:] = 0
        self.sel = 0
        self.app.audio.set_game(self.game)
        self.app.audio.play_bgm(BGM_TITLE, 8)

    def enter_menu(self, sel=0):
        self.phase = 'menu'
        self.map[:] = 0
        self.sel = sel

    def start_game(self):
        """State 5."""
        self.score = 0
        self.shield = SHIELD_MAX
        self.mission = -1
        self.next_mission()

    def next_mission(self):
        self.mission += 1
        self.diff = 16 * (self.mission // 3 + 3) // 3
        self.stage = self.stages[self.mission % 3]
        self.reset_field()
        self.map[:] = 0
        self.phase = 'play'
        self.t = 0
        self.fade_dir = -1
        self.app.audio.set_game(self.game)
        self.app.audio.play_bgm(BGM_GAME, 1)

    def reset_field(self):
        self.enemies, self.bullets, self.missiles, self.booms = [], [], [], []
        self.frame = 0
        self.scroll = 0
        self.spawn_t = 0
        self.formed = 0
        self.locks = []
        self.volley = 0
        self.px, self.py, self.pz = 0.0, 0.0, 0.0
        self.bank = 0
        self.invuln = 0

    def se(self, sid):
        self.sfx(sid, self.game)

    # ------------------------------------------------------------ enemies
    def spawn(self):
        """End of 0x2052e54."""
        st = self.stage
        if self.spawn_t > 0:
            if not self.enemies:
                self.spawn_t = 0
            else:
                self.spawn_t -= 1
            return
        if self.frame >= st.limit:
            return
        if self.mission < 3 and self.formed < st.nform:
            idx = self.formed
        else:
            idx = random.randrange(st.nform)
        self.formed += 1
        delay, members = st.forms[idx]
        for x, y, s in members:
            if 0 <= s < len(st.scripts) and len(self.enemies) < 20:
                typ, code = st.scripts[s]
                self.enemies.append(Enemy(typ, code, x, y))
        self.spawn_t = (delay * 2 // 3 + random.randrange(max(1, delay))) * 16 // self.diff

    def run_script(self, e):
        code = e.code
        d = self.diff
        for _ in range(16):
            if e.wait > 0:
                e.wait -= 1
                return
            if e.dist > 0:
                e.dist -= e.speed
                return
            if e.pc >= len(code):
                return
            op = code[e.pc] & 0xFFFF
            a = lambda k: code[e.pc + k] if e.pc + k < len(code) else 0
            if op == 0x0100:
                e.base_speed = a(1) * d // 16
                e.pc += 2
            elif op == 0x0110:
                e.turn = a(1) * d // 16
                e.pc += 2
            elif op == 0x0200:
                e.speed = e.base_speed
                e.dist = a(1)
                e.steer = False
                e.pc += 2
            elif op == 0x0300:
                e.speed = e.base_speed
                tx, ty = a(1), a(2)
                if (tx - e.x) ** 2 + (ty - e.y) ** 2 < e.speed ** 2:
                    e.x, e.y = float(tx), float(ty)
                    e.pc += 3
                    continue
                self.steer_to(e, tx - e.x, ty - e.y)
                return
            elif op == 0x0400:
                e.h = int(a(1) * 65536 / 360) & 0xFFFF
                e.pc += 2
            elif op == 0x0600:
                e.fire = (a(1), a(2), a(3))
                e.fire_t = a(3) + random.randrange(max(1, a(3) // 2))
                e.pc += 4
            elif op == 0x0700:
                e.speed = 0
                e.wait = a(1) * 16 // d
                e.pc += 2
                return
            elif op == 0x0800:
                e.steer = False
                e.speed = 0
                e.pc += 1
            elif op == 0x1000:
                e.speed = e.base_speed
                self.steer_to(e, self.px - e.x, self.pz - e.y)
                if not (-8000 <= e.x <= 8000 and e.y <= 8000):
                    e.alive = False
                return
            elif op in (0x1100, 0x1200):
                if not (-8000 <= e.x <= 8000 and e.y <= 8000):
                    e.alive = False
                e.speed = e.base_speed
                e.steer = False
                return
            else:
                return

    def steer_to(self, e, dx, dy):
        target = angle_to(dx, dy)
        if not e.steer:
            e.h = target
            e.steer = True
            return
        diff = (target - e.h) & 0xFFFF
        if diff <= e.turn or diff >= 0x10000 - e.turn:
            e.h = target
        elif diff < 0x8000:
            e.h = (e.h + e.turn) & 0xFFFF
        else:
            e.h = (e.h - e.turn) & 0xFFFF

    def update_enemy(self, e):
        self.run_script(e)
        if not e.alive:
            return
        if e.typ in (1, 2, 3):
            e.anim += 1
        else:                                   # 0x20532ec: these always face forward
            if 0x1800 < e.h < 0x8000:
                e.h = 0x1800
            elif 0x8000 <= e.h < 0xe800:
                e.h = 0xe800
        if e.speed:
            s, c = sin_cos(e.h)
            e.x += e.speed * s
            e.y += e.speed * c
        if self.phase == 'play' and (e.x - self.px) ** 2 + (e.y - self.pz) ** 2 < HIT_R2:
            self.destroy(e)
            self.boom(e.x, e.y)
            self.hurt(e.x, e.y)
            return
        if e.fire and -5500 <= e.x <= 5500 and -14000 < e.y < 0:
            if e.fire_t > 0:
                e.fire_t -= 1
            else:
                spread, count, interval = e.fire
                if count > 0:
                    span = int(spread * 65536 / 360)
                    h = angle_to(self.px - e.x, self.pz - e.y) - span // 2
                    step = span // count
                    for _ in range(count):
                        self.shoot(e.x, e.y, h)
                        h += step
                e.fire_t = interval + random.randrange(max(1, interval // 2))
        if e.y > 2000:
            self.destroy(e)
        if e.locks > 0:
            e.lock_t = min(30, e.lock_t + 1)

    def shoot(self, x, z, h):
        """0x20543f0: speed difficulty * 50 / 16."""
        if len(self.bullets) >= 64:
            return
        v = self.diff * 50 // 16
        s, c = sin_cos(h)
        self.bullets.append([x, z, v * s, v * c, 0])

    def boom(self, x, z):
        if len(self.booms) < 32:
            self.booms.append([x, z, 0])

    def destroy(self, e):
        """0x2052db0: gone with an explosion, no score."""
        if e.alive:
            e.alive = False
            self.boom(e.x, e.y)
        self.unlock(e)

    def kill(self, e):
        """0x20539e8: shot down by a missile."""
        if not e.alive:
            return
        e.alive = False
        self.boom(e.x, e.y)
        if e.typ == 1:                          # bursts into 7 bullets
            h = angle_to(self.px - e.x, self.pz - e.y) - 0x2000
            for _ in range(7):
                self.shoot(e.x, e.y, h)
                h += 0x924
        self.unlock(e)
        self.score += KILL_SCORES[min(self.volley, len(KILL_SCORES) - 1)]
        self.volley += 1

    def unlock(self, e):
        self.locks = [x for x in self.locks if x is not e]
        for m in self.missiles:
            if m.target is e:
                m.target = None

    def hurt(self, x, z):
        """0x2054ca0."""
        if self.shield < 0 or self.invuln:
            return False
        self.boom(x, z)
        self.se(SE_HIT)
        self.invuln = 120
        self.shield -= 1
        if self.shield < 0 and self.phase == 'play':
            self.phase = 'dying'
            self.t = 0
        return True

    # ------------------------------------------------------------ player
    def lock_at(self, hx, hy):
        """0x2053718: the nearest lockable enemy within sqrt(200) px."""
        if len(self.locks) >= MAX_LOCK:
            return
        best, bd = None, 200
        for e in self.enemies:
            if not e.alive or e.locks >= 1:
                continue
            p = self.cam.project(e.x, e.y)
            if p is None:
                continue
            d = (p[0] - hx) ** 2 + (p[1] - hy) ** 2
            if d < bd:
                best, bd = e, d
        if best is not None:
            best.locks += 1
            best.lock_t = 0
            self.locks.append(best)
            self.se(SE_LOCK)

    def fire(self):
        if not self.locks:
            return
        for e in self.locks:
            if len(self.missiles) < 8:
                self.missiles.append(Missile(self.px, e))
        for e in self.enemies:
            e.locks = 0
        self.locks = []
        self.volley = 0
        self.se(SE_FIRE)

    def update_missiles(self):
        """0x2053ebc."""
        for m in self.missiles:
            if not m.alive:
                if m.trail:
                    m.trail.pop(0)
                continue
            m.trail.append((m.x, m.y))
            if len(m.trail) > 8:
                m.trail.pop(0)
            e = m.target
            if e is None or not e.alive:
                m.speed += 4
            else:
                to = angle_to(e.x - m.x, e.y - m.y)
                d = (to - m.h) & 0xFFFF
                if d < 0x180 or d > 0xfe80:
                    m.h = to
                    m.speed += 4
                elif d < 0xc00:
                    m.h = (m.h + 0x180) & 0xFFFF
                    m.speed += 2
                elif d > 0xf400:
                    m.h = (m.h - 0x180) & 0xFFFF
                    m.speed += 2
                elif d < 0x8000:
                    m.h = (m.h + 0x180) & 0xFFFF
                    m.speed -= 3
                else:
                    m.h = (m.h - 0x180) & 0xFFFF
                    m.speed -= 3
            m.speed = max(10, min(200, m.speed))
            s, c = sin_cos(m.h)
            m.x += m.speed * s
            m.y += m.speed * c
            if e is not None and e.alive and (m.x - e.x) ** 2 + (m.y - e.y) ** 2 < 2 * m.speed ** 2:
                self.se(SE_HIT)
                self.kill(e)
                m.alive = False
            if not (-20000 <= m.y <= 4000 and -10000 <= m.x <= 10000):
                m.alive = False
        self.missiles = [m for m in self.missiles if m.alive or m.trail]

    def update_bullets(self):
        keep = []
        for b in self.bullets:
            b[4] += 1
            b[0] += b[2]
            b[1] += b[3]
            if self.phase == 'play' and (b[1] - self.pz) ** 2 + (b[0] - self.px) ** 2 < HIT_R2:
                if self.hurt(b[0], b[1]):
                    continue
            if -20000 <= b[1] <= 4000 and -10000 <= b[0] <= 10000:
                keep.append(b)
        self.bullets = keep

    def update_field(self, play=True):
        """One frame of the field (0x205286c and the update calls of 0x20522e8)."""
        self.scroll += 0x20
        self.frame += 1
        if self.scroll > 0:
            self.scroll -= 0x1000
        if play:
            self.move_player()
        for e in self.enemies:
            if e.alive:
                self.update_enemy(e)
        self.enemies = [e for e in self.enemies if e.alive]
        self.locks = [e for e in self.locks if e.alive]
        if self.phase in ('play', 'dying', 'result'):
            self.spawn()
        self.update_missiles()
        self.update_bullets()
        for bm in self.booms:
            bm[2] += 1
        self.booms = [b for b in self.booms if b[2] < 16]

    def move_player(self):
        """0x20547f4."""
        keys = pygame.key.get_pressed()
        left = keys[pygame.K_LEFT] or keys[pygame.K_a]
        right = keys[pygame.K_RIGHT] or keys[pygame.K_d]
        if left:
            self.bank = max(-4, self.bank - 1)
            self.px = max(-3000, self.px - 0x30)
        elif right:
            self.bank = min(4, self.bank + 1)
            self.px = min(3000, self.px + 0x30)
        elif self.bank > 0:
            self.bank -= 1
        elif self.bank < 0:
            self.bank += 1
        if self.invuln:
            self.invuln -= 1

    # ------------------------------------------------------------ input
    def hw_point(self, lpos):
        """Canvas position -> (screen, hardware x, y)."""
        x, y = int(lpos[0]), int(lpos[1])          # scaled windows give fractional positions
        if 0 <= y < PAGE_H:
            for scr, x0 in (('top', 0), ('bottom', PAGE_W + GAP)):
                if x0 <= x < x0 + PAGE_W:
                    ux, uy = x - x0, y
                    return scr, HW_W - 1 - uy, ux
        return None

    def event(self, ev):
        if self.fade_dir:
            return
        key = ev.key if ev.type == pygame.KEYDOWN else None
        a_btn = key in (pygame.K_RETURN, pygame.K_SPACE, pygame.K_z, pygame.K_KP_ENTER)
        b_btn = key in (pygame.K_BACKSPACE, pygame.K_x)
        start = key in (pygame.K_ESCAPE, pygame.K_p)
        up = key in (pygame.K_UP, pygame.K_w)
        down = key in (pygame.K_DOWN, pygame.K_s)
        tap = None
        if ev.type == pygame.MOUSEBUTTONDOWN and ev.button == 1:
            tap = self.hw_point(ev.lpos)
        rclick = ev.type == pygame.MOUSEBUTTONDOWN and ev.button == 3
        anything = key is not None or tap is not None
        ph = self.phase

        if ph == 'intro':
            if a_btn or start or tap:
                self.app.ui_sound('enter')
                self.enter_menu()
        elif ph == 'menu':
            if up or down:
                self.sel = (self.sel + (-1 if up else 1)) % 4
                self.app.ui_sound('move')
            pick = None
            if a_btn or start:
                pick = self.sel
            elif b_btn:
                self.app.ui_sound('cancel')
                self.enter_intro()
                return
            elif tap and tap[0] == 'bottom':
                _, hx, hy = tap
                if 0x40 <= hy <= 0x98 and 0x40 <= hx <= 0x80:
                    pick = self.sel = min(3, (0x80 - hx) >> 4)
            if pick is not None:
                act = MENU_ITEMS[pick][1]
                if act == 'exit':
                    self.app.ui_sound('cancel')
                    self.go_fade(self.leave)
                elif act == 'game':
                    self.app.ui_sound('enter')
                    self.go_fade(self.start_game)
                else:
                    self.app.ui_sound('enter')
                    self.phase = act
                    self.map[:] = 0
                    self.t = 0
        elif ph in ('help', 'ranking'):
            if anything:
                self.app.ui_sound('cancel')
                self.enter_menu(1 if ph == 'help' else 2)
        elif ph == 'play':
            if start:
                self.app.ui_sound('enter')
                self.phase = 'pause'
                self.sel = 0
                self.map[:] = 0
            elif up or key == pygame.K_x or rclick:
                self.fire()
            elif tap and tap[0] == 'bottom':
                self.lock_at(tap[1], tap[2])
        elif ph == 'pause':
            if up or down:
                self.sel ^= 1
                self.app.ui_sound('move')
            pick = None
            if a_btn:
                pick = self.sel
            elif start or b_btn:
                pick = 0
            elif tap and tap[0] == 'bottom':
                _, hx, hy = tap
                if 0x38 <= hy <= 0x90 and 0x60 <= hx <= 0x80:
                    pick = min(1, (0x80 - hx) >> 4)
            if pick == 0:
                self.phase = 'play'
                self.map[:] = 0
            elif pick == 1:
                self.app.ui_sound('cancel')
                self.app.audio.stop_all()
                self.go_fade(self.to_title)
        elif ph == 'next':
            if anything:
                self.go_fade(self.next_mission)
        elif ph == 'result':
            if anything:
                self.result_input()

    def to_title(self):
        self.reset_field()
        self.enter_intro()
        self.fade_dir = -1

    def result_input(self):
        """0x2055ed4."""
        t = self.t
        if t < 100:
            if t >= 0x1e:
                self.t = 100
        elif t < 200:
            if t >= 0x82:
                rk = self.data['ranking']
                own = self.app.owned_cards('iriya1')
                if self.score >= rk[0] and CARD_AFTERWORD not in own:
                    self.app.give_card('iriya1', CARD_AFTERWORD)
                    self.se(SE_CARD)
                    self.t = 200
                else:
                    self.go_fade(self.to_title)
        elif t < 300:
            if t >= 0xe6:
                self.go_fade(self.to_title)

    # ------------------------------------------------------------ frame
    def update(self):
        if self.fade_dir > 0:
            self.fade += 2
            if self.fade >= 16:
                self.fade, self.fade_dir = 16, 0
                cb, self.after_fade = self.after_fade, None
                if cb:
                    cb()
                if self.fade_dir == 0 and self.fade:
                    self.fade_dir = -1
            return
        if self.fade_dir < 0:
            self.fade -= 2
            if self.fade <= 0:
                self.fade, self.fade_dir = 0, 0
        self.t += 1
        ph = self.phase
        if ph == 'play':
            self.update_field()
            if self.frame > self.stage.limit and not self.enemies:
                self.phase, self.t = 'complete', 0
        elif ph in ('complete', 'next'):
            self.update_field(play=False)
            self.invuln = 0
            if self.pz > -20000:
                self.pz -= 0x28
            if self.bank > 0:
                self.bank -= 1
            elif self.bank < 0:
                self.bank += 1
            if ph == 'complete' and self.pz < -4800:
                self.phase = 'next'
        elif ph == 'dying':
            self.update_field(play=False)
            if not self.t & 4:
                self.boom(self.px + random.randrange(0x155), self.pz + random.randrange(0x155))
            self.py -= 0x40
            if self.t > 120:
                self.app.audio.stop_all()
                self.pz = 4000
                self.phase, self.t = 'result', 0
                self.record()
        elif ph == 'result':
            self.update_field(play=False)
            if self.t < 100:
                self.t = min(self.t, 0x3c)
            elif self.t < 200:
                self.t = min(self.t, 0xa0)
            elif self.t < 300:
                self.t = min(self.t, 0x104)

    def record(self):
        rk = self.data['ranking']
        for i in range(6):
            if self.score > rk[i]:
                rk.insert(i, self.score)
                del rk[6:]
                break
        self.app.save_progress()

    # ------------------------------------------------------------ drawing: layers
    def draw_letters(self, dst):
        rows, cols = np.nonzero(self.map[:24])
        for r, c in zip(rows, cols):
            t = self.map[r, c]
            tile = self.letters[t] if t < len(self.letters) else None
            if tile is None:
                continue
            blk = dst[r * 8:r * 8 + 8, c * 8:c * 8 + 8]
            if t == W_BLANK:
                blk[:] = 0
                continue
            m = tile != 0
            blk[m] = self.letter_pal[tile[m]]

    def draw_status(self, dst):
        rows, cols = np.nonzero(self.smap[:24])
        for r, c in zip(rows, cols):
            t = self.smap[r, c]
            if t >= len(self.stat_tiles):
                continue
            tile = self.stat_tiles[t]
            blk = dst[r * 8:r * 8 + 8, c * 8:c * 8 + 8]
            m = tile != 0
            blk[m] = self.stat_pal[tile[m]]

    def draw_sky(self, dst):
        """0x2052748: 7 x 7 tiles of sora.nnt, 1 unit each, at height -682. The
        scroll moves 0x20 units a frame, exactly one texel."""
        if not hasattr(self, '_sky'):
            px, pz = self.cam.plane(-682)
            fx = np.mod(px + 2048, 4096) - 2048
            s = np.clip(((0.5 - fx / 4096) * 128).astype(np.int32), 0, 127)
            t = np.floor(64 - pz / 32).astype(np.int64)
            okx = (px > -3.5 * 4096) & (px < 3.5 * 4096)
            self._sky = (s, t, okx, pz)
        s, t, okx, pz = self._sky
        k = self.scroll // 32
        lz = pz - self.scroll
        ok = okx & (lz > -4.5 * 4096) & (lz < 2.5 * 4096)
        idx, pal = self.tex_sky
        i = idx[(t + k) & 127, s]
        m = ok & (i != 0)
        dst[m] = pal[i[m]]

    def quad(self, dst, q, cx, cz, tex, ang=0, scale=1.0, cy=0.0):
        """A flat textured quad on the plane (0x2052124)."""
        half, verts = q
        half *= scale
        r = half * 1.5
        pts = [self.cam.project(cx + dx, cz + dz, cy) for dx in (-r, r) for dz in (-r, r)]
        if any(p is None for p in pts):
            return
        x0 = max(0, int(min(p[0] for p in pts)))
        x1 = min(HW_W, int(max(p[0] for p in pts)) + 1)
        y0 = max(0, int(min(p[1] for p in pts)))
        y1 = min(HW_H, int(max(p[1] for p in pts)) + 1)
        if x0 >= x1 or y0 >= y1:
            return
        px, pz = self.cam.plane(cy)
        lx = px[y0:y1, x0:x1] - cx
        lz = pz[y0:y1, x0:x1] - cz
        if ang:
            sn, cs = math.sin(ang), math.cos(ang)
            lx, lz = lx * cs + lz * sn, -lx * sn + lz * cs
        m = (np.abs(lx) < half) & (np.abs(lz) < half)
        if not m.any():
            return
        (_, _, s1, t1), _, (_, _, s0, t0), _ = verts
        s = (s0 + (lx + half) / (2 * half) * (s1 - s0 + 1)).astype(np.int32)
        t = (t0 + (lz + half) / (2 * half) * (t1 - t0 + 1)).astype(np.int32)
        s = np.clip(s, s0, s1)
        t = np.clip(t, t0, t1)
        idx, pal = tex
        i = idx[t, s]
        m &= i != 0
        blk = dst[y0:y1, x0:x1]
        blk[m] = pal[i[m]]

    def draw_field(self, dst):
        self.draw_sky(dst)
        obj = self.tex_obj
        for m in self.missiles:                                   # 0x2053c04
            for x, z in m.trail:
                self.quad(dst, TRAIL_Q, x, z, obj)
            if m.alive:
                self.quad(dst, MISSILE_Q, m.x, m.y, obj, ang=-(m.h - 0x8000) / 65536 * 2 * math.pi)
        for b in self.bullets:
            self.quad(dst, BULLET_Q[1 if b[4] & 4 else 0], b[0], b[1], obj)
        if self.phase not in ('result',) and (self.invuln & 2) == 0:   # 0x2054710
            self.quad(dst, PLAYER_Q[self.bank], self.px, self.pz, obj, cy=self.py)
        for e in self.enemies:                                    # 0x2052a7c
            if e.typ in (1, 2, 3):
                k = (e.anim // 8) % 5
                for layer in (3, 2, 1):             # equal depth: the first drawn shows
                    if e.typ <= layer:
                        self.quad(dst, ENEMY_Q[layer][k], e.x, e.y, obj)
            elif e.typ in ENEMY_Q:
                self.quad(dst, ENEMY_Q[e.typ][heading_bin(e.h)], e.x, e.y, obj)
        for x, z, f in self.booms:                                # 0x20544cc
            q = _quad(BOOM_HALF, (f % 4) * 32, (f // 4) * 32)
            self.quad(dst, q, x, z, self.tex_eff)
        idx, pal = self.tex_green                                 # 0x205672c lock marks
        k = self.frame & 31
        if k >= 16:
            k = 31 - k
        pal = pal.copy()
        pal[2] = (min(255, k * 16), 255, min(255, k * 16))
        for e in self.enemies:
            if e.locks > 0:
                t = e.lock_t
                half = (60 - t) * 4096 / 160
                q = (half, _quad(half, 32, 0)[1])
                self.quad(dst, q, e.x, e.y, (idx, pal), ang=t * math.pi / 2 / 30)

    def build_texts(self):
        """The letter map for the current screen."""
        ph = self.phase
        if ph == 'menu':                                          # 0x20552e0
            self.clear(8, 0xe, 10, 10)
            self.put(W_MENU, 0xa, 0xe)
            for i, (w, _) in enumerate(MENU_ITEMS):
                self.put(w if self.sel == i else w + 0x100, 9, 0x10 + 2 * i)
            self.put(W_CURSOR, 8, 0x10 + 2 * self.sel)
        elif ph in ('ranking',) or (ph == 'result' and 100 <= self.t < 200):
            self.clear(7, 0xc, 0xb, 0xe)                          # 0x205584c
            self.put(W_RANKING, 9, 0xc)
            for i, sc in enumerate(self.data['ranking']):
                self.number(i + 1, 2, 7, 0xe + 2 * i)
                self.put(W_DOT, 9, 0xe + 2 * i)
                self.number(sc, 7, 0xa, 0xe + 2 * i)
        elif ph == 'pause':                                       # 0x2055a2c
            self.clear(7, 0xe, 0xa, 6)
            self.put(W_PAUSE, 9, 0xe)
            self.put(W_CONTINUE if self.sel == 0 else W_CONTINUE + 0x100, 8, 0x10)
            self.put(W_QUIT if self.sel == 1 else W_QUIT + 0x100, 8, 0x12)
            self.put(W_CURSOR, 7, 0x10 + 2 * self.sel)
        elif ph in ('complete', 'next'):                          # 0x2055dbc
            self.clear(3, 0xe, 0x11, 2 if ph == 'complete' else 4)
            self.put(W_MISSION, 4, 0xe)
            self.put(W_COMPLETE, 0xb, 0xe)
            if ph == 'next':
                self.put(W_NEXT, 6, 0x10)
                self.put(W_MISSION, 0xb, 0x10)
        elif ph == 'result':
            if self.t < 100:
                self.clear(5, 0xe, 0xe, 5)
                self.put(W_GAMEOVER, 8, 0xe)
                self.put(W_SCORE, 6, 0x11)
                self.number(self.score, 7, 0xb, 0x11)
            elif self.t >= 200:
                self.clear(6, 0x12, 0xd, 4)
                self.put(W_NEWCARD, 7, 0x13)
        if ph in ('play', 'pause', 'complete', 'next', 'dying', 'result'):
            n = len(self.locks)                                   # 0x2054710 gauge
            for i in range(8):
                self.put(W_GAUGE_ON if i < n else W_GAUGE_OFF, 0x17 - i, 0x1e)

    def build_status(self):
        """0x205672c: the status panel on the top screen."""
        sm = self.smap
        sm[:] = 0
        for c in range(8):
            for k in range(24):
                sm[k, c] = 6 + c + 16 * k
        m = max(0, self.mission)
        self.status_number(m // 3 + 1, 2, 0xe, 5)
        sm[16, 5], sm[16, 6] = 0xde, 0xdf
        self.status_number(m % 3 + 1, 1, 0x11, 5)
        self.status_number(self.score, 8, 0xa, 3)
        for i in range(10):
            row = 0x11 - i
            sm[row, 1], sm[row, 2] = (0x10e, 0x10f) if i < self.shield else (0x11e, 0x11e)

    # ------------------------------------------------------------ drawing
    def draw(self, surf):
        top = self.bg_titleL.copy()
        ph = self.phase
        if ph in ('intro', 'menu', 'ranking'):
            bot = self.bg_title.copy()
            if ph == 'intro':                                     # sweeping scan line
                x = (self.t % 512)
                if x < HW_W:
                    col = (90, 222, 115)
                    bot[:, x] = col
                    for y in range(0, HW_H, 8):
                        j = (y // 8 + self.t) % 3
                        bot[y:y + 2, max(0, x - j):x] = col
        elif ph == 'help':
            bot = self.bg_help.copy()
        else:
            bot = np.zeros((HW_H, HW_W, 3), np.uint8)
            self.draw_field(bot)
            key = (self.mission, self.score, self.shield)
            if getattr(self, '_top_key', None) != key:
                self._top_key = key
                self._top = self.bg_titleL.copy()
                self.build_status()
                self.draw_status(self._top)
            top = self._top.copy()
        self.map[:] = 0
        self.build_texts()
        self.draw_letters(bot)
        if self.fade:
            k = 1 - self.fade / 16
            top = (top * k).astype(np.uint8)
            bot = (bot * k).astype(np.uint8)
        surf.fill((0, 0, 0))
        for arr, x0 in ((top, 0), (bot, PAGE_W + GAP)):
            s = pygame.surfarray.make_surface(np.ascontiguousarray(arr.transpose(1, 0, 2)))
            surf.blit(pygame.transform.rotate(s, 90), (x0, 0))
        pygame.draw.rect(surf, (18, 18, 22), (PAGE_W, 0, GAP, PAGE_H))

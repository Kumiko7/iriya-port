"""BARCAP-S06 (Game I shooting game: the arcade game Iriya plays).

Rebuilt from mini/shoot/barcap0N.dat and the game code (mini_iriya.c):
  * Stage file: u16 spawn time limit (5000 frames), u16 script count (25),
    u16 formation count, u32 word offsets. Entries 0..24 are enemy scripts
    (u16 type, u16 length, commands); the rest are formations (u16 delay,
    u16 count, count x (x, y, script)). The first formations play in order,
    later ones at random, each followed by delay*2/3 + rand(delay) frames.
  * Enemy script commands (0x2052e54): 0100 speed, 0110 turn rate,
    0200 n straight, 0300 x y steer to a waypoint, 0400 heading (deg),
    0600 spread count interval (aimed fan of bullets), 0700 n hover,
    0800 stop turning, 1000 chase the player, 11xx/12xx keep flying.
    Speeds scale with difficulty (x diff / 16). Heading: 0x10000 = 360 deg,
    x += v sin(h), y += v cos(h); enemies fire only inside |x| < 5500,
    -14000 < y < 0, and leave the field past y > 2000.
  * Lock on to up to 8 enemies by touching them; Up fires homing missiles.
    Kills in one volley score 100, 110, 120, 130, 150, 170, 200, 240, 300...
    (table at 0x2061dc0). A type-1 enemy bursts into 7 bullets when shot.
  * Touching an enemy or bullet costs shield; at 0 shield a hit is fatal.
"""
import math
import random
import struct
import numpy as np
import pygame

from .. import nnt
from ..assets import _surface
from . import MiniState, PauseMenu, SCREEN_W, TOP_H, BOTTOM_Y, GAP

KILL_SCORES = (100, 110, 120, 130, 150, 170, 200, 240, 300)
HIT_R2 = 116281                 # 341^2, enemy body vs player
BULLET_R2 = 300 * 300
MAX_LOCK = 8
SHIELD_MAX = 5
UNIT = 62.5                     # world units per screen pixel
WORLD_TOP = -21000              # world y at the top of the top screen
PLAYER_Y = 0
GREEN = (60, 255, 90)
DARK_GREEN = (20, 120, 40)
SE_MOVE, SE_CANCEL, SE_ENTER = 0, 1, 2
SE_HIT = (15, 16, 17, 18, 19)
SPRITE_ROW = {1: 3, 2: 0, 3: 1, 11: 2, 12: 3, 13: 1}   # obj.nnt rows (5 bank frames each)
SPRITE_COL0 = {1: 5, 2: 0, 3: 0, 11: 0, 12: 0, 13: 5}
DEFAULT_RANKING = [30000, 25000, 20000, 15000, 10000]


def to_screen(x, y):
    return 128 + x / UNIT, (y - WORLD_TOP) / UNIT + (GAP if (y - WORLD_TOP) / UNIT > TOP_H else 0)


def angle_to(dx, dy):
    """Heading (0..65535) pointing along (dx, dy) with h=0 meaning +y."""
    return int(math.atan2(dx, dy) / (2 * math.pi) * 65536) & 0xFFFF


class Stage:
    def __init__(self, data):
        self.limit, self.nscript, self.nform = struct.unpack_from('<IHH', data, 0)
        offs = struct.unpack_from('<%dI' % (self.nscript + self.nform), data, 8)
        self.scripts = []
        for i in range(self.nscript):
            o = offs[i] * 2
            typ, ln = struct.unpack_from('<HH', data, o)
            words = struct.unpack_from('<%dh' % max(0, ln - 2), data, o + 4)
            self.scripts.append((typ, words))
        self.forms = []
        for i in range(self.nscript, self.nscript + self.nform):
            o = offs[i] * 2
            delay, cnt = struct.unpack_from('<hh', data, o)
            trip = [struct.unpack_from('<hhH', data, o + 4 + 6 * k) for k in range(cnt)]
            self.forms.append((delay, [(x, y if y < 20000 else y - 65536, s) for x, y, s in trip]))


class Enemy:
    __slots__ = ('typ', 'code', 'pc', 'x', 'y', 'h', 'speed', 'base_speed', 'turn', 'steer', 'dist',
                 'wait', 'fire', 'fire_t', 'bank', 'alive', 'locked', 'chase')

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
        self.bank = 2
        self.alive = True
        self.locked = False
        self.chase = False


class BarcapState(MiniState):
    def __init__(self, app, back):
        super().__init__(app, back)
        self.game = g = app.games['iriya1']
        rd = lambda n: g.rom.read(f'mini/shoot/{n}')
        self.stages = [Stage(rd(f'barcap0{i}.dat')) for i in (1, 2, 3)]
        obj = self._linear('mini/shoot/obj.nnt')   # stored as a plain bitmap, not OBJ tiles
        self.cell = lambda c, r: obj.subsurface((c * 32, r * 32, 32, 32))
        self.manta = [self.cell(c, 4) for c in range(5)]
        self.missile = self.cell(5, 1)
        self.bullet = self.cell(6, 0)
        eff = g.image('mini/shoot/eff2.nnt')
        self.boom = [eff.subsurface(((k % 4) * 32, (k // 4) * 32, 32, 32)) for k in range(16)]
        green = g.image('mini/shoot/green.nnt')
        self.reticle = green.subsurface((32, 0, 32, 32))
        self.sky = g.image('mini/shoot/sora.nnt')
        art = g.image('mini/shoot/titleL.nnt', rotate=True)
        self.title_art = pygame.transform.smoothscale(art, (144, 192))
        self.logo = g.image('mini/shoot/title.nnt', rotate=True)
        self.status = g.image('mini/shoot/status.nnt', rotate=True)
        self._rot = {}
        self.data = app.minigame.setdefault('barcap', {'ranking': list(DEFAULT_RANKING)})
        self.phase = 'title'
        self.menu_sel = 0
        self.t = 0
        app.audio.set_game(g)
        app.audio.play_bgm(50, 30)      # BGM_SENNYU

    def _linear(self, path):
        d = self.game.rom.read(path)
        h = nnt.header(d)
        pal = nnt.palette(d, h['poff'], h['psize'])
        idx = np.frombuffer(d, np.uint8, h['isize'], h['ioff']).reshape(h['th'], h['tw'])
        rgba = np.dstack([pal[idx], np.where(idx == 0, 0, 255).astype(np.uint8)])
        return _surface(rgba)

    # ------------------------------------------------------------ setup
    def start(self):
        self.mission = 0
        self.loop = 0
        self.score = 0
        self.shield = SHIELD_MAX
        self.px = 0.0
        self.begin_mission()

    def begin_mission(self):
        self.stage = self.stages[self.mission % 3]
        self.diff = 16 + 4 * (self.mission // 3)
        self.enemies, self.bullets, self.missiles, self.booms = [], [], [], []
        self.frame = 0
        self.spawn_t = 60
        self.formed = 0
        self.locks = []
        self.volley_kills = 0
        self.final = False
        self.phase = 'intro'
        self.t = 0
        self.invuln = 0

    # ------------------------------------------------------------ enemies
    def spawn_formation(self):
        st = self.stage
        if self.formed < st.nform:
            idx = self.formed
        else:
            idx = random.randrange(st.nform)
        self.formed += 1
        delay, members = st.forms[idx]
        for x, y, s in members:
            if 0 <= s < len(st.scripts):
                typ, code = st.scripts[s]
                self.enemies.append(Enemy(typ, code, x, y))
        self.spawn_t = int((delay * 2 // 3 + random.randrange(max(1, delay))) * 16 / self.diff)

    def run_script(self, e):
        """Execute commands until one consumes the frame (0x2052e54)."""
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
                e.base_speed = a(1) * d / 16
                e.pc += 2
            elif op == 0x0110:
                e.turn = int(a(1) * d / 16)
                e.pc += 2
            elif op == 0x0200:
                e.speed = e.base_speed
                e.dist = a(1)
                e.steer = False
                e.pc += 2
            elif op == 0x0300:
                e.speed = e.base_speed
                tx, ty = a(1), a(2)
                if (tx - e.x) ** 2 + (ty - e.y) ** 2 < e.speed ** 2 + 1:
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
                e.fire_t = a(3) + random.randrange(max(1, a(3) // 2 + 1))
                e.pc += 4
            elif op == 0x0700:
                e.speed = 0
                e.wait = int(a(1) * 16 / d)
                e.pc += 2
                return
            elif op == 0x0800:
                e.steer = False
                e.speed = 0
                e.pc += 1
            elif op == 0x1000:
                e.speed = e.base_speed
                e.chase = True
                self.steer_to(e, self.px - e.x, PLAYER_Y - e.y)
                return
            else:   # 11xx / 12xx: keep flying straight
                e.speed = e.base_speed
                e.steer = False
                return

    def steer_to(self, e, dx, dy):
        target = angle_to(dx, dy)
        if not e.steer:
            e.h = target
            e.steer = True
            e.bank = 2
            return
        diff = (target - e.h) & 0xFFFF
        if diff <= e.turn or diff >= 0x10000 - e.turn:
            e.h = target
            e.bank = 2
        elif diff < 0x8000:
            e.h = (e.h + e.turn) & 0xFFFF
            e.bank = 3 if diff < 0x2000 else 4
        else:
            e.h = (e.h - e.turn) & 0xFFFF
            e.bank = 1 if diff > 0xE000 else 0

    def update_enemy(self, e):
        self.run_script(e)
        if e.speed:
            rad = e.h / 65536 * 2 * math.pi
            e.x += e.speed * math.sin(rad)
            e.y += e.speed * math.cos(rad)
        # firing
        if e.fire and -5500 <= e.x <= 5500 and -14000 < e.y < 0:
            e.fire_t -= 1
            if e.fire_t <= 0:
                spread, count, interval = e.fire
                self.shoot_fan(e.x, e.y, spread, count)
                e.fire_t = interval + random.randrange(max(1, interval // 2 + 1))
        # collisions with the player
        if (e.x - self.px) ** 2 + (e.y - PLAYER_Y) ** 2 < HIT_R2:
            self.hurt()
            self.kill(e, scored=False)
        if e.y > 2000 or abs(e.x) > 9000 or e.y < -40000:
            e.alive = False

    def shoot_fan(self, x, y, spread, count):
        if count <= 0:
            return
        aim = angle_to(self.px - x, PLAYER_Y - y)
        span = int(spread * 65536 / 360)
        step = span // max(1, count - 1) if count > 1 else 0
        h = aim - span // 2 if count > 1 else aim
        v = 50 * self.diff / 16
        for _ in range(count):
            rad = (h & 0xFFFF) / 65536 * 2 * math.pi
            self.bullets.append([x, y, v * math.sin(rad), v * math.cos(rad)])
            h += step

    def kill(self, e, scored=True):
        if not e.alive:
            return
        e.alive = False
        self.booms.append([e.x, e.y, 0])
        self.sfx(random.choice(SE_HIT), self.game)
        if e.typ == 1:                      # death burst of 7 bullets
            aim = angle_to(self.px - e.x, PLAYER_Y - e.y) - 0x2000
            v = 50 * self.diff / 16
            for k in range(7):
                rad = ((aim + k * 0x924) & 0xFFFF) / 65536 * 2 * math.pi
                self.bullets.append([e.x, e.y, v * math.sin(rad), v * math.cos(rad)])
        if scored:
            self.score += KILL_SCORES[min(self.volley_kills, len(KILL_SCORES) - 1)]
            self.volley_kills += 1
        if e in self.locks:
            self.locks.remove(e)

    def hurt(self):
        if self.invuln:
            return
        self.invuln = 60
        self.booms.append([self.px, PLAYER_Y, 0])
        if self.shield <= 0:
            self.game_over()
        else:
            self.shield -= 1
            self.sfx(SE_CANCEL, self.game)

    def fire(self):
        if not self.locks:
            return
        self.volley_kills = 0
        for k, e in enumerate(self.locks):
            ang = (-0x3000 + k * 0x6000 // max(1, len(self.locks) - 1)) if len(self.locks) > 1 else 0
            self.missiles.append({'x': self.px, 'y': PLAYER_Y - 300, 'h': (0x8000 + ang) & 0xFFFF,
                                  'target': e, 'life': 240})
        self.locks = []
        self.sfx(SE_ENTER, self.game)

    def update_missiles(self):
        for m in self.missiles:
            e = m['target']
            m['life'] -= 1
            if e is not None and e.alive:
                target = angle_to(e.x - m['x'], e.y - m['y'])
                diff = (target - m['h']) & 0xFFFF
                turn = 0x900
                if diff <= turn or diff >= 0x10000 - turn:
                    m['h'] = target
                elif diff < 0x8000:
                    m['h'] = (m['h'] + turn) & 0xFFFF
                else:
                    m['h'] = (m['h'] - turn) & 0xFFFF
                if (e.x - m['x']) ** 2 + (e.y - m['y']) ** 2 < 500 ** 2:
                    self.kill(e)
                    m['life'] = 0
            rad = m['h'] / 65536 * 2 * math.pi
            m['x'] += 420 * math.sin(rad)
            m['y'] += 420 * math.cos(rad)
        self.missiles = [m for m in self.missiles if m['life'] > 0 and m['y'] > -30000]

    def game_over(self):
        self.phase = 'over'
        self.t = 0
        self.record_score()

    def record_score(self):
        rk = self.data.setdefault('ranking', list(DEFAULT_RANKING))
        rk.append(self.score)
        rk.sort(reverse=True)
        del rk[5:]
        self.rank = rk.index(self.score) + 1 if self.score in rk else None
        self.new_card = False
        if self.rank == 1 and self.score > 0:
            got = self.app.give_card('iriya1', 4)        # 「あとがき」: 1位 in BARCAP-S06
            got2 = self.app.give_card('iriya1', 45)      # BARCAP-S06 card
            self.new_card = got or got2
        self.app.save_progress()

    # ------------------------------------------------------------ input
    def event(self, ev):
        A = self.A
        key = ev.key if ev.type == pygame.KEYDOWN else None
        confirm = key in (pygame.K_RETURN, pygame.K_SPACE, pygame.K_z)
        click = ev.lpos if ev.type == pygame.MOUSEBUTTONDOWN and ev.button == 1 else None
        if self.phase == 'title':
            items = ['ＳＴＡＲＴ', 'ＨＥＬＰ', 'ＲＡＮＫＩＮＧ', 'ＥＸＩＴ']
            if key in (pygame.K_UP, pygame.K_w):
                self.menu_sel = (self.menu_sel - 1) % len(items)
            elif key in (pygame.K_DOWN, pygame.K_s):
                self.menu_sel = (self.menu_sel + 1) % len(items)
            elif confirm or click:
                if click:
                    p = self.bottom_pos(click)
                    if not p:
                        return
                    i = (p[1] - 60) // 26
                    if not 0 <= i < len(items):
                        return
                    self.menu_sel = i
                self.sfx(SE_ENTER, self.game)
                [self.start, lambda: self._set('help'), lambda: self._set('ranking'), self.leave][self.menu_sel]()
            elif A.is_back(ev):
                self.leave()
            return
        if self.phase in ('help', 'ranking'):
            if confirm or click or A.is_back(ev):
                self.phase = 'title'
            return
        if self.phase in ('over', 'clear') and self.t > 40:
            if confirm or click or A.is_back(ev):
                if self.phase == 'clear':
                    self.mission += 1
                    self.begin_mission()
                else:
                    self.phase = 'title'
            return
        if self.phase in ('play', 'intro'):
            if key in (pygame.K_UP, pygame.K_w, pygame.K_x) or (ev.type == pygame.MOUSEBUTTONDOWN and ev.button == 3):
                self.fire()
            elif key == pygame.K_ESCAPE or key == pygame.K_p:
                if self.phase == 'play':
                    self.phase = 'pause'
                    self.pause_menu = PauseMenu()
            elif click:
                self.lock_at(click)
            return
        if self.phase == 'pause':
            act = self.pause_menu.event(ev, self.bottom_pos)
            if act == 'resume':
                self.phase = 'play'
            elif act == 'title':
                self.sfx(SE_CANCEL, self.game)
                self.phase = 'title'
            elif act == 'exit':
                self.sfx(SE_CANCEL, self.game)
                self.leave()

    def _set(self, ph):
        self.phase = ph

    def lock_at(self, pos):
        if len(self.locks) >= MAX_LOCK:
            return
        best = None
        for e in self.enemies:
            if not e.alive or e in self.locks:
                continue
            sx, sy = to_screen(e.x, e.y)
            d = (sx - pos[0]) ** 2 + (sy - pos[1]) ** 2
            if d < 20 ** 2 and (best is None or d < best[0]):
                best = (d, e)
        if best:
            self.locks.append(best[1])
            self.sfx(SE_MOVE, self.game)

    # ------------------------------------------------------------ frame
    def update(self):
        self.t += 1
        if self.phase == 'intro':
            if self.t > 90:
                self.phase = 'play'
            return
        if self.phase != 'play':
            return
        keys = pygame.key.get_pressed()
        dx = (keys[pygame.K_RIGHT] or keys[pygame.K_d]) - (keys[pygame.K_LEFT] or keys[pygame.K_a])
        self.px = max(-7000.0, min(7000.0, self.px + dx * 140))
        if self.invuln:
            self.invuln -= 1
        self.frame += 1
        st = self.stage
        if self.frame < st.limit:
            self.spawn_t -= 1
            if self.spawn_t <= 0 and len([e for e in self.enemies if e.alive]) < 20:
                self.spawn_formation()
        elif not self.final:
            self.final = True               # the last big wave
            for _ in range(2):
                self.formed = st.nform - 1
                self.spawn_formation()
        for e in self.enemies:
            if e.alive:
                self.update_enemy(e)
        self.enemies = [e for e in self.enemies if e.alive]
        self.locks = [e for e in self.locks if e.alive]
        for b in self.bullets:
            b[0] += b[2]
            b[1] += b[3]
            if (b[0] - self.px) ** 2 + (b[1] - PLAYER_Y) ** 2 < BULLET_R2:
                b[1] = 99999
                self.hurt()
        self.bullets = [b for b in self.bullets if -9000 < b[0] < 9000 and -30000 < b[1] < 4000]
        self.update_missiles()
        for bm in self.booms:
            bm[2] += 1
        self.booms = [b for b in self.booms if b[2] < 32]
        if self.phase == 'play' and self.final and not self.enemies:
            self.phase = 'clear'
            self.t = 0
            self.score += 1000 * (1 + self.mission)
            self.app.audio.play_bgm(57, 0)

    # ------------------------------------------------------------ drawing
    def rotated(self, surf, ang):
        key = (id(surf), int(ang) // 15)
        r = self._rot.get(key)
        if r is None:
            r = pygame.transform.rotate(surf, (int(ang) // 15) * 15)
            self._rot[key] = r
        return r

    def gtext(self, surf, s, x, y, color=GREEN, align='left'):
        g = self.game
        idxs = g.text_glyphs(s)
        w = 12 * len(idxs)
        if align == 'center':
            x -= w // 2
        for i, gi in enumerate(idxs):
            if gi is None or gi == 614:
                continue
            surf.blit(self.A.Scene._tinted(g.glyph(gi, vertical=False), color), (x + 12 * i, y))

    def draw_sky(self, surf):
        off = (self.t * 2) % 128 if self.phase in ('play', 'intro') else 0
        for y in range(-128, TOP_H * 2 + GAP + 128, 128):
            for x in range(0, SCREEN_W, 128):
                surf.blit(self.sky, (x, y + off))

    def draw(self, surf):
        surf.fill((0, 0, 0))
        if self.phase in ('title', 'help', 'ranking'):
            self.draw_title(surf)
            self.draw_gap(surf)
            return
        self.draw_sky(surf)
        for e in self.enemies:
            row = SPRITE_ROW.get(e.typ, 0)
            spr = self.cell(min(7, SPRITE_COL0.get(e.typ, 0) + (e.bank if SPRITE_COL0.get(e.typ, 0) == 0 else 0)), row)
            ang = 180 - e.h * 360 / 65536 if e.typ >= 11 else 0
            img = self.rotated(spr, ang) if ang else spr
            sx, sy = to_screen(e.x, e.y)
            surf.blit(img, (sx - img.get_width() / 2, sy - img.get_height() / 2))
        for b in self.bullets:
            sx, sy = to_screen(b[0], b[1])
            surf.blit(self.bullet, (sx - 16, sy - 16))
        for m in self.missiles:
            img = self.rotated(self.missile, 180 - m['h'] * 360 / 65536)
            sx, sy = to_screen(m['x'], m['y'])
            surf.blit(img, (sx - img.get_width() / 2, sy - img.get_height() / 2))
        if self.phase != 'over' and (self.invuln // 4) % 2 == 0:
            keys = pygame.key.get_pressed()
            bank = 2 + (keys[pygame.K_RIGHT] or keys[pygame.K_d]) - (keys[pygame.K_LEFT] or keys[pygame.K_a])
            sx, sy = to_screen(self.px, PLAYER_Y)
            surf.blit(self.manta[bank], (sx - 16, sy - 16))
        for bm in self.booms:
            sx, sy = to_screen(bm[0], bm[1])
            surf.blit(self.boom[min(15, bm[2] // 2)], (sx - 16, sy - 16))
        for e in self.locks:
            sx, sy = to_screen(e.x, e.y)
            surf.blit(self.reticle, (sx - 16, sy - 16))
        self.draw_gap(surf)
        # status
        top = self.top
        pygame.draw.rect(top, (0, 0, 0), (0, 0, SCREEN_W, 16))
        self.gtext(top, f'ＭＩＳＳＩＯＮ{self.mission + 1}', 2, 2)
        self.gtext(top, f'{self.score:07d}', 254 - 84, 2)
        sh = '■' * self.shield + '□' * (SHIELD_MAX - self.shield)
        bot = self.bottom
        pygame.draw.rect(bot, (0, 0, 0), (0, TOP_H - 16, SCREEN_W, 16))
        self.gtext(bot, 'ＳＨＩＥＬＤ' + sh, 2, TOP_H - 14)
        self.gtext(bot, f'ＬＯＣＫ{len(self.locks)}', 254 - 60, TOP_H - 14)
        msg = {'intro': f'ＭＩＳＳＩＯＮ　{self.mission + 1}', 'over': 'ＧＡＭＥ　ＯＶＥＲ',
               'clear': 'ＭＩＳＳＩＯＮ　ＣＯＭＰＬＥＴＥ', 'pause': 'ＰＡＵＳＥ'}.get(self.phase)
        if msg:
            self.gtext(top, msg, SCREEN_W // 2, 90, align='center')
            if self.phase == 'over':
                self.gtext(top, f'ＳＣＯＲＥ　{self.score}', SCREEN_W // 2, 112, align='center')
                if self.rank:
                    self.gtext(top, f'ＲＡＮＫＩＮＧ　{self.rank}', SCREEN_W // 2, 130, align='center')
                if getattr(self, 'new_card', False):
                    self.gtext(top, 'ＮＥＷ　ＣＡＲＤ　ＧＥＴ！', SCREEN_W // 2, 150, (255, 230, 90), 'center')
            if self.phase == 'clear':
                self.gtext(top, 'ＮＥＸＴ', SCREEN_W // 2, 112, align='center')
        if self.phase == 'pause' and getattr(self, 'pause_menu', None):
            self.pause_menu.draw(self.bottom, lambda sf, t, x, y, c, al: self.gtext(sf, t, x, y, c, al))

    def draw_title(self, surf):
        top, bot = self.top, self.bottom
        top.fill((0, 0, 0))
        top.blit(self.title_art, ((SCREEN_W - 144) // 2, 0))
        bot.fill((0, 0, 0))
        pygame.draw.rect(bot, GREEN, bot.get_rect().inflate(-6, -6), 1)
        if self.phase == 'title':
            lw = self.logo.get_width()
            bot.blit(self.logo, (SCREEN_W // 2 - lw // 2, -60), (0, 0, lw, 110))
            for i, s in enumerate(['ＳＴＡＲＴ', 'ＨＥＬＰ', 'ＲＡＮＫＩＮＧ', 'ＥＸＩＴ']):
                c = (220, 255, 120) if i == self.menu_sel else GREEN
                self.gtext(bot, ('▶' if i == self.menu_sel else '　') + s, 70, 64 + 26 * i, c)
        elif self.phase == 'help':
            lines = ['ブラックマンタを操作して、', '誘導ミサイルで敵機を撃破せよ。', '',
                     '敵をクリック：ロックオン（最大８）', '↑／Ｗ／右クリック：ミサイル発射',
                     '←→／ＡＤ：自機の移動', '一度に多く倒すほど高得点。',
                     'シールド０で被弾するとゲームオーバー。']
            for i, l in enumerate(lines):
                self.gtext(bot, l, 10, 12 + 20 * i)
        else:
            self.gtext(bot, 'ＲＡＮＫＩＮＧ', SCREEN_W // 2, 16, align='center')
            for i, s in enumerate(self.data.get('ranking', DEFAULT_RANKING)):
                self.gtext(bot, f'{i + 1}．　{s:07d}', 60, 46 + 24 * i)

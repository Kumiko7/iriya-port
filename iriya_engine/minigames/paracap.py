"""Game II mini-game PARACAP-S07 (the Black Manta / Iriya shooter), run from the game's own code.

Unlike the other mini-games, this one is not reimplemented: the shooter's ARM9 code is booted from the
user's ROM on a CPU emulator (dsarm9) and its screens are drawn from the emulated video memory (dsppu),
so it plays exactly as on the DS. The port answers what the DS's second CPU would: cartridge reads,
save memory, the touch screen and sound (routed to the port's audio channels).

Boot: the ROM's start-up code runs until Game II's main loop first waits for v-blank, then the shooter's
entry 0x203d474 replaces Game II's front end (scene manager 0x20a32d4, frame callback 0x203d4c8).
Each frame: input registers, the v-blank interrupt handler 0x2000edc, then the main loop until its next
v-blank wait 0x203123c. The game's EXIT (scene 7, 0x203d4ec) returns to the port's mini-game list.
"""
import os

import pygame

from . import MiniState, PauseMenu, SCREEN_W, TOP_H
from .. import dsarm9, dsppu
from ..paths import user_dir, write_file

SHOOTER_START = 0x203d474
SHOOTER_EXIT = 0x203d4ec
WAIT_VBLANK = 0x203123c
VBLANK_IRQ = 0x2000edc
# ARM7 stand-ins
PXI_INIT = 0x203a3a0          # handshake with the ARM7
PXI_READY = 0x027ffc00 + 904 + 4      # ARM7's registered PXI tags
CARD_READ = 0x2039e74         # (dma, rom src, dst, len, callback, arg, async)
BACKUP_IDENTIFY = 0x20397d4
BACKUP_TYPE = 0x20397ac
BACKUP_REQUEST = 0x20396c4    # (src, dst, len, callback, [arg, async, type, retry, mode])
CARD_RESULT = 0x20a0720       # -> card common block, first word = result code
TP_REQUEST, TP_RESULT, TP_CALIBRATE = 0x203bff8, 0x203c08c, 0x203c2c8
SND_LOAD_ARCHIVE = 0x201f28c
SND_MAIN = 0x202e46c
STRM_START, STRM_STOP, STRM_VOLUME, STRM_BUSY = 0x202fc10, 0x202fc4c, 0x202fc68, 0x202fcc0
SND_HANDLES = 0x2074aa4       # +4 -> 4 stream handles: 0 BGM, 1-3 effects
BACKUP_SIZE = 0x10000
# DS buttons: A B Select Start Right Left Up Down R L X Y
BTN = {name: 1 << k for k, name in enumerate(('A', 'B', 'SELECT', 'START', 'RIGHT', 'LEFT', 'UP', 'DOWN', 'R', 'L', 'X', 'Y'))}
KEYMAP = {pygame.K_z: 'A', pygame.K_SPACE: 'A', pygame.K_x: 'B', pygame.K_c: 'X', pygame.K_v: 'Y',
          pygame.K_RETURN: 'START', pygame.K_KP_ENTER: 'START', pygame.K_RIGHT: 'RIGHT', pygame.K_d: 'RIGHT',
          pygame.K_LEFT: 'LEFT', pygame.K_a: 'LEFT', pygame.K_UP: 'UP', pygame.K_w: 'UP',
          pygame.K_DOWN: 'DOWN', pygame.K_s: 'DOWN'}


class Shooter:
    """The DS code and its stand-in ARM7. sound(event, ch, ...) and busy(ch) connect the audio."""

    def __init__(self, rom, backup=None, sound=None, busy=None):
        wram, entry = dsarm9.load_image(rom)
        wram[0x3ffc00 + 904 + 4:0x3ffc00 + 904 + 8] = b'\xff\xff\xff\xff'
        m = self.m = dsarm9.Arm9(wram)
        self.rom = rom
        self.backup = bytearray(backup if backup and len(backup) == BACKUP_SIZE else b'\xff' * BACKUP_SIZE)
        self.backup_dirty = False
        self.sound = sound or (lambda *a: None)
        self.busy = busy or (lambda ch: False)
        self.touch = None
        self.exited = False
        self.resume = None
        m.stub(PXI_INIT, lambda m: 0)
        m.stub(CARD_READ, self._card_read)
        m.stub(BACKUP_IDENTIFY, self._backup_identify)
        m.stub(BACKUP_TYPE, lambda m: 0x901)
        m.stub(BACKUP_REQUEST, self._backup_request)
        m.stub(TP_REQUEST, lambda m: 0)
        m.stub(TP_RESULT, self._tp_result)
        m.stub(TP_CALIBRATE, lambda m: m.write(m.arg(0), m.read(m.arg(1), 8)) or 0)
        m.stub(SND_LOAD_ARCHIVE, lambda m: 0)
        m.stub(SND_MAIN, lambda m: 0)
        m.stub(STRM_START, lambda m: self._strm('play', m.arg(3)))
        m.stub(STRM_STOP, lambda m: self._strm('stop', m.arg(1)))
        m.stub(STRM_VOLUME, lambda m: self._strm('volume', m.arg(1), m.arg(2)))
        m.stub(STRM_BUSY, lambda m: 1 if self.busy(self._channel()) else 0)
        m.stub(SHOOTER_EXIT, self._exit)
        m.stub(WAIT_VBLANK, self._wait)
        u = m.u
        u.reg_write(dsarm9.A.UC_ARM_REG_CPSR, 0xd3)
        u.emu_start(entry, dsarm9.RETURN, count=100_000_000)
        if self.resume is None:
            raise RuntimeError('Game II start-up did not reach its main loop')
        m.call(SHOOTER_START)

    # stand-in ARM7 ---------------------------------------------------------
    def _card_read(self, m):
        src, dst, n = m.arg(1), m.arg(2), m.arg(3)
        m.write(dst, self.rom[src:src + n])
        cb, arg = m.arg(4), m.arg(5)
        if cb:                                            # completion callback (tail call)
            m.u.reg_write(dsarm9.A.UC_ARM_REG_R0, arg)
            m.u.reg_write(dsarm9.A.UC_ARM_REG_PC, cb)
            return 'stop'
        return 1

    def _backup_identify(self, m):
        m.w32(m.r32(CARD_RESULT), 0)
        return 1

    def _backup_request(self, m):
        src, dst, n, typ = m.arg(0), m.arg(1), m.arg(2), m.arg(6)
        code = 0
        if typ == 6:                                      # read
            m.write(dst, self.backup[src:src + n])
        elif typ in (7, 8):                               # write / program
            self.backup[dst:dst + n] = m.read(src, n)
            self.backup_dirty = True
        elif typ == 9:                                    # verify
            code = 0 if bytes(self.backup[dst:dst + n]) == m.read(src, n) else 1
        m.w32(m.r32(CARD_RESULT), code)
        return 0 if code else 1

    def _tp_result(self, m):
        t, dst = self.touch, m.arg(0)
        m.w16(dst, t[0] if t else 0)
        m.w16(dst + 2, t[1] if t else 0)
        m.w16(dst + 4, 1 if t else 0)
        m.w16(dst + 6, 0)
        return 0

    def _channel(self):
        return (self.m.arg(0) - self.m.r32(SND_HANDLES + 4)) // 4

    def _strm(self, event, *args):
        self.sound(event, self._channel(), *args)
        return 1 if event == 'play' else 0

    def _exit(self, m):
        self.exited = True
        return 0

    def _wait(self, m):
        u = m.u
        self.resume = u.reg_read(dsarm9.A.UC_ARM_REG_LR)
        u.reg_write(dsarm9.A.UC_ARM_REG_PC, self.resume)
        u.emu_stop()
        return 'stop'

    # frames ----------------------------------------------------------------
    def frame(self, keys=0, touch=None):
        m = self.m
        self.touch = touch
        m.w16(0x4000130, ~keys & 0x3ff)                   # KEYINPUT, active low
        m.w16(0x27fffa8, (~keys & 0xc00) | 0x2000)        # X / Y from the ARM7; lid open
        m.call(VBLANK_IRQ)
        pc, self.resume = self.resume, None
        m.u.emu_start(pc, dsarm9.RETURN, count=50_000_000)
        if self.resume is None:
            raise RuntimeError('the shooter did not finish its frame')

    def screens(self):
        return dsppu.render(self.m)


class ParacapState(MiniState):
    def __init__(self, app, back):
        super().__init__(app, back)
        self.game = app.games['iriya2']
        self.save_path = os.path.join(user_dir(), 'paracap.sav')
        self.error = None
        self.pause = None
        self.vol = [1.0] * 4
        self._click = None
        app.audio.stop_all()
        app.audio.set_game(self.game)
        self.boot()

    def boot(self):
        self.shooter = None
        if not dsarm9.HAVE_UNICORN:
            self.error = 'このミニゲームには unicorn が必要です（pip install unicorn）'
            return
        backup = None
        if self.save_path and os.path.exists(self.save_path):
            with open(self.save_path, 'rb') as f:
                backup = f.read()
        rom = self.game.rom.data
        try:
            self.shooter = Shooter(rom, backup, self.on_sound, self.channel_busy)
        except Exception as ex:                           # unexpected ROM / emulator trouble
            self.error = f'起動できませんでした: {ex}'

    # audio -----------------------------------------------------------------
    def voice(self, ch):
        au = self.app.audio
        if not au.ok or not 0 <= ch < 4:
            return None
        return au.bgm if ch == 0 else au.se[ch - 1]

    def on_sound(self, event, ch, *args):
        v = self.voice(ch)
        if v is None:
            return
        au = self.app.audio
        if event == 'play':
            v.start(au._sound(args[0]), self.vol[ch])
            v.id = args[0]
        elif event == 'stop':
            if args and args[0] > 0 and v.busy():
                v.set_volume(0.0, args[0])
                v.stop_after = True
            else:
                v.stop()
        elif event == 'volume':
            self.vol[ch] = min(127, args[0]) / 127
            v.set_volume(self.vol[ch], args[1] if len(args) > 1 else 0)

    def channel_busy(self, ch):
        v = self.voice(ch)
        return bool(v and v.busy())

    # input -----------------------------------------------------------------
    def buttons(self):
        held = pygame.key.get_pressed()
        keys = 0
        for k, name in KEYMAP.items():
            if held[k]:
                keys |= BTN[name]
        return keys

    def event(self, ev):
        if self.error:
            if ev.type in (pygame.KEYDOWN, pygame.MOUSEBUTTONDOWN):
                self.leave()
            return
        if self.pause:
            r = self.pause.event(ev, self.bottom_pos)
            if r == 'resume':
                self.pause = None
            elif r == 'title':
                self.pause = None
                self.app.audio.stop_all()
                self.boot()
            elif r == 'exit':
                self.close()
            return
        if ev.type == pygame.KEYDOWN and ev.key in (pygame.K_ESCAPE, pygame.K_p):
            self.pause = PauseMenu()
        elif ev.type == pygame.MOUSEBUTTONDOWN and ev.button == 1:
            self._click = self.bottom_pos(ev.lpos)
        elif ev.type == pygame.MOUSEBUTTONUP and ev.button == 1:
            self._click = None
        elif ev.type == pygame.MOUSEMOTION and self._click is not None:
            self._click = self.bottom_pos(ev.lpos) or self._click

    def update(self):
        s = self.shooter
        if self.error or s is None or self.pause:
            return
        try:
            s.frame(self.buttons(), self._click)
        except Exception as ex:                           # never take the port down with it
            self.error = f'停止しました: {ex}'
            self.app.audio.stop_all()
            return
        if s.backup_dirty and self.save_path:
            s.backup_dirty = not write_file(self.save_path, s.backup)
        if s.exited:
            self.close()

    def close(self):
        s = self.shooter
        if s is not None and s.backup_dirty and self.save_path:
            write_file(self.save_path, s.backup)
        self.leave()

    def draw(self, surf):
        c = self.canvas
        c.fill((0, 0, 0))
        if self.error:
            msg = self.error                              # 12 px glyphs: 20 to a line
            for k in range(0, len(msg), 20):
                self.app.text(self.bottom, msg[k:k + 20], 8, 40 + 16 * (k // 20), (255, 255, 255))
        elif self.shooter is not None:
            top, bot = self.shooter.screens()
            self.top.blit(pygame.image.frombuffer(top.tobytes(), (SCREEN_W, TOP_H), 'RGB'), (0, 0))
            self.bottom.blit(pygame.image.frombuffer(bot.tobytes(), (SCREEN_W, TOP_H), 'RGB'), (0, 0))
            if self.pause:
                self.pause.draw(self.bottom, self.app.text)
        self.draw_gap(c)
        if surf is not c:
            surf.blit(c, (0, 0))

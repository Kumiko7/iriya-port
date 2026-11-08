"""Interpreter for the .nsb chapter scripts.

Instruction stream: 16-bit words. A word whose high byte is < 0x40 is a font
glyph. Otherwise low byte = instruction length in words (incl. header), high
byte = opcode. Jump targets are word offsets from the start of the code.

Up to 3 threads run concurrently (thread 0 is the main story thread); each has
a 3-deep call stack. Arguments of the form 0x8xxx read a script variable.
"""
import struct
import logging

log = logging.getLogger('vm')

WAIT_NONE, WAIT_FRAMES, WAIT_CLICK, WAIT_THREAD, DONE = range(5)


class Instr:
    __slots__ = ('off', 'op', 'args', 'glyph')

    def __init__(self, off, op, args, glyph=None):
        self.off, self.op, self.args, self.glyph = off, op, args, glyph


class Program:
    def __init__(self, data):
        hs, self.nlines_hint, nentries = struct.unpack_from('<IHH', data, 0)
        self.line_table = struct.unpack_from('<%dI' % nentries, data, 8)
        self.code = data[hs:]
        self.instrs = []
        self.index = {}
        d = self.code
        p = 0
        while p + 1 < len(d):
            sz, op = d[p], d[p + 1]
            if op < 0x40:
                self.index[p] = len(self.instrs)
                self.instrs.append(Instr(p, 'G', (), sz | (op << 8)))
                p += 2
                continue
            if sz == 0:
                p += 2
                continue
            args = struct.unpack_from('<%dH' % (sz - 1), d, p + 2) if sz > 1 else ()
            self.index[p] = len(self.instrs)
            self.instrs.append(Instr(p, op, args))
            p += 2 * sz
        # card table after the 0xfdfdfdfd marker: u16 count, u16 first card,
        # then (line, page, next card) triples -> cards awarded at those lines
        self.card_lines = {}
        mark = data.find(b'\xfd\xfd\xfd\xfd', 8, hs)
        if mark > 0 and hs - mark >= 8:
            words = struct.unpack_from('<%dH' % ((hs - mark - 4) // 2), data, mark + 4)
            cnt, card = words[0], words[1]
            for k in range(cnt):
                if 2 + 3 * k + 2 >= len(words):
                    break
                line, _page, nxt = words[2 + 3 * k:5 + 3 * k]
                self.card_lines.setdefault(line, []).append(card)
                card = nxt
        self.keyword_cards = set(i.args[1] for i in self.instrs if i.op == 0x54 and len(i.args) > 1)
        self.max_page = max([i.args[1] for i in self.instrs if i.op == 0x4f] or [0])
        # map text line ids -> instruction index (for bookmarks / loading)
        self.line_index = {}
        for i, ins in enumerate(self.instrs):
            if ins.op == 0x4f:
                self.line_index.setdefault(ins.args[0], i)

    def target(self, word_off):
        return self.index.get(word_off * 2)

    @staticmethod
    def string_arg(args, start):
        raw = struct.pack('<%dH' % len(args), *args)[start * 2:]
        return raw.split(b'\0')[0].decode('ascii', 'replace')


class Thread:
    def __init__(self, pc, start=None):
        self.pc = pc
        self.start = start
        self.stack = []
        self.wait = WAIT_NONE
        self.timer = 0
        self.wait_arg = None


def s16(v):
    return v - 0x10000 if v >= 0x8000 else v


class VM:
    """Runs a Program against a Scene (see scene.py) which implements the effects."""

    def __init__(self, program, scene):
        self.prog = program
        self.scene = scene
        self.threads = [Thread(0, 0), None, None]
        self.vars = {}
        self.last_var = 0
        self.finished = False
        self.unknown = {}
        self.seek_line = None       # stop fast-forward when this text line starts
        self.seek_done = False
        self.stop_at_clicks = False  # skip mode: fast, but still stop at each message

    # ------------------------------------------------------------- helpers
    def val(self, v):
        if v & 0xF000 == 0x8000:
            return self.vars.get(v & 0x0FFF, 0)
        if v & 0xF000 == 0x9000:
            return self.vars.get(0x1000 | (v & 0x0FFF), 0)
        return s16(v)

    def frames(self, v):
        return max(0, self.val(v))

    def swap_pending(self):
        """True while a helper thread (a background crossfade) is still on its way
        to a 7d layer swap."""
        instrs = self.prog.instrs
        for t in self.threads[1:]:
            if t is None or t.wait == DONE or t.pc is None:
                continue
            for ins in instrs[t.pc:t.pc + 8]:
                if ins.op == 0x7d:
                    return True
                if ins.op in (0xb7, 0xb0, 0xff):
                    break
        return False

    @property
    def main(self):
        return self.threads[0]

    def click(self):
        """Release the main thread from a click wait (running any page transition).

        Returns the wait kind released, or None.
        """
        t = self.main
        if t and t.wait == WAIT_FRAMES and t.wait_arg == 0x59:
            t.timer = min(t.timer, 1)
            return None
        if t and t.wait == WAIT_CLICK:
            kind = t.wait_arg
            frames = self.scene.after_click(kind)
            if frames:
                t.wait, t.timer = WAIT_FRAMES, frames
            else:
                t.wait = WAIT_NONE
            return kind
        return None

    def waiting_click(self):
        t = self.main
        return t is not None and t.wait == WAIT_CLICK

    # ---------------------------------------------------------------- step
    def step_frame(self, fast=False):
        """Advance one frame. fast=True collapses timed waits (skip / load)."""
        for ti in range(3):
            t = self.threads[ti]
            if t is None or t.wait == DONE:
                continue
            if t.wait == WAIT_FRAMES:
                t.timer -= 1
                if fast or t.timer <= 0:
                    t.wait = WAIT_NONE
                    t.wait_arg = None
                else:
                    continue
            elif t.wait == WAIT_THREAD:
                if any(o is not None and o.wait != DONE and o.start == t.wait_arg for o in self.threads if o is not t):
                    continue
                t.wait = WAIT_NONE
            elif t.wait == WAIT_CLICK:
                continue
            self._run(ti, t, fast)
            if fast and ti > 0:
                # fast-forward: helper threads (e.g. the background crossfade
                # spawned with b4 kind 3) also get through their timed waits in
                # this frame, so they finish before the main thread, which runs
                # ahead, spawns the next one; otherwise two crossfades overlap
                # and the second swaps an empty layer in (black screen on load)
                k = 0
                while t.wait == WAIT_FRAMES and k < 64:
                    t.wait, t.wait_arg = WAIT_NONE, None
                    self._run(ti, t, fast)
                    k += 1
        if self.threads[0] is None or self.threads[0].wait == DONE:
            self.finished = True

    def _run(self, ti, t, fast, budget=20000):
        instrs = self.prog.instrs
        sc = self.scene
        while budget > 0 and t.wait == WAIT_NONE:
            budget -= 1
            if t.pc is None or t.pc >= len(instrs):
                t.wait = DONE
                return
            ins = instrs[t.pc]
            t.pc += 1
            op, a = ins.op, ins.args
            if op == 'G':
                if ti == 0:
                    d = sc.add_glyph(ins.glyph)
                    if d and not fast:
                        t.wait, t.timer = WAIT_FRAMES, d
                continue
            # ---------------------------------------------- flow control
            if op == 0xb0:  # goto
                t.pc = self._jump(a[0] | (a[1] << 16))
            elif op in (0xb4, 0xb5, 0xb6):  # gosub / spawn thread
                raw = (a[0] | (a[1] << 16)) if op == 0xb4 else (a[1] | (a[2] << 16))
                kind, addr = raw >> 24, raw & 0xFFFFFF
                tgt = self.prog.target(addr)
                if kind == 3:
                    for k in range(3):
                        o = self.threads[k]
                        if o is None or o.wait == DONE:
                            self.threads[k] = Thread(tgt, addr)
                            break
                    if fast and ti == 0:
                        return      # let the new thread run (and finish its waits) this frame
                else:
                    if len(t.stack) < 3:
                        t.stack.append(t.pc)
                        t.pc = tgt
            elif op == 0xb7:  # return / end thread
                if t.stack:
                    t.pc = t.stack.pop()
                else:
                    t.wait = DONE
            elif op == 0xff:
                t.wait = DONE
            elif op == 0xbd:  # stop all other threads
                for k, o in enumerate(self.threads):
                    if o is not None and o is not t:
                        o.wait = DONE
            elif op == 0xbf:  # stop thread started at addr
                addr = (a[0] | (a[1] << 16)) & 0xFFFFFF
                for o in self.threads:
                    if o is not None and o.start == addr:
                        o.wait = DONE
            elif op == 0xbe:  # wait for thread started at addr
                addr = (a[0] | (a[1] << 16)) & 0xFFFFFF
                if any(o is not None and o is not t and o.wait != DONE and o.start == addr for o in self.threads):
                    t.wait, t.wait_arg = WAIT_THREAD, addr
            # ---------------------------------------------- variables
            elif op == 0xf2:
                self.last_var = a[0]
                self._set(a[0], self.val(a[1]))
            elif 0xf3 <= op <= 0xf7:
                cur = self.val(self.last_var)
                v = self.val(a[0])
                if op == 0xf3:
                    cur += v
                elif op == 0xf4:
                    cur -= v
                elif op == 0xf5:
                    cur *= v
                elif op == 0xf6:
                    cur = int(cur / v) if v else 0
                else:
                    cur = cur % v if v else 0
                self._set(self.last_var, cur)
            elif 0xf8 <= op <= 0xfd:
                x, y = self.val(a[0]), self.val(a[1])
                ok = {0xf8: x == y, 0xf9: x != y, 0xfa: x < y, 0xfb: x > y, 0xfc: x <= y, 0xfd: x >= y}[op]
                if not ok:
                    t.pc = self._jump(a[2] | (a[3] << 16))
            # ---------------------------------------------- waits
            elif op == 0x58:
                n = self.frames(a[0])
                if n > 0 and not fast:
                    t.wait, t.timer, t.wait_arg = WAIT_FRAMES, n, None
            elif op == 0x59:
                # a timed wait the player can cut short (state 0xd, 0x20129d8): a tap
                # ends it on the next frame
                n = self.frames(a[0])
                if n > 0 and not fast:
                    t.wait, t.timer, t.wait_arg = WAIT_FRAMES, n, 0x59
            # ---------------------------------------------- text
            elif op == 0x4f:
                if ti == 0 and self.seek_line is not None and a[0] == self.seek_line:
                    t.pc -= 1
                    self.seek_line = None
                    self.seek_done = True
                    return
                if ti == 0:
                    w = sc.begin_line(a[0], a[1], a[2])
                    if w and not fast:
                        t.wait, t.timer = WAIT_FRAMES, w
            elif op == 0x40:
                if ti == 0:
                    if sc.scroll_mode:
                        if not fast:
                            t.wait, t.timer = WAIT_FRAMES, sc.scroll_frames
                    else:
                        # DS: 0x4001 goes through the glyph path (0x200df84), which
                        # always puts the thread in state 2 until the text engine is
                        # free again, so a new column costs at least one frame
                        w = max(1, sc.new_column())
                        if not fast:
                            t.wait, t.timer = WAIT_FRAMES, w
            elif op in (0x41, 0x42, 0x48, 0x49, 0x4a):
                if ti == 0:
                    sc.text_wait(op)
                    if fast and not self.stop_at_clicks:
                        sc.after_click(op, fast=True)
                    else:
                        t.wait, t.wait_arg = WAIT_CLICK, op
            elif op == 0x44:
                sc.add_ruby(a[0], a[1:])
            elif op == 0x54:
                sc.keyword(a[0], a[1])
            else:
                try:
                    sc.op(op, a, self, fast)
                except Exception as e:  # never let one bad op kill the story
                    log.warning('op %02x %s failed: %s', op, a, e)

    def _set(self, ref, v):
        v = int(v)
        if ref & 0xF000 == 0x8000:
            self.vars[ref & 0x0FFF] = v
        elif ref & 0xF000 == 0x9000:
            self.vars[0x1000 | (ref & 0x0FFF)] = v

    def _jump(self, raw):
        tgt = self.prog.target(raw & 0xFFFFFF)
        if tgt is None:
            log.warning('bad jump target %x', raw)
        return tgt

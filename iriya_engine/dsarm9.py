"""Run a DS game's own ARM9 code (from the user's ROM) on the unicorn CPU emulator.

Only what a self-contained piece of game code needs is provided: main RAM, ITCM/DTCM, I/O, VRAM, OAM and
palette memory as plain RAM; the hardware divider and square-root unit; immediate and v-blank DMA; and
the BIOS calls (as Python). There is no ARM7: whatever the code would ask of it (cartridge, save
memory, touch screen, sound) is answered by Python stubs registered with Arm9.stub(addr, fn).
Coprocessor instructions (caches, MPU, TCM setup) are patched out of the loaded image.
"""
import math
import struct

try:
    from unicorn import Uc, UC_ARCH_ARM, UC_MODE_ARM, UC_HOOK_CODE, UC_HOOK_MEM_READ, UC_HOOK_MEM_WRITE, \
        UC_HOOK_INTR
    from unicorn import arm_const as A
    HAVE_UNICORN = True
except ImportError:                                  # the shooter needs it; the rest of the port does not
    HAVE_UNICORN = False

RETURN = 0x01000000          # return address that ends Arm9.call()
NOP = 0xe1a00000
REGS = None


def load_image(rom):
    """(memory dict, entry) as the firmware leaves it: arm9.bin at its RAM address, the ROM header at
    0x027ffe00, cp15 instructions removed."""
    off, entry, ram, size = struct.unpack_from('<IIII', rom, 0x20)
    wram = bytearray(0x400000)
    base = ram - 0x2000000
    wram[base:base + size] = rom[off:off + size]
    wram[0x3ffe00:0x3fff70] = rom[:0x170]
    for o in range(base, base + size, 4):
        w = struct.unpack_from('<I', wram, o)[0]
        if (w >> 24) == 0xee and (w >> 8) & 0xf == 15 and (w >> 4) & 1:
            rd = (w >> 12) & 15
            mrc = (w >> 20) & 1
            struct.pack_into('<I', wram, o, 0xe3a00000 | (rd << 12) if mrc and rd != 15 else NOP)
    return wram, entry


class Arm9:
    def __init__(self, wram):
        global REGS
        REGS = [A.UC_ARM_REG_R0 + k for k in range(13)] + [A.UC_ARM_REG_SP, A.UC_ARM_REG_LR, A.UC_ARM_REG_PC]
        u = self.u = Uc(UC_ARCH_ARM, UC_MODE_ARM)
        try:
            u.ctl_set_cpu_model(A.UC_CPU_ARM_926)
        except Exception:
            pass
        for addr, size in ((0x00000000, 0x8000), (RETURN, 0x1000), (0x01ff8000, 0x8000), (0x02000000, 0x400000),
                           (0x027e0000, 0x4000), (0x027ff000, 0x1000), (0x04000000, 0x110000),
                           (0x05000000, 0x1000), (0x06000000, 0x900000), (0x07000000, 0x1000),
                           (0x08000000, 0x2000000)):                 # GBA slot: empty
            u.mem_map(addr, size)
        u.mem_write(0x02000000, bytes(wram))
        u.mem_write(0x027ff000, bytes(wram[0x3ff000:0x400000]))
        self.stubs = {}
        self.sp0 = 0x027e3f00
        u.hook_add(UC_HOOK_MEM_READ, self._math, begin=0x040002a0, end=0x040002bf)
        u.hook_add(UC_HOOK_MEM_WRITE, self._dma_write, begin=0x040000b0, end=0x040000df)
        u.hook_add(UC_HOOK_MEM_READ, self._dma_read, begin=0x040000b0, end=0x040000df)
        u.hook_add(UC_HOOK_INTR, self._swi)

    # ------------------------------------------------------------------ calls
    def stub(self, addr, fn):
        """Replace the function at addr: fn(arm9) -> r0 (or None), then return to lr.
        fn may instead return 'stop' after setting pc itself."""
        if addr not in self.stubs:
            self.u.hook_add(UC_HOOK_CODE, self._stubbed, begin=addr, end=addr)
        self.stubs[addr] = fn

    def _stubbed(self, u, addr, size, data):
        fn = self.stubs.get(addr)
        if fn is None:
            return
        r = fn(self)
        if r == 'stop':
            return
        if r is not None:
            u.reg_write(A.UC_ARM_REG_R0, r & 0xffffffff)
        u.reg_write(A.UC_ARM_REG_PC, u.reg_read(A.UC_ARM_REG_LR))

    def arg(self, k):
        if k < 4:
            return self.u.reg_read(REGS[k])
        return self.r32(self.u.reg_read(A.UC_ARM_REG_SP) + 4 * (k - 4))

    def call(self, addr, *args, limit=50_000_000):
        """Call a function in system mode on a scratch stack; returns r0. Registers are restored."""
        u = self.u
        ctx = u.context_save()
        u.reg_write(A.UC_ARM_REG_CPSR, 0x1f)
        for k, a in enumerate(args[:4]):
            u.reg_write(REGS[k], a & 0xffffffff)
        sp = self.sp0 - 4 * len(args[4:])
        for k, a in enumerate(args[4:]):
            self.w32(sp + 4 * k, a)
        u.reg_write(A.UC_ARM_REG_SP, sp)
        u.reg_write(A.UC_ARM_REG_LR, RETURN)
        u.emu_start(addr, RETURN, count=limit)
        r = u.reg_read(A.UC_ARM_REG_R0)
        u.context_restore(ctx)
        return r

    # ----------------------------------------------------------------- memory
    def r8(self, a):
        return self.u.mem_read(a, 1)[0]

    def r16(self, a):
        return struct.unpack('<H', self.u.mem_read(a, 2))[0]

    def r32(self, a):
        return struct.unpack('<I', self.u.mem_read(a, 4))[0]

    def w16(self, a, v):
        self.u.mem_write(a, struct.pack('<H', v & 0xffff))

    def w32(self, a, v):
        self.u.mem_write(a, struct.pack('<I', v & 0xffffffff))

    def read(self, a, n):
        return bytes(self.u.mem_read(a, n))

    def write(self, a, data):
        self.u.mem_write(a, bytes(data))

    # --------------------------------------------------------------- hardware
    def _math(self, u, access, addr, size, value, data):
        if addr < 0x040002b0:                                       # divider results
            mode = struct.unpack('<H', u.mem_read(0x04000280, 2))[0] & 3
            num, den = struct.unpack('<qq', u.mem_read(0x04000290, 16))
            if mode == 0:
                num = struct.unpack('<i', struct.pack('<I', num & 0xffffffff))[0]
            if mode in (0, 1):
                den = struct.unpack('<i', struct.pack('<I', den & 0xffffffff))[0]
            if den == 0:
                q, r = (-1 if num >= 0 else 1), num
            else:
                q = abs(num) // abs(den)
                if (num < 0) != (den < 0):
                    q = -q
                r = num - q * den
            u.mem_write(0x040002a0, struct.pack('<qq', max(-2 ** 63, min(2 ** 63 - 1, q)), r))
        elif 0x040002b4 <= addr < 0x040002b8:                      # square root
            mode = struct.unpack('<H', u.mem_read(0x040002b0, 2))[0] & 1
            p = struct.unpack('<Q', u.mem_read(0x040002b8, 8))[0]
            u.mem_write(0x040002b4, struct.pack('<I', math.isqrt(p if mode else p & 0xffffffff)))

    def _dma_write(self, u, access, addr, size, value, data):
        k = (addr - 0x040000b0) % 12
        if not ((size == 4 and k == 8 and value >> 31) or (size == 2 and k == 10 and value >> 15)):
            return
        r = addr - k
        src, dst = struct.unpack('<II', u.mem_read(r, 8))
        cnt = value if size == 4 else (value << 16) | struct.unpack('<H', u.mem_read(r + 8, 2))[0]
        if (cnt >> 27) & 7 not in (0, 1):                          # immediate / v-blank: done at once
            return
        n = cnt & 0x1fffff or 0x200000
        w = 4 if cnt >> 26 & 1 else 2
        ds, ss = (cnt >> 21) & 3, (cnt >> 23) & 3
        if ss == 0 and ds in (0, 3):
            u.mem_write(dst, bytes(u.mem_read(src, n * w)))
        elif ss == 2 and ds in (0, 3):
            u.mem_write(dst, bytes(u.mem_read(src, w)) * n)
        else:
            step = {0: w, 1: -w, 2: 0, 3: w}
            for _ in range(n):
                u.mem_write(dst, bytes(u.mem_read(src, w)))
                src += step[ss]
                dst += step[ds]

    def _dma_read(self, u, access, addr, size, value, data):
        r = addr - (addr - 0x040000b0) % 12 + 8
        c = struct.unpack('<I', u.mem_read(r, 4))[0]
        if c >> 31:
            u.mem_write(r, struct.pack('<I', c & 0x7fffffff))

    def _swi(self, u, intno, data):
        """BIOS calls. unicorn reports them with pc after the swi and the mode unchanged."""
        pc = u.reg_read(A.UC_ARM_REG_PC)
        thumb = u.reg_read(A.UC_ARM_REG_CPSR) & 0x20
        n = u.mem_read(pc - 2, 1)[0] if thumb else (struct.unpack('<I', u.mem_read(pc - 4, 4))[0] >> 16) & 0xff
        R = lambda k: u.reg_read(REGS[k])
        W = lambda k, v: u.reg_write(REGS[k], v & 0xffffffff)
        if n in (0x0b, 0x0c):                                      # CpuSet / CpuFastSet
            src, dst, cnt = R(0), R(1), R(2)
            w = 4 if (n == 0x0c or cnt >> 26 & 1) else 2
            words = cnt & 0x1fffff
            if cnt >> 24 & 1:
                u.mem_write(dst, bytes(u.mem_read(src, w)) * words)
            elif words:
                u.mem_write(dst, bytes(u.mem_read(src, words * w)))
        elif n == 0x09:                                            # Div
            a, b = (struct.unpack('<i', struct.pack('<I', R(k)))[0] for k in (0, 1))
            q = int(a / b) if b else 0
            W(0, q)
            W(1, a - q * b)
            W(3, abs(q))
        elif n == 0x0d:                                            # Sqrt
            W(0, math.isqrt(R(0)))
        elif n in (0x11, 0x12):                                    # LZ77UnComp
            src, dst = R(0), R(1)
            size = struct.unpack('<I', u.mem_read(src, 4))[0] >> 8
            data = bytes(u.mem_read(src + 4, min(size * 2 + 64, 0x3f0000)))
            out = bytearray()
            i = 0
            while len(out) < size:
                flags = data[i]
                i += 1
                for b in range(8):
                    if len(out) >= size:
                        break
                    if flags & (0x80 >> b):
                        x = data[i] << 8 | data[i + 1]
                        i += 2
                        disp = (x & 0xfff) + 1
                        for _ in range((x >> 12) + 3):
                            out.append(out[-disp])
                    else:
                        out.append(data[i])
                        i += 1
            u.mem_write(dst, bytes(out[:size]))
        u.reg_write(A.UC_ARM_REG_PC, pc | (1 if thumb else 0))

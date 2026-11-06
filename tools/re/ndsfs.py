"""Minimal Nintendo DS ROM filesystem reader (NitroFS)."""
import struct, sys, os

class NDSRom:
    def __init__(self, path):
        with open(path, 'rb') as f:
            self.data = f.read()
        d = self.data
        self.title = d[0:12].rstrip(b'\0').decode('ascii', 'replace')
        self.code = d[12:16].decode('ascii', 'replace')
        (self.fnt_off, self.fnt_size, self.fat_off, self.fat_size) = struct.unpack_from('<4I', d, 0x40)
        self.files = {}  # path -> (start, end)
        self._walk(0xF000, '')

    def _fat(self, fid):
        return struct.unpack_from('<2I', self.data, self.fat_off + fid * 8)

    def _walk(self, dir_id, prefix):
        d = self.data
        base = self.fnt_off + (dir_id & 0xFFF) * 8
        sub_off, first_id, _ = struct.unpack_from('<IHH', d, base)
        p = self.fnt_off + sub_off
        fid = first_id
        while True:
            t = d[p]; p += 1
            if t == 0:
                break
            n = t & 0x7F
            name = d[p:p+n].decode('shift_jis', 'replace'); p += n
            if t & 0x80:
                sub = struct.unpack_from('<H', d, p)[0]; p += 2
                self._walk(sub, prefix + name + '/')
            else:
                self.files[prefix + name] = self._fat(fid)
                fid += 1

    def read(self, path):
        s, e = self.files[path]
        return self.data[s:e]

if __name__ == '__main__':
    rom = NDSRom(sys.argv[1])
    print(rom.title, rom.code, len(rom.files), 'files')
    if len(sys.argv) > 2:
        out = sys.argv[2]
        for p, (s, e) in rom.files.items():
            fp = os.path.join(out, p)
            os.makedirs(os.path.dirname(fp), exist_ok=True)
            with open(fp, 'wb') as f:
                f.write(rom.data[s:e])
    else:
        for p, (s, e) in sorted(rom.files.items()):
            print(f'{e-s:10d}  {p}')

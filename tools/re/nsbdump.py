import os, sys, struct
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from nsbdis import parse
from iriya_engine.textcodec import Font, ruby_char
game, chap = sys.argv[1], sys.argv[2]
EX = os.path.join(ROOT, 'dev', 'extracted')
font = Font(open(f'{EX}/{game}/com/font.bin', 'rb').read())
d = open(f"{EX}/{game}/{chap}.nsb" if not chap[:1].isalnum() or len(chap)>1 else f"{EX}/{game}/chapter{chap}.nsb", "rb").read()
hs, ins = parse(d)
line = ''
def flush():
    global line
    if line: print('    TEXT', line); line = ''
for off, op, a in ins:
    if op == 'G':
        line += font.char(a[0]); continue
    if op == 0x44:
        line += '《' + ''.join(ruby_char(x) for x in a[1:]) + f'|{a[0]:x}》'; continue
    if op in (0x40, 0x41, 0x42, 0x48):
        line += {0x40: '⏎', 0x41: '⏏', 0x42: '¶', 0x48: '■'}[op]
        if op != 0x48: flush()
        continue
    flush()
    s = ''
    if op in (0x70, 0x80):
        # find ascii string tail
        raw = struct.pack('<%dH' % len(a), *a)
        k = 4 if op == 0x70 else 2
        nums = [hex(x) for x in a[:k]]
        txt = raw[k*2:].split(b'\0')[0].decode('ascii', 'replace')
        s = f'{nums} "{txt}"'
    else:
        s = ' '.join(hex(x) for x in a)
    print(f'{off:06x} {op:02x} {s}' if isinstance(op, int) else f'{off:06x} {op} {s}')
flush()

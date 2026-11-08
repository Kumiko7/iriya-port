"""Glyph-index <-> Unicode mapping for the game's 12x12 font (com/font.bin).

Glyphs 614.. follow Shift-JIS (JIS X 0208 + NEC row 13) order, with six extra
glyphs (long dash / wave variants) after '〓' and one extra after 'ん'.
Glyphs 0..613 are an alternate copy of the non-kanji rows; they are mapped by
matching bitmaps against the main block.
"""
import numpy as np

MAIN_BASE = 614
GLYPH_BYTES = 24


def _sjis_chars():
    chars = []
    for lead in list(range(0x81, 0xA0)) + list(range(0xE0, 0xEB)):
        for t in range(0x40, 0xFD):
            if t == 0x7F:
                continue
            code = lead << 8 | t
            if code > 0xEAA4:
                continue
            try:
                c = bytes([lead, t]).decode('cp932')
            except UnicodeDecodeError:
                continue
            chars.append(c)
    return chars


RUBY_CHARS = (
    '‐ー0123456789ABCD'
    'EFGHIJKLMNOPQRST'
    'UVWXYZabcdefghij'
    'klmnopqrstuvwxyz'
    '※あいうえおかきくけこさしすせそ'
    'たちつてとなにぬねのはひふへほま'
    'みむめもやゆよらりるれろわをんが'
    'ぎぐげござじずぜぞだぢづでどばび'
    'ぶべぼぱぴぷぺぽアイウエオカキク'
    'ケコサシスセソタチツテトナニヌネ'
    'ノハヒフヘホマミムメモヤユヨラリ'
    'ルレロワヲンガギグゲゴザジズゼゾ'
    'ダヂヅデドバビブベボパピプペポ・'
    '＼！＼？'
)


# Symbol slots each game's font redraws as other text: two half-width letters per
# cell (a space is an empty half), or another kanji. Found by checking every glyph
# the scripts use in the box-drawing and NEC row-13 blocks against its bitmap.
REDRAWN = {
    'iriya1': {**dict(zip(range(1113, 1121), ['Ex', 'cu', 'se', ' m', 'e,', ' s', 'ir', '. '])),
               1198: '祥'},
    'iriya2': {**dict(zip(range(1175, 1189), ['"M', 'IA', 'Yo', 'u ', 'ar', 'e ', 'no', 't ',
                                              'fo', 'rg', 'ot', 'te', 'n.', '" '])),
               **dict(zip(range(1204, 1214), ['AM', '10', ':4', '2 ', 'DA', 'MA', 'sh', 'ot',
                                              '5M', 'S ']))},
}


class Font:
    def __init__(self, font_bin, game_id=None):
        self.data = font_bin
        self.count = len(font_bin) // GLYPH_BYTES
        sj = _sjis_chars()
        a = sj.index('〓') + 1
        b = sj.index('ん') + 1
        seq = sj[:a] + ['―', '―', '―', '～', '～', '～'] + sj[a:b] + ['ゔ'] + sj[b:]
        table = {}
        for k, c in enumerate(seq[:self.count - MAIN_BASE]):
            table[MAIN_BASE + k] = c
        # alternate block: match bitmaps
        by_bitmap = {}
        for i in range(MAIN_BASE, self.count):
            by_bitmap.setdefault(self.raw(i), i)
        for i in range(0, MAIN_BASE):
            j = by_bitmap.get(self.raw(i))
            if j is not None and j in table:
                table[i] = table[j]
        table.update(REDRAWN.get(game_id, {}))
        self.to_char = table
        self.from_char = {}
        for i in sorted(table, reverse=True):
            self.from_char[table[i]] = i

    def raw(self, i):
        return self.data[i * GLYPH_BYTES:(i + 1) * GLYPH_BYTES]

    def bitmap(self, i):
        """Upright 12x12 bitmap (rows x cols) as a bool ndarray."""
        g = np.frombuffer(self.raw(i), dtype='<u2')  # one u16 per column
        bits = (g[:, None] >> np.arange(16)[None, :]) & 1  # [col, bit]
        img = bits[:, ::-1].T  # rows top->bottom = bit 15..0
        return img[3:15, :12].astype(bool) if False else img[:13, :12].astype(bool)

    def char(self, i):
        return self.to_char.get(i, '�')


def ruby_char(i, game_id=None):
    """The character of ruby glyph i (com/rubi.bin). Game II's table has one more
    glyph than Game I's, ヴ at 0xca, so its codes from there on are one higher.
    0xd0 / 0xd2 (II: 0xd3) are the slanted 傍点 stroke, kept here as ＼."""
    if game_id == 'iriya2':
        if i == 0xca:
            return 'ヴ'
        if i > 0xca:
            i -= 1
    return RUBY_CHARS[i] if 0 <= i < len(RUBY_CHARS) else '?'

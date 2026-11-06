"""Keyword cards (card/card.bin + card/*.nnt).

card.bin layout (all little endian):
  u16 card_count, u16 category_count, u32 table_offset
  text area from 0x8: category names, then per card: name, description and
  image name. Text is font glyph indices; 0xff01 ends a string, 0x4001 is a
  line break, 0x20XX is the half-width ASCII character XX.
  table_offset: card_count records of 16 bytes:
    u16 name, u16 desc, u16 image (byte offsets into the file), u16 category,
    u16 kind, u16 value, u16 number, u16 0
"""
import struct


# 電撃カード大戦 attributes (kind): suit k beats suit k+1 (mod 3), like
# rock-paper-scissors. Colours follow com/card_r, card_b, card_g.
ATTR_NAMES = ('熱血', '泣き', '萌え')


class Card:
    __slots__ = ('index', 'name', 'desc', 'image', 'category', 'kind', 'power')

    def __init__(self, index, name, desc, image, category, kind=0, power=0):
        self.index, self.name, self.desc, self.image, self.category = index, name, desc, image, category
        self.kind, self.power = kind, power

    @property
    def is_battle(self):
        """Usable in 電撃カード大戦 (has a power value)."""
        return self.power > 0 and self.category not in (12, 13)


def _read_text(words, i, font):
    out = []
    while i < len(words):
        w = words[i]
        i += 1
        if w == 0xff01:
            break
        if w == 0x4001:
            out.append('\n')
        elif w & 0xFF00 == 0x2000:
            c = w & 0xFF
            out.append(chr(c) if c else '')
        elif w >= 614:
            out.append(font.char(w))
    return ''.join(out), i


def _read_ascii(data, off):
    end = data.index(b'\0', off)
    return data[off:end].decode('ascii', 'replace')


def load_cards(data, font):
    count, ncat, table = struct.unpack_from('<HHI', data, 0)
    words = struct.unpack_from('<%dH' % ((table - 8) // 2), data, 8)
    cats = []
    i = 0
    for _ in range(ncat):
        t, i = _read_text(words, i, font)
        cats.append(t)
    cards = []
    for k in range(count):
        n_off, d_off, i_off, cat, kind, power = struct.unpack_from('<6H', data, table + 16 * k)
        name, _ = _read_text(words, (n_off - 8) // 2, font)
        desc, _ = _read_text(words, (d_off - 8) // 2, font)
        image = _read_ascii(data, i_off)
        cards.append(Card(k, name, desc, image, cat, kind, power))
    return cats, cards

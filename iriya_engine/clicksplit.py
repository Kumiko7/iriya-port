"""Extra clicks inside text runs too long to read.

The text scrolls once 8 columns are on the page, and the script types on until
the next click (ops 41/42/48/49/4a). If a run of text between two clicks fills
more than 8 columns, its start has scrolled off before the reader can stop and
read it. The DS has a few such runs, and the 番外編 built from the books have
more (long dialogue paragraphs with no 。 in them). This pass adds a click (op
41) inside each such run, after a punctuation mark, so every run fits on one
page. Runs typed in the staff-roll scroll mode (5a/5b), and runs with no
punctuation to break at, are left alone.
"""
from .vm import Instr

COLS = 8
CLICKS = (0x41, 0x42, 0x48, 0x49, 0x4a)
CLEARS = (0x48, 0x49, 0x4a)
STRONG = set('。！？!?」』）)')
WEAK = set('、，,…―')


def split_long_runs(prog, char_of):
    """Add clicks to prog (a Program) in place; returns how many were added."""
    L = prog.instrs
    runs, run = [], []           # run: [(glyph instr index, column, strength, insert-after index)]
    col = 0
    scroll_mode = False
    for i, x in enumerate(L):
        op = x.op
        if op == 'G':
            if scroll_mode:
                continue
            ch = char_of(x.glyph)
            j = i + 1                                   # after its ruby / keyword ops
            while j < len(L) and L[j].op in (0x44, 0x54):
                j += 1
            run.append((i, col, 2 if ch in STRONG else 1 if ch in WEAK else 0, j))
        elif op == 0x40:
            col += 1
        elif op in CLICKS or op in (0x5a, 0x5b):
            if run:
                runs.append(run)
            run = []
            if op in CLEARS:
                col += COLS + 1                         # the page is wiped
            if op in (0x5a, 0x5b):
                scroll_mode = bool(x.args and x.args[0])
    inserts = set()
    for r in runs:
        _split(r, inserts)
    if not inserts:
        return 0
    for at in sorted(inserts, reverse=True):
        L.insert(at, Instr(None, 0x41, ()))
    from .correct import reindex
    reindex(prog)
    return len(inserts)


def _split(run, inserts):
    """Cut run (a list of glyph entries) until no piece spans more than COLS columns."""
    while run[-1][1] - run[0][1] >= COLS:
        c0, c1 = run[0][1], run[-1][1]
        best, key = None, None
        for k, (i, c, strength, after) in enumerate(run[:-1]):
            if c - c0 >= COLS:
                break
            if not strength or run[k + 1][2] or run[k + 1][1] == c0:   # not before 』、 etc.
                continue
            rest_fits = c1 - run[k + 1][1] < COLS
            kk = (rest_fits, strength, -abs((c - c0) - (c1 - c0) / 2))
            if key is None or kk > key:
                best, key = k, kk
        if best is None:
            # no punctuation to break at: the DS's own unbroken runs (Game II's
            # 「何か恐ろしいことが起こる」 racing past) are an effect; leave them
            return
        inserts.add(run[best][3])
        run = run[best + 1:]

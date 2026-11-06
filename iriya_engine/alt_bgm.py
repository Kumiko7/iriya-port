"""Alternative soundtrack: the BGM of 「ひまわり」 (ぶらんくのーと) in place of the DS
music (オプション → ＢＧＭ → ひまわり).

The tracks are extracted once from your own copy of Himawari with
tools/bgm/extract_himawari.py into a separate data/himawari_bgm/ folder; this module only
plays those files. MAP below is the built-in matching; tools/bgm/bgm_matcher.py
(a page to listen to both soundtracks side by side) saves your own matching to
data/himawari_bgm/mapping.json, which takes its place. An empty list there keeps
the DS track. Each DS track is replaced by the Himawari track used for the
same kind of scene there. Where the DS games lean on one track far more than
Himawari leans on any of its own (Himawari's most used track covers 4.9 % of its
text; BGM_SUIZENJI covers 11 % of Game I's), the DS track gets several
replacements and each scene picks one of them, so the same piece doesn't play
through a fifth of a chapter.
"""
import json
import os
import threading
import zlib

# DS track -> Himawari tracks, matched by mood, instruments and the kind of scene
# each is used for (see himawari_bgm_descriptions.txt / iriya_bgm_descriptions.txt).
MAP = {
    # heavily used tracks get several replacements (one picked per scene)
    'BGM_SUIZENJI':  ['piano2_007', 'free0235', 'free0554'],   # comic ragtime / eccentric swagger / the UFO dig
    'BGM_NITIJO':    ['free35', '102'],            # bright school mornings / lively everyday banter
    'BGM_GENSOU':    ['free0512', 'asanagi'],      # gentle sunlit acoustic reverie
    'BGM_AKIHO':     ['teek', 'piano2_004'],       # the prickly heroine / bubbly teasing
    'BGM_ROUKA':     ['y07', 'free0337'],          # sunlit school days / relaxed pastoral walk
    # everyday and comedy
    'BGM_KENSOU':    ['free0335'],                 # bustling, spirited school life
    'BGM_LEMON':     ['asana-0'],                  # sweet, wholesome warmth
    'BGM_MAYUMI':    ['piano2_015'],               # tipsy adult barging in
    'BGM_CITY':      ['free0557'],                 # downtown streets
    'BGM_BONYOU':    ['hiphop_004'],               # lazy, bored summer afternoons
    'BGM_GAKUSAI':   ['free0551'],                 # the school festival
    'BGM_MAIMU':     ['free0551'],                 # the folk dance (festive accordion)
    'BGM_TYUUKA':    ['loop7'],                    # the eating contest
    'BGM_TYUUKA_B':  ['free0537'],                 # mock martial standoffs
    'BGM_DATURYOKU': ['free0428'],                 # comic letdowns (pizzicato, bassoon)
    'BGM_MOUSOU_RE': ['hiphop_013'],               # cocky swagger
    'BGM_KONWAKU':   ['Robot_Island'],             # quirky bewilderment
    'BGM_KONWAKU_ARR': ['dot_006'],                # casual snooping
    'BGM_SHOUSOU':   ['loop7'],                    # frantic, running late
    'BGM_YUUSEN':    ['free0426'],                 # upbeat, devoted, happy together
    # tender and wistful
    'BGM_YOKAN':     ['oikaze'],                   # 追い風 (the prologue)
    'BGM_IRIYA':     ['free0429'],                 # music-box tenderness (the heroine)
    'BGM_YUMEUTSUTSU': ['free34'],                 # fragile lilting reverie
    'BGM_SEIJYAKU':  ['piano3_001'],               # the two of them alone at night
    'BGM_SINSEKAI':  ['free21'],                   # orchestral, radiant and bittersweet
    'BGM_NITIJO_ARR': ['asana-0'],                 # pastoral walk home
    'BGM_NITIJO_ARR2': ['piano2_020'],             # everyday life with grief underneath
    'BGM_ROUKA_ARR': ['piano3_014'],               # empty corridors at sunset
    'BGM_AKIHO_ARR': ['piano3_016'],               # unrequited, bittersweet
    'BGM_MAYUMI_ARR': ['piano2_014'],              # dropping the cheerful mask
    'BGM_MAYUMI_ARR2': ['piano_011'],              # warm, comforting
    'BGM_KANDOU_ARR': ['omoiwo'],                  # reassurance after tears
    'BGM_KANDOU_SHORT': ['free0221'],              # cinematic emotional climax
    # sorrow
    'BGM_AISETSU':   ['piano3_003'],               # the main sad piano piece
    'BGM_AISETSU_ARR': ['piano_012'],              # fragile stillness, looming fate
    'BGM_OMOTAIKEI': ['modulor_019'],              # crushing dread
    'BGM_OMOTAIKEI_B': ['hakuu'],                  # cold, sinister revelation
    # mystery and suspense
    'BGM_ENBAN':     ['ambient_009'],              # the night sky, cosmic wonder
    'BGM_NAZO':      ['ambient_003'],              # classified secrets
    'BGM_INBOU':     ['minimal_020'],              # conspiracy
    'BGM_FUAN':      ['piano_010'],                # breathless unease
    'BGM_KINPAKU':   ['ambient_001'],              # menace close by
    'BGM_APPAKU':    ['minimal_019'],              # face to face with ruthless authority
    'BGM_APPAKU_ARR': ['free0334'],                # evading surveillance
    'BGM_KEIHOU':    ['ambient2_001'],             # sirens, wartime drone
    'BGM_SENNYU':    ['ea2'],                      # sneaking in
    'BGM_SASUPENSUKEI': ['drumn_001'],             # sudden ambush, chase
    'BGM_KINKYUU':   ['minimal_009'],              # emergency
    'BGM_TOUSOU':    ['techno_006'],               # the escape
    # battle and catastrophe
    'BGM_KESSEN':    ['free65'],                   # the showdowns
    'BGM_IKARIBAKUHATU': ['hakugin'],              # desperate fury
    'BGM_HOUKAIMAE': ['pianodance01'],             # racing the collapse
    'BGM_HOUKAI':    ['free0343'],                 # orchestral catastrophe
}

# RMS loudness of each DS track (dBFS), so a replacement plays at the level of
# the track it stands in for.
DS_LOUDNESS = {
    'BGM_NITIJO': -19.9, 'BGM_IRIYA': -21.8, 'BGM_AKIHO': -13.9, 'BGM_MAYUMI': -14.8, 'BGM_SUIZENJI': -17.5,
    'BGM_YOKAN': -17.4, 'BGM_GENSOU': -17.7, 'BGM_YUMEUTSUTSU': -17.7, 'BGM_SEIJYAKU': -20.1, 'BGM_KEIHOU': -17.7,
    'BGM_ROUKA': -16.8, 'BGM_ENBAN': -15.7, 'BGM_AISETSU': -19.2, 'BGM_TOUSOU': -15.5, 'BGM_GAKUSAI': -16.8,
    'BGM_INBOU': -15.1, 'BGM_KONWAKU': -13.5, 'BGM_APPAKU': -16.4, 'BGM_LEMON': -17.0, 'BGM_SENNYU': -15.4,
    'BGM_KENSOU': -17.2, 'BGM_DATURYOKU': -14.4, 'BGM_SHOUSOU': -18.4, 'BGM_KINPAKU': -17.3, 'BGM_CITY': -15.9,
    'BGM_FUAN': -14.7, 'BGM_KANDOU_SHORT': -14.2, 'BGM_MOUSOU_RE': -14.5, 'BGM_MAIMU': -13.2, 'BGM_SINSEKAI': -20.0,
    'BGM_KESSEN': -12.2, 'BGM_IKARIBAKUHATU': -15.6, 'BGM_NAZO': -12.1, 'BGM_AISETSU_ARR': -18.3,
    'BGM_AKIHO_ARR': -20.5, 'BGM_APPAKU_ARR': -17.3, 'BGM_BONYOU': -14.2, 'BGM_HOUKAI': -10.6,
    'BGM_HOUKAIMAE': -10.0, 'BGM_KANDOU_ARR': -22.0, 'BGM_KINKYUU': -16.0, 'BGM_KONWAKU_ARR': -14.1,
    'BGM_NITIJO_ARR': -15.3, 'BGM_MAYUMI_ARR': -15.6, 'BGM_OMOTAIKEI': -14.1, 'BGM_ROUKA_ARR': -15.9,
    'BGM_SASUPENSUKEI': -13.8, 'BGM_TYUUKA': -14.4, 'BGM_YUUSEN': -11.6, 'BGM_MAYUMI_ARR2': -19.4,
    'BGM_NITIJO_ARR2': -16.7, 'BGM_TYUUKA_B': -16.4, 'BGM_OMOTAIKEI_B': -16.6,
}
MAX_GAIN_DB = 9.0
FOLDER = 'himawari_bgm'
MAPPING_FILE = 'mapping.json'      # your own matching, saved by tools/bgm/bgm_matcher.py


def load_mapping(folder):
    """The saved mapping in folder (DS name -> [tracks]) over the built-in MAP."""
    m = {k: list(v) for k, v in MAP.items()}
    try:
        with open(os.path.join(folder, MAPPING_FILE), encoding='utf-8') as f:
            saved = json.load(f)
    except (OSError, ValueError):
        return m
    for k, v in saved.items():
        if isinstance(v, list) and all(isinstance(t, str) for t in v):
            m[k] = v
    return m


def needed(mapping=None):
    return sorted({t for ts in (mapping or MAP).values() for t in ts})


class AltBGM:
    """The extracted tracks in a data/himawari_bgm/ folder (see tools/bgm/extract_himawari.py)."""

    def __init__(self, folder):
        self.dir = folder
        with open(os.path.join(folder, 'tracks.json'), encoding='utf-8') as f:
            self.tracks = json.load(f)
        self.map = load_mapping(folder)

    def has(self, ds_name):
        return any(t in self.tracks for t in self.map.get(ds_name, ()))

    def pick(self, ds_name, ctx=None):
        """The replacement for ds_name in the scene ctx (a stable key, or None)."""
        opts = [t for t in self.map.get(ds_name, ()) if t in self.tracks]
        if not opts:
            return None
        if ctx is None or len(opts) == 1:
            return opts[0]
        return opts[zlib.crc32(repr(ctx).encode()) % len(opts)]

    def path(self, track):
        return os.path.join(self.dir, self.tracks[track]['file'])

    def loop(self, track):
        """(loop_from, loop_to) in the file's own samples, and its sample rate."""
        info = self.tracks[track]
        return (tuple(info['loop']) if info.get('loop') else None), info.get('rate', 44100)

    def next_after(self, ds_names, ctx):
        """[(track, ds name)] the next scenes will want, given the DS tracks of the
        chapter's next op 96s in order (for prefetching)."""
        out = []
        for i, n in enumerate(ds_names):
            c = ctx[:-1] + (ctx[-1] + 1 + i,) if ctx else None
            t = self.pick(n, c)
            if t is not None and (t, n) not in out:
                out.append((t, n))
        return out


# ------------------------------------------------------------------ decoding
# SDL_mixer holds the mixer's audio lock while it decodes an Ogg file, which
# would freeze the game (and the music already playing) for up to a second per
# track. So the tracks are decoded in a small helper process with its own silent
# mixer; it sends back raw 16-bit stereo PCM at our mixing rate.

def _worker(conn, rate):
    os.environ['SDL_AUDIODRIVER'] = 'dummy'
    os.environ.setdefault('PYGAME_HIDE_SUPPORT_PROMPT', '1')
    import pygame
    pygame.mixer.init(rate, -16, 2, 1024)
    while True:
        try:
            path = conn.recv()
        except (EOFError, OSError):
            return
        if path is None:
            return
        try:
            snd = pygame.mixer.Sound(file=path)
            raw = snd.get_raw()
            conn.send(('ok', len(raw)))
            conn.send_bytes(raw)
        except Exception as e:            # report, keep serving
            conn.send(('error', str(e)))


class Decoder:
    """decode(path) -> int16 bytes (stereo, at rate) or None; thread-safe, one job at a time."""

    def __init__(self, rate):
        self.rate = rate
        self.lock = threading.Lock()
        self.conn = self.proc = None
        self.failed = False

    def _start(self):
        import multiprocessing as mp
        ctx = mp.get_context('spawn')
        a, b = ctx.Pipe()
        self.proc = ctx.Process(target=_worker, args=(b, self.rate), daemon=True)
        self.proc.start()
        self.conn = a

    def decode(self, path):
        with self.lock:
            if self.failed:
                return None
            try:
                if self.proc is None or not self.proc.is_alive():
                    self._start()
                self.conn.send(path)
                status = self.conn.recv()
                if status[0] != 'ok':
                    return None
                return self.conn.recv_bytes()
            except (OSError, EOFError, RuntimeError, ImportError):
                self.failed = True        # no helper process (e.g. a frozen build without freeze_support)
                return None

    def close(self):
        if self.conn is not None:
            try:
                self.conn.send(None)
            except OSError:
                pass


def find(dirs):
    for d in dirs:
        for sub in (os.path.join(d, FOLDER), os.path.join(d, '..', FOLDER)):
            if os.path.isfile(os.path.join(sub, 'tracks.json')):
                try:
                    return AltBGM(os.path.abspath(sub))
                except (OSError, ValueError):
                    pass
    return None

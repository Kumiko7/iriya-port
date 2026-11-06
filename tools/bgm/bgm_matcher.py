"""A local page for matching the DS tracks with Himawari's: listen to both side by
side, see where each is used, and save the matching the game uses for
オプション → ＢＧＭ → ひまわり.

    python tools/bgm/bgm_matcher.py [--port 8765] [--no-browser]

It opens http://localhost:8765/ in your browser. The DS music comes from your
ROMs, Himawari's from your copy (data/himawari/, as for
extract_himawari.py) or from the already extracted data/himawari_bgm/ folder.
Saving writes data/himawari_bgm/mapping.json and extracts any newly chosen tracks.

The first start also works out where each track plays: it runs every chapter of
both games headless (a few minutes) and reads Himawari's scenarios. The result
is cached in data/himawari_bgm/usage.json.
"""
import argparse
import collections
import io
import json
import os
import re
import sys
import tempfile
import threading
import wave
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
PORT_DIR = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, PORT_DIR)
sys.path.insert(0, HERE)
os.environ['SDL_VIDEODRIVER'] = 'dummy'
os.environ['SDL_AUDIODRIVER'] = 'dummy'
os.environ['PYGAME_HIDE_SUPPORT_PROMPT'] = '1'
# the engine keeps settings and progress under %APPDATA%; this tool must not
# touch yours (the analysis plays through every chapter)
os.environ['APPDATA'] = tempfile.mkdtemp(prefix='iriya_matcher_')

from iriya_engine import alt_bgm, paths                  # noqa: E402
import extract_himawari as XH                             # noqa: E402

OUT = paths.data_path(alt_bgm.FOLDER)
USAGE = os.path.join(OUT, 'usage.json')
PAGE = os.path.join(HERE, 'bgm_matcher.html')

# what the DS track names mean (romanised Japanese)
GLOSS = {
    'NITIJO': '日常', 'IRIYA': '伊里野', 'AKIHO': '晶穂', 'MAYUMI': '椎名真由美', 'SUIZENJI': '水前寺',
    'YOKAN': '予感', 'GENSOU': '幻想', 'YUMEUTSUTSU': '夢現', 'SEIJYAKU': '静寂', 'KEIHOU': '警報',
    'ROUKA': '廊下', 'ENBAN': '円盤', 'AISETSU': '哀切', 'TOUSOU': '逃走', 'GAKUSAI': '学祭',
    'INBOU': '陰謀', 'KONWAKU': '困惑', 'APPAKU': '圧迫', 'LEMON': 'レモン', 'SENNYU': '潜入',
    'KENSOU': '喧騒', 'DATURYOKU': '脱力', 'SHOUSOU': '焦燥', 'KINPAKU': '緊迫', 'CITY': '街',
    'FUAN': '不安', 'KANDOU': '感動', 'MOUSOU': '妄想', 'MAIMU': 'マイム・マイム', 'SINSEKAI': '新世界',
    'KESSEN': '決戦', 'IKARIBAKUHATU': '怒り爆発', 'NAZO': '謎', 'BONYOU': '凡庸', 'HOUKAI': '崩壊',
    'HOUKAIMAE': '崩壊前', 'KINKYUU': '緊急', 'OMOTAIKEI': '重たい系', 'SASUPENSUKEI': 'サスペンス系',
    'TYUUKA': '中華', 'YUUSEN': '優先',
}


def gloss(name):
    parts = name[4:].split('_')
    g = GLOSS.get(parts[0], '')
    extra = {'ARR': 'アレンジ', 'ARR2': 'アレンジ2', 'B': 'B', 'SHORT': '短', 'RE': 'リテイク'}
    tail = ' '.join(extra.get(p, p) for p in parts[1:])
    return (g + (' ' + tail if tail else '')).strip()


# ---------------------------------------------------------------- the games
class World:
    def __init__(self):
        from iriya_engine import app as A
        self.A = A
        self.app = A.App(paths.search_dirs())
        self.games = self.app.games
        self.lock = threading.Lock()
        self.ds = {}                       # name -> {game: sid}
        for gid, g in self.games.items():
            for sid, n in enumerate(g.sdat.names):
                if str(n).startswith('BGM'):
                    self.ds.setdefault(str(n), {})[gid] = sid
        self.source = None                 # Himawari: (read, origin, names)
        try:
            self.source = XH.open_source(paths.data_path('himawari'))
        except (OSError, ValueError) as e:
            print('Himawari source not found (only extracted tracks are playable):', e)
        self.extracted = self._load_tracks()
        self.usage = None
        self.progress = ''
        try:
            with open(USAGE, encoding='utf-8') as f:
                self.usage = json.load(f)
        except (OSError, ValueError):
            threading.Thread(target=self._analyse, daemon=True).start()

    def _load_tracks(self):
        try:
            with open(os.path.join(OUT, 'tracks.json'), encoding='utf-8') as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def him_names(self):
        names = set(self.extracted)
        if self.source:
            names |= set(XH.bgm_names(self.source[2]))
        return sorted(names)

    # ------------------------------------------------------------ audio
    def ds_wav(self, gid, sid):
        g = self.games.get(gid)
        if g is None:
            return None
        with self.lock:
            p = g.pcm(sid)
        if p is None:
            return None
        buf = io.BytesIO()
        with wave.open(buf, 'wb') as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(44100)
            w.writeframes(p[0].tobytes())
        return buf.getvalue()

    def him_ogg(self, name):
        info = self.extracted.get(name)
        if info:
            p = os.path.join(OUT, info['file'])
            if os.path.exists(p):
                with open(p, 'rb') as f:
                    return f.read()
        if self.source:
            return self.source[0](f'bgm/{name}.ogg')
        return None

    # ------------------------------------------------------------ saving
    def save(self, mapping):
        ds_names = set(self.ds)
        him = set(self.him_names())
        clean = {}
        for k, v in mapping.items():
            if k in ds_names and isinstance(v, list):
                clean[k] = [t for t in v if isinstance(t, str) and t in him]
        os.makedirs(OUT, exist_ok=True)
        with open(os.path.join(OUT, alt_bgm.MAPPING_FILE), 'w', encoding='utf-8') as f:
            json.dump(clean, f, indent=1, ensure_ascii=False, sort_keys=True)
        written, missing = [], []
        want = alt_bgm.needed(alt_bgm.load_mapping(OUT))
        lacking = [t for t in want if t not in self.extracted]
        if lacking and self.source:
            written, missing = XH.extract(self.source[0], lacking, OUT)
        elif lacking:
            missing = lacking
        self.extracted = self._load_tracks()
        return {'ok': True, 'written': written, 'missing': missing}

    def reset(self):
        try:
            os.remove(os.path.join(OUT, alt_bgm.MAPPING_FILE))
        except OSError:
            pass
        return {'ok': True}

    # ------------------------------------------------------------ usage
    def _analyse(self):
        try:
            usage = {'ds': self._ds_usage(), 'him': self._him_usage()}
            os.makedirs(OUT, exist_ok=True)
            with open(USAGE, 'w', encoding='utf-8') as f:
                json.dump(usage, f, ensure_ascii=False)
            self.usage = usage
            self.progress = 'done'
        except Exception as e:                 # show it on the page rather than die
            self.progress = f'analysis failed: {e}'
            raise

    def _ds_usage(self):
        app = self.app
        app.settings['char_frames'] = 0
        app.audio.ok = False                   # silence; we only count text
        per = collections.defaultdict(lambda: {'n': collections.Counter(), 'scenes': collections.Counter(), 'ctx': []})
        totals = collections.Counter()
        order = list(app.order)
        for k, (gid, chap) in enumerate(order):
            self.progress = f'DS chapters {k}/{len(order)}'
            with self.lock:
                app.start_chapter(gid, chap)
            ses = app.session
            sc = ses.scene
            g = app.games[gid]
            cur = {'key': None, 'text': ''}

            def key():
                return str(g.sdat.names[sc.bgm]) if sc.bgm is not None and sc.bgm_on else None

            def flush():
                if cur['key'] and cur['text']:
                    e = per[cur['key']]
                    if len(e['ctx']) < 6 and len(cur['text']) > 40:
                        e['ctx'].append(f"{'I' if gid == 'iriya1' else 'II'}-{chap}: {cur['text'][:110]}")
                cur['text'] = ''

            orig = sc.add_glyph

            def hook(idx, orig=orig):
                k2 = key()
                if k2 != cur['key']:
                    flush()
                    cur['key'] = k2
                    if k2:
                        per[k2]['scenes'][gid] += 1
                if idx != 614 and idx & 0xFF00 not in (0x2100, 0x2200):
                    totals[gid] += 1
                    if k2:
                        per[k2]['n'][gid] += 1
                        if len(cur['text']) < 120:
                            cur['text'] += g.font.to_char.get(idx, '')
                return orig(idx)
            sc.add_glyph = hook
            f = 0
            while not ses.vm.finished and f < 2000000:
                with self.lock:
                    ses.update()
                f += 1
                if ses.vm.waiting_click():
                    ses.click()
                if app.session is not ses:
                    break
            flush()
        out = {}
        for name, e in per.items():
            out[name] = {'share': {gid: round(100 * e['n'][gid] / totals[gid], 2) for gid in totals if e['n'][gid]},
                         'scenes': dict(e['scenes']), 'ctx': e['ctx']}
        return out

    def _him_usage(self):
        if not self.source:
            return {}
        self.progress = 'Himawari scenarios'
        read, _origin, names = self.source
        titles = {}
        extra = _text(read('scenario/extra.ks'))
        if extra:
            cur = None
            for line in extra.splitlines():
                m = re.search(r"playfilename='([^']+)'", line)
                if m:
                    cur = m.group(1)
                m = re.search(r'ch2 text="([^"]*)"', line)
                if m and cur and not line.startswith(';'):
                    titles[cur] = m.group(1)
                    cur = None
        tagre = re.compile(r'\[[^\]]*\]')
        n = collections.Counter()
        scenes = collections.Counter()
        ctx = collections.defaultdict(list)
        total = 0
        for path in sorted(x for x in names if x.startswith('scenario_main/') and x.endswith('.ks')):
            t = _text(read(path))
            if not t:
                continue
            cur, buf = None, ''
            for line in t.splitlines():
                st = line.strip()
                if not st or st[0] in ';*':
                    continue
                if st.startswith('@'):
                    m = re.match(r'@bgm\b.*?\bf=("?)([^"\s]+)\1', st)
                    new = cur
                    if m:
                        new = m.group(2).replace('.ogg', '')
                    elif re.match(r'@(stop|pause)\b', st):
                        new = None
                    if new != cur:
                        if cur and len(buf) > 40 and len(ctx[cur]) < 6:
                            ctx[cur].append(buf[:110])
                        cur, buf = new, ''
                        if cur:
                            scenes[cur] += 1
                    continue
                s = tagre.sub('', st)
                total += len(s)
                if cur:
                    n[cur] += len(s)
                    if len(buf) < 120:
                        buf += s + '／'
        out = {}
        for name in set(XH.bgm_names(names)) | set(titles):
            out[name] = {'title': titles.get(name, ''), 'share': round(100 * n[name] / max(1, total), 2),
                         'scenes': scenes[name], 'ctx': ctx.get(name, [])}
        return out

    # ------------------------------------------------------------ state
    def state(self):
        mapping = alt_bgm.load_mapping(OUT)
        saved = os.path.exists(os.path.join(OUT, alt_bgm.MAPPING_FILE))
        u = self.usage or {'ds': {}, 'him': {}}
        ds = []
        for name, games in sorted(self.ds.items()):
            e = u['ds'].get(name, {})
            ds.append({'name': name, 'gloss': gloss(name), 'games': games, 'share': e.get('share', {}),
                       'scenes': e.get('scenes', {}), 'ctx': e.get('ctx', []),
                       'loud': alt_bgm.DS_LOUDNESS.get(name)})
        him = []
        for name in self.him_names():
            e = u['him'].get(name, {})
            him.append({'name': name, 'title': e.get('title', ''), 'share': e.get('share', 0),
                        'scenes': e.get('scenes', 0), 'ctx': e.get('ctx', []),
                        'extracted': name in self.extracted})
        return {'ds': ds, 'him': him, 'map': mapping, 'builtin': alt_bgm.MAP, 'saved': saved,
                'usage': 'ready' if self.usage else (self.progress or 'starting'),
                'source': self.source[1] if self.source else None}


def _text(raw):
    if not raw:
        return ''
    if raw[:2] in (b'\xff\xfe', b'\xfe\xff'):
        return raw.decode('utf-16', 'replace')
    for enc in ('utf-8-sig', 'cp932'):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            pass
    return raw.decode('cp932', 'replace')


# ---------------------------------------------------------------- the server
class Handler(BaseHTTPRequestHandler):
    world = None

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype, extra=None):
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store' if ctype.startswith(('application/json', 'text/html')) else 'max-age=3600')
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode('utf-8'), 'application/json; charset=utf-8')

    def _audio(self, data, ctype):
        if data is None:
            return self._send(404, b'not found', 'text/plain')
        rng = self.headers.get('Range')
        m = re.match(r'bytes=(\d*)-(\d*)', rng or '')
        if m and (m.group(1) or m.group(2)):
            n = len(data)
            a = int(m.group(1)) if m.group(1) else n - int(m.group(2))
            b = int(m.group(2)) if m.group(1) and m.group(2) else n - 1
            a, b = max(0, a), min(n - 1, b)
            return self._send(206, data[a:b + 1], ctype,
                              {'Content-Range': f'bytes {a}-{b}/{n}', 'Accept-Ranges': 'bytes'})
        self._send(200, data, ctype, {'Accept-Ranges': 'bytes'})

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        path = unquote(urlparse(self.path).path)
        w = self.world
        if path in ('/', '/index.html'):
            with open(PAGE, 'rb') as f:
                return self._send(200, f.read(), 'text/html; charset=utf-8')
        if path == '/api/state':
            return self._json(w.state())
        m = re.match(r'^/audio/ds/(iriya[12])/(\d+)\.wav$', path)
        if m:
            return self._audio(w.ds_wav(m.group(1), int(m.group(2))), 'audio/wav')
        m = re.match(r'^/audio/h/(.+)\.ogg$', path)
        if m:
            return self._audio(w.him_ogg(m.group(1)), 'audio/ogg')
        self._send(404, b'not found', 'text/plain')

    def do_POST(self):
        path = urlparse(self.path).path
        n = int(self.headers.get('Content-Length') or 0)
        try:
            body = json.loads(self.rfile.read(n) or b'{}')
        except ValueError:
            return self._json({'ok': False, 'error': 'bad json'}, 400)
        if path == '/api/save':
            return self._json(self.world.save(body.get('map') or {}))
        if path == '/api/reset':
            return self._json(self.world.reset())
        self._send(404, b'not found', 'text/plain')


def main():
    ap = argparse.ArgumentParser(description=__doc__.split(chr(10))[0])
    ap.add_argument('--port', type=int, default=8765)
    ap.add_argument('--no-browser', action='store_true')
    args = ap.parse_args()
    print('loading the games...')
    Handler.world = World()
    srv = ThreadingHTTPServer(('127.0.0.1', args.port), Handler)
    url = f'http://localhost:{args.port}/'
    print('BGM matcher at', url, '(Ctrl+C to stop)')
    if not args.no_browser:
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()

"""Light-novel bonus chapters (番外編) that the DS games left out.

The two DS games cover the four Dengeki Bunko volumes, and II already contains
vol. 3's 「番外編・ＥＳＰの冬」. The 番外編 from vol. 1 and vol. 2 are not in
either game. If the user's own EPUBs of those volumes are found (an `epub`
folder next to the game or the ROMs), the chapter text and its title
illustration are read from them at runtime and compiled into a normal .nsb
chapter script, staged with backgrounds and music from Game I.

Nothing from the books is stored in this program.
"""
import html
import os
import re
import zipfile
import posixpath
import logging

log = logging.getLogger('bonus')

# chapter id -> (volume number, TOC title, index title)
BONUS = {
    'X1': (1, '番外編・そんなことだから', '番外編・そんなことだから'),
    'X2': (2, '番外編・死体を洗え', '番外編・死体を洗え'),
}
# Where they go in Game I's chapter list: after the staff roll, before あとがき
# (Game II places its own 番外編 the same way).
INSERT_BEFORE = 'D'

from .bonus_script import SCENE_BREAK, compile_chapter  # noqa: E402


# =========================================================================
# EPUB reading
class Epub:
    def __init__(self, path):
        self.path = path
        self.zip = zipfile.ZipFile(path)
        names = set(self.zip.namelist())
        cont = self.zip.read('META-INF/container.xml').decode('utf-8')
        self.opf_path = re.search(r'full-path="([^"]+)"', cont).group(1)
        opf = self.zip.read(self.opf_path).decode('utf-8')
        self.base = posixpath.dirname(self.opf_path)
        m = re.search(r'<dc:title[^>]*>([^<]*)', opf)
        self.title = html.unescape(m.group(1)) if m else ''
        self.manifest = {}
        nav = None
        for item in re.finditer(r'<item\b([^>]*)/?>', opf):
            attrs = dict(re.findall(r'([\w:-]+)="([^"]*)"', item.group(1)))
            href = posixpath.normpath(posixpath.join(self.base, html.unescape(attrs.get('href', ''))))
            self.manifest[attrs.get('id')] = href
            if 'nav' in attrs.get('properties', '').split():
                nav = href
        self.spine = [self.manifest[i] for i in re.findall(r'<itemref\b[^>]*idref="([^"]+)"', opf)
                      if i in self.manifest and self.manifest[i] in names]
        self.toc = []
        if nav:
            t = self.zip.read(nav).decode('utf-8')
            nbase = posixpath.dirname(nav)
            for href, label in re.findall(r'<a\b[^>]*href="([^"]+)"[^>]*>(.*?)</a>', t, re.S):
                href = posixpath.normpath(posixpath.join(nbase, html.unescape(href).split('#')[0]))
                self.toc.append((re.sub(r'<[^>]+>', '', label).strip(), href))

    @property
    def volume(self):
        m = re.search(r'その\s*([0-9０-９])', self.title)
        if not m:
            return None
        return int(m.group(1).translate(str.maketrans('０１２３４５６７８９', '0123456789')))

    def read(self, path):
        return self.zip.read(path)

    def chapter_files(self, title):
        """Spine documents from the TOC entry `title` up to the next TOC entry."""
        starts = [h for _, h in self.toc]
        hit = [h for t, h in self.toc if t == title]
        if not hit:
            return []
        i = self.spine.index(hit[0]) if hit[0] in self.spine else -1
        if i < 0:
            return []
        out = [hit[0]]
        for p in self.spine[i + 1:]:
            if p in starts:
                break
            out.append(p)
        return out


def find_epubs(dirs):
    """{volume: Epub} for the series' volumes found in dirs (and their epub/ subfolders)."""
    found = {}
    seen = set()
    for d in dirs:
        for sub in (d, os.path.join(d, 'epub'), os.path.join(d, '..', 'epub')):
            if not os.path.isdir(sub):
                continue
            for f in os.listdir(sub):
                p = os.path.abspath(os.path.join(sub, f))
                if not f.lower().endswith('.epub') or p in seen:
                    continue
                seen.add(p)
                try:
                    e = Epub(p)
                except (OSError, zipfile.BadZipFile, KeyError, AttributeError) as ex:
                    log.warning('cannot read %s: %s', p, ex)
                    continue
                if 'イリヤの空' in e.title and e.volume and e.volume not in found:
                    found[e.volume] = e
    return found


# ---------------------------------------------------------------- parsing
def parse_chapter(epub, title):
    """-> (illustration bytes or None, [paragraph]) where a paragraph is a list of
    tokens: ('t', text) | ('r', base, ruby) | ('tcy', digits), or SCENE_BREAK, or ''
    for a blank line."""
    image = None
    paras = []
    for path in epub.chapter_files(title):
        doc = epub.read(path).decode('utf-8')
        body = doc.split('<body', 1)[-1]
        if 'p-image' in body[:80] or ('<p' in body and not re.search(r'<p[^>]*>(?!\s*<img)[^<\s]', body)):
            m = re.search(r'<img\b[^>]*src="([^"]+)"', body)
            if m and image is None:
                ip = posixpath.normpath(posixpath.join(posixpath.dirname(path), html.unescape(m.group(1))))
                try:
                    image = epub.read(ip)
                except KeyError:
                    pass
            continue
        for m in re.finditer(r'<p\b[^>]*>(.*?)</p>', body, re.S):
            paras.append(_parse_para(m.group(1)))
    # drop leading / trailing blank lines (the book's chapter-opening indent)
    while paras and paras[0] == '':
        paras.pop(0)
    while paras and paras[-1] == '':
        paras.pop()
    return image, paras


def _parse_para(s):
    if 'gaiji' in s and '区切り' in s:
        return SCENE_BREAK
    if re.fullmatch(r'\s*<br\s*/?>\s*', s):
        return ''
    toks = []
    pos = 0
    for m in re.finditer(r'<ruby>(.*?)</ruby>|<span class="tcy">(.*?)</span>|<[^>]+>', s, re.S):
        if m.start() > pos:
            toks.append(('t', html.unescape(s[pos:m.start()])))
        if m.group(1) is not None:
            # <ruby>隙<rt>すき</rt>間<rt>ま</rt></ruby> -> one group per base/rt pair
            for base, rt in re.findall(r'(.*?)<rt>(.*?)</rt>', m.group(1), re.S):
                base = html.unescape(re.sub(r'<[^>]+>', '', base))
                rt = html.unescape(re.sub(r'<[^>]+>', '', rt))
                if base:
                    toks.append(('r', base, rt))
        elif m.group(2) is not None:
            toks.append(('tcy', html.unescape(re.sub(r'<[^>]+>', '', m.group(2)))))
        pos = m.end()
    if pos < len(s):
        toks.append(('t', html.unescape(s[pos:])))
    return [t for t in toks if t[0] != 't' or t[1]]


def plain(para):
    if not isinstance(para, list):
        return para
    out = ''
    for t in para:
        out += t[1] if t[0] != 'r' else f'{t[1]}《{t[2]}》'
    return out


# =========================================================================
# Staging: backgrounds, music, sounds and face windows from the games.
# Anchors are text from the paragraph the cue precedes (without ruby), matched
# in order. 'a|b' tries a first; '2/name' is a Game II image (Game I scripts
# can't otherwise reach it); 'x/name' is an image read from the EPUB.
# ('bgm', id[, fade_in=120[, fade_out=60]]) fades out the playing track first and
# waits for that, as the DS scripts do; ('bgm_out', frames) leaves a silence.
# Game I sound ids (see docs/bgm): BGM_NITIJO 31, BGM_MAYUMI 34, BGM_YOKAN 36, BGM_YUMEUTSUTSU 38,
# BGM_SEIJYAKU 39, BGM_ENBAN 42, BGM_AISETSU 43, BGM_INBOU 46, BGM_KONWAKU 47,
# BGM_APPAKU 48, BGM_KENSOU 51, BGM_DATURYOKU 52, BGM_SHOUSOU 53, BGM_KINPAKU 54,
# BGM_FUAN 56; SE_IRI_068 183 (speaker noise), SE_IRI_084 195 (receiver down),
# SE_IRI_088 198 (receiver up), SE_IRI_189l 238 (phone ringing),
# SE_IRI_057_2 246 (receiver + buttons), SE_IRI_WARNING 260 (siren).
D = '──'      # the book's dash
STAGING = {
    'X1': {
        'open': [('bg', 'ev_apart_01'), ('bgm', 34)],
        'cues': [
            ('部屋中に散らばっているダンボール箱を見回して', [('face', 'mayumi', 1)]),
            (D + 'いっか', [('face', 'mayumi', 2)]),
            ('部屋の隅に放り出したまま', [('face', 'mayumi', 1)]),
            ('のままベランダに出た', [('face_off',), ('bg', '2/bg_001_02|bg_001_02', 45)]),
            ('いつの間にこれほど日が長く', [('bg', 'bg_044_bla', 45)]),
            ('プルトップを開ける', [('bg', 'bg_001_02', 45)]),
            ('保健教諭として', [('face', 'mayumi', 5)]),
            ('基地に電話を入れるのを忘れていた', [('face', 'mayumi', 3)]),
            ('ガラス戸を開け放したまま部屋に戻った', [('bg', 'ev_apart_01', 30), ('face', 'mayumi', 1)]),
            ('その途端にベルが鳴った', [('bgm_out', 60), ('se', 238), ('face', 'mayumi', 6)]),
            ('もしもし、というヒマもなかった', [('se_stop', 1), ('se', 198, 2)]),
            ('おいテレビつけろテレビ', [('bgm', 52, 0), ('face', 'enomoto', 2)]),
            ('な、なによいきなり', [('face', 'mayumi', 6)]),
            ('画面の右上の表示が', [('bg', '2/ev_tv_news|ev_apart_02', 20), ('bgm', 42)]),
            ('見たことのある顔だった', [('face', 'mayumi', 5)]),
            ('つまりは、ＵＦＯ特番なのだった', [('face', 'mayumi', 1)]),
            ('少しがっかりした', [('face', 'mayumi', 7)]),
            ('ここで園原基地を呼んでみたい', [('bgm', 46)]),
            ('画面が切り替わった。そこに映し出された光景を', [('bg', '2/ev_tv_reporter|bg_kiti_image', 20),
                                                          ('face', 'mayumi', 6)]),
            ('アホはあんたよこのバカ', [('face', 'mayumi', 6), ('shake', 3, 2, 15)]),
            ('ったくデカい声出すなって', [('face', 'enomoto', 2)]),
            ('雲が出て夕暮れは深まり', [('bg', 'ev_apart_04', 60), ('dim', 170, 60), ('face', 'mayumi', 5)]),
            ('まあな。空母の方にも', [('face', 'enomoto', 3)]),
            ('あれはない。あれはないです', [('face', 'enomoto', 2)]),
            ('椎名真由美も苦笑した', [('face', 'mayumi', 2)]),
            ('この人、何て名前だっけ', [('face', 'mayumi', 1)]),
            ('息が止まった', [('face', 'mayumi', 6), ('bgm_out', 20)]),
            ('心配すんな、二回とも', [('face', 'enomoto', 1), ('bgm', 46)]),
            ('やっぱりキレてた', [('face', 'mayumi', 3)]),
            ('グラスワークってなに', [('face', 'mayumi', 3)]),
            ('奴だけじゃない。', [('face', 'enomoto', 3)]),
            ('ある意味ものすごく純真な人', [('face', 'enomoto', 4), ('bgm', 39)]),
            ('その筆頭って実はおれたちなのかも', [('face', 'enomoto', 3)]),
            ('あんたさ、生顔はさすがに', [('face', 'mayumi', 2)]),
            ('やなこったみっともねえ', [('face', 'enomoto', 2)]),
            ('お住まいの河口さん', [('bgm_out', 60), ('bg', '2/ev_tv_news|ev_apart_02', 20)]),
            ('極秘情報', [('bgm', 53), ('face', 'mayumi', 3)]),
            ('やべ』', [('face', 'enomoto', 4)]),
            ('みろ言わんこっちゃねえ', [('face', 'enomoto', 3), ('shake', 3, 2, 20)]),
            ('ねえ、水前寺って誰よ', [('face', 'mayumi', 6)]),
            ('悪い、忙しくなるから切るぞ', [('face', 'enomoto', 3)]),
            ('受話器が叩きつけられ', [('se', 195, 2), ('face_off', 'enomoto'), ('bgm_out', 60)]),
            ('何が何だかわからなかった', [('face', 'mayumi', 7)]),
            ('大板橋です！', [('face_off',), ('bg', '2/bg_syuusekijyo_yoru|bg_030_01', 20), ('bgm', 51, 0)]),
            ('現場は混乱していた', [('shake', 3, 2, 30)]),
            ('すみません、このコンテナの中身を', [('bg', '2/ev_tv_reporter|bg_kiti_image', 15)]),
            ('暴動、に近い状況', [('bg', '2/bg_syuusekijyo_yoru|bg_030_01', 15), ('shake', 3, 3, 40)]),
            ('ダッチワイフだった', [('bgm_out', 0), ('wait', 30)]),
            ('カメラのレンズを誰かの手が覆い', [('bg', '2/ev_tv_news|ev_apart_02', 10)]),
            ('しばらくお待ちください', [('bg', 'bg_white', 10), ('bgm', 52, 0)]),
            ('六畳間の中途半端な闇に、電話のベルが鳴る', [('bg', 'ev_apart_04', 45), ('dim', 170, 1),
                                                       ('se', 238)]),
            ('のろのろと受話器を取って', [('se_stop', 1), ('se', 198, 2), ('face', 'mayumi', 7)]),
            ('ため息が聞こえた', [('face', 'enomoto', 4)]),
            ('どっちだと思う', [('face', 'enomoto', 3)]),
            ('兵隊さんもいろいろと大変ですよね', [('face_off',), ('bg', '2/ev_tv_news|ev_apart_02', 30)]),
            ('六畳間には今、夜の闇が満ちている', [('bgm_out', 120), ('bg', 'ev_apart_04', 45), ('dim', 150, 1)]),
            ('保健室の先生、か', [('bg', '2/bg_001_06|bg_001_03', 60), ('bgm', 34), ('face', 'mayumi', 1)]),
            ('左手で金属バットのグリップを握る', [('face', 'mayumi', 5)]),
            ('園原電波新聞、か', [('face', 'mayumi', 2)]),
            ('今度の新聞屋は', [('face', 'mayumi', 4)]),
        ],
    },
    'X2': {
        'open': [('bg', '2/ev_denwabox_up|bg_024_01'), ('bgm', 31)],
        'cues': [
            ('受話器を取る。カードを挿し込む。', [('se', 246, 2)]),
            ('すっごく変な奴にナンパされちゃった', [('bgm', 47)]),
            ('そしたらね', [('bgm_out', 60)]),
            ('出身どこ？', [('bg', '2/bg_tetujin_tennai_long|bg_ff_3f', 30), ('bgm', 36)]),
            ('園原基地で死体を洗うバイト', [('bgm', 56), ('dim', 200, 30)]),
            ('木村はそう言って部屋から出ていった', [('bg', '2/bg_konbini_hiru|bg_shotengai', 30)]),
            ('迎えは、すぐに来た', [('bg', '2/bg_kamotu_tenjyo|bg_030_01', 45), ('dim', 180, 30)]),
            ('結論を言っちまおう', [('bgm_out', 90), ('bg', 'bg_030_02', 45), ('dim', 170, 30)]),
            ('変な話だけどさ', [('bgm', 43)]),
            ('やれることはみんなやって、最後には', [('bg', '2/bg_tetujin_tennai_long|bg_ff_3f', 45)]),
            ('まず、木村はあの仕事をどうやって', [('bgm', 46)]),
            ('そう。木村だ。', [('bg', 'bg_030_02', 45), ('dim', 150, 30), ('bgm', 48)]),
            ('エイリアンがいる', [('bgm', 54, 0, 0), ('flash', 20), ('shake', 3, 3, 30)]),
            ('警報が鳴り始めた', [('se', 260, 3)]),
            ('奴の叫び声が聞こえなくなった', [('bg', '2/bg_kiti_rouka|bg_031_01', 30)]),
            ('扉の中には、木村とおれが', [('bg', 'bg_032_02', 30), ('se_stop', 3, 60)]),
            ('木村が、水槽のハッチを閉めて', [('bgm_out', 90)]),
            ('その次の瞬間', [('flash', 10), ('shake', 3, 4, 20)]),
            ('あれ、そう言えば、さっきから手も口も', [('bg', '2/bg_tetujin_tennai_long|bg_ff_3f', 45),
                                                    ('bgm', 52)]),
            ('どうしてあのまま解放されたのか', [('bgm', 42)]),
            ('バンは、おれのアパートの近所の', [('bg', '2/bg_konbini_yuugata|bg_043_01', 45)]),
            ('その翌日に、おれは何だか心配になって', [('bg', 'bg_035_01', 45), ('dim', 160, 30), ('bgm', 56)]),
            ('おれの話は、これでおしまい', [('bgm_out', 90)]),
            ('ちゅみ、よく聞きなさい', [('bg', '2/ev_denwabox_up|bg_024_01', 30), ('bgm', 52)]),
            ('もしもしちゅみ？', [('bgm_out', 0), ('se', 183, 2), ('flash', 8), ('shake', 3, 2, 20)]),
            ('ちゅみよね、やっぱり', [('bgm', 56)]),
            ('ねえ待ってちゅみ', [('shake', 3, 1, 30)]),
            ('受話器が置かれる', [('se', 195, 2), ('bgm_out', 120), ('dim', 120, 90)]),
        ],
    },
}


# =========================================================================
def _title_loader(jpeg):
    """A loader for the chapter title page: the book illustration on the left
    page (DS top screen), white paper elsewhere, as a 480x272 background."""
    def load(k=1):
        import io
        import pygame
        try:
            img = pygame.image.load(io.BytesIO(jpeg))
        except pygame.error as e:
            log.warning('cannot decode the title illustration: %s', e)
            return None
        w, h = img.get_size()
        th = 256 * k
        tw = max(1, round(w * th / h))
        if tw > 192 * k:
            tw, th = round(192 * k), max(1, round(h * 192 * k / w))
        img = pygame.transform.smoothscale(img.convert(), (round(tw), round(th)))
        out = pygame.Surface((round(480 * k), round(272 * k))).convert()
        out.fill((255, 255, 255))
        out.blit(img, (round(8 * k + (192 * k - img.get_width()) / 2), round(8 * k + (256 * k - img.get_height()) / 2)))
        return out
    return load


def install(games, dirs, epubs=None):
    """Add the 番外編 found in the user's EPUBs to Game I's chapter list."""
    g1 = games.get('iriya1')
    if g1 is None:
        return []
    g1.alt = games.get('iriya2')
    if epubs is None:
        try:
            epubs = find_epubs(dirs)
        except OSError:
            return []
    added = []
    chapters = list(g1.chapters)
    for cid, (vol, toc_title, title) in BONUS.items():
        e = epubs.get(vol)
        if e is None:
            continue
        try:
            image, paras = parse_chapter(e, toc_title)
            if not paras:
                continue
            img_name = None
            if image:
                img_name = 'x/title_' + cid
                g1.extra_images[img_name[2:]] = _title_loader(image)
            data, _unused, _missing = compile_chapter(g1.font, paras, STAGING.get(cid, {}), img_name)
        except Exception as ex:                  # a damaged book must not stop the game
            log.warning('cannot build %s from %s: %s', title, e.path, ex)
            continue
        g1.extra_scripts[cid] = data
        g1.extra_titles[cid] = title
        at = next((i for i, (c, _) in enumerate(chapters) if c == INSERT_BEFORE), len(chapters))
        chapters.insert(at, (cid, title))
        added.append(cid)
    g1.chapters = chapters
    return added

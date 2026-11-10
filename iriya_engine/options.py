"""Options screen, including the high-res font combobox with a live preview."""
import os
import pygame

from .hires import HiresText, font_label, list_japanese_fonts


class OptionsState:
    KEYS = ['text', 'fx', 'auto', 'script', 'bgm', 'bgmset', 'se', 'scale', 'full', 'smooth', 'art', 'hires',
            'font', 'back']
    HIRES_ROW = KEYS.index('hires')
    FONT_ROW = KEYS.index('font')
    BACK_ROW = KEYS.index('back')
    COMBO_ROWS = 10
    ITEM_H = 16
    ROW_PITCH = 15
    ROW_TOP = 27
    hires_always = True   # draw_hires is called even with high-res text off (font preview)

    def __init__(self, app, back):
        from . import app as A
        self.A = A
        self.app = app
        self.back = back
        self.sel = 0
        self.combo = None          # {'items': [(label, path)], 'sel': int, 'top': int}
        self.script0 = app.settings.get('script', 'corrected')
        self._preview_fonts = {}

    def update(self):
        pass

    # ------------------------------------------------------------ rows
    def _rows(self):
        A = self.A
        s = self.app.settings
        ts = min(A.TEXT_SPEEDS, key=lambda x: abs(x[1] - s['char_frames']))[0]
        au = min(A.AUTO_SPEEDS, key=lambda x: abs(x[1] - s.get('auto_frames', 30)))[0]
        pct = lambda v: f'{int(round(v * 100)):3d}％'.replace(' ', '　')
        font = font_label(self.app.hires.path) if self.app.hires.ok else 'なし'
        if len(font) > 9:
            font = font[:8] + '…'
        return [
            ('文字速度', ts),
            ('演出速度', '速い' if s.get('fast_fx') else '原作'),
            ('オート速度', au),
            ('本文', ('修正版' if s.get('script', 'corrected') == 'corrected' else 'ＤＳ版')
             if self._can_correct() else 'ＤＳ版のみ'),
            ('ＢＧＭ音量', pct(s['bgm_volume'])),
            ('ＢＧＭ', ('ひまわり' if s.get('bgm_set') == 'himawari' else '原作') if self.app.audio.alt else '原作のみ'),
            ('効果音音量', pct(s['se_volume'])),
            ('画面サイズ', f'×{s["scale"]}'),
            ('全画面', 'オン' if s['fullscreen'] else 'オフ'),
            ('拡大方法', 'なめらか' if s.get('smooth') else 'ドット'),
            ('画質', ('高画質' if s.get('art', 'hd') == 'hd' else '原画') if self.app.hd_ready else '原画のみ'),
            ('高解像度文字', ('オン' if s.get('hires_text') else 'オフ') if self.app.hires.ok else 'フォントなし'),
            ('フォント', font + '▼'),
            ('戻る', ''),
        ]

    def _change(self, i, d):
        A = self.A
        s = self.app.settings
        key = self.KEYS[i]
        if key == 'text':
            vals = [v for _, v in A.TEXT_SPEEDS]
            k = min(range(len(vals)), key=lambda j: abs(vals[j] - s['char_frames']))
            s['char_frames'] = vals[(k + d) % len(vals)]
        elif key == 'fx':
            s['fast_fx'] = not s.get('fast_fx')
        elif key == 'auto':
            vals = [v for _, v in A.AUTO_SPEEDS]
            k = min(range(len(vals)), key=lambda j: abs(vals[j] - s.get('auto_frames', 30)))
            s['auto_frames'] = vals[(k + d) % len(vals)]
        elif key == 'script':
            if not self._can_correct():
                return
            s['script'] = 'ds' if s.get('script', 'corrected') == 'corrected' else 'corrected'
        elif key in ('bgm', 'se'):
            key = 'bgm_volume' if key == 'bgm' else 'se_volume'
            s[key] = round(max(0.0, min(1.0, s[key] + 0.1 * d)), 2)
            self.app._apply_volume()
        elif key == 'bgmset':
            if not self.app.audio.alt:
                return
            s['bgm_set'] = 'ds' if s.get('bgm_set') == 'himawari' else 'himawari'
            self.app.audio.set_alt(s['bgm_set'] == 'himawari')
        elif key == 'scale':
            s['scale'] = max(1, min(6, s['scale'] + d))
            if not s['fullscreen']:
                self.app._apply_video()
        elif key == 'full':
            self.app.toggle_fullscreen()
        elif key == 'smooth':
            s['smooth'] = not s.get('smooth')
        elif key == 'art':
            if not self.app.hd_ready:
                return
            s['art'] = 'original' if s.get('art', 'hd') == 'hd' else 'hd'
        elif key == 'hires':
            s['hires_text'] = not s.get('hires_text')
        elif i == self.FONT_ROW:
            self._open_combo()
            return
        elif i == self.BACK_ROW:
            self.app.save_settings()
            self._leave()
            return
        self.app.save_settings()
        self.app.ui_sound('move')

    def _rect(self, i):
        return pygame.Rect(self.A.RIGHT + 6, self.ROW_TOP + i * self.ROW_PITCH, self.A.PAGE_W - 12, 14)

    def _can_correct(self):
        return any(getattr(g, 'corrector', None) for g in self.app.games.values())

    def _leave(self):
        """Back to where options was opened. If 本文 changed during a chapter, reload it
        at the current page so the other text shows at once."""
        ses = self.app.session
        if (self.app.settings.get('script', 'corrected') != self.script0 and ses is not None
                and isinstance(self.back, self.A.PauseState)):
            self.app.start_chapter(ses.gid, ses.chap, ses.scene.page_line)
            return
        self.app.goto(self.back)

    # ------------------------------------------------------------ combobox
    def _open_combo(self):
        items = list_japanese_fonts()
        if not items:
            return
        cur = os.path.normcase(os.path.abspath(self.app.hires.path or ''))
        sel = next((k for k, (_, p) in enumerate(items)
                    if os.path.normcase(os.path.abspath(p)) == cur), 0)
        top = max(0, min(sel - self.COMBO_ROWS // 2, len(items) - self.COMBO_ROWS))
        self.combo = {'items': items, 'sel': sel, 'top': top}
        self.app.ui_sound('enter')

    def _combo_rect(self):
        n = min(self.COMBO_ROWS, len(self.combo['items']))
        row = self._rect(self.FONT_ROW)
        h = n * self.ITEM_H + 4
        return pygame.Rect(row.x, row.y - h - 1, row.w, h)

    def _combo_item_rect(self, vis):
        r = self._combo_rect()
        return pygame.Rect(r.x + 2, r.y + 2 + vis * self.ITEM_H, r.w - 8, self.ITEM_H)

    def _combo_scroll(self, d):
        c = self.combo
        c['top'] = max(0, min(c['top'] + d, len(c['items']) - self.COMBO_ROWS))

    def _combo_pick(self, idx):
        _label, path = self.combo['items'][idx]
        self.combo = None
        self.app.settings['hires_font'] = path
        self.app.hires = HiresText(path)
        self.app.save_settings()
        self.app.ui_sound('enter')

    def _combo_event(self, ev):
        c = self.combo
        n = len(c['items'])
        if self.A.is_back(ev):
            self.combo = None
            self.app.ui_sound('cancel')
            return
        if ev.type == pygame.KEYDOWN:
            if ev.key in (pygame.K_DOWN, pygame.K_s):
                c['sel'] = min(n - 1, c['sel'] + 1)
            elif ev.key in (pygame.K_UP, pygame.K_w):
                c['sel'] = max(0, c['sel'] - 1)
            elif ev.key == pygame.K_PAGEDOWN:
                c['sel'] = min(n - 1, c['sel'] + self.COMBO_ROWS)
            elif ev.key == pygame.K_PAGEUP:
                c['sel'] = max(0, c['sel'] - self.COMBO_ROWS)
            elif ev.key in (pygame.K_RETURN, pygame.K_SPACE, pygame.K_z, pygame.K_KP_ENTER):
                self._combo_pick(c['sel'])
                return
            if c['sel'] < c['top']:
                c['top'] = c['sel']
            elif c['sel'] >= c['top'] + self.COMBO_ROWS:
                c['top'] = c['sel'] - self.COMBO_ROWS + 1
        elif ev.type == pygame.MOUSEMOTION:
            for vis in range(min(self.COMBO_ROWS, n)):
                if self._combo_item_rect(vis).collidepoint(ev.lpos):
                    c['sel'] = c['top'] + vis
        elif ev.type == pygame.MOUSEBUTTONDOWN:
            if ev.button == 4:
                self._combo_scroll(-1)
            elif ev.button == 5:
                self._combo_scroll(1)
            elif ev.button == 1:
                for vis in range(min(self.COMBO_ROWS, n)):
                    if self._combo_item_rect(vis).collidepoint(ev.lpos):
                        self._combo_pick(c['top'] + vis)
                        return
                self.combo = None          # clicking outside closes the list
                self.app.ui_sound('cancel')

    # ------------------------------------------------------------ events
    def event(self, ev):
        if self.combo is not None:
            self._combo_event(ev)
            return
        if self.A.is_back(ev):
            self.app.save_settings()
            self.app.ui_sound('cancel')
            self._leave()
            return
        n = len(self._rows())
        if ev.type == pygame.KEYDOWN:
            if ev.key in (pygame.K_DOWN, pygame.K_s):
                self.sel = (self.sel + 1) % n
            elif ev.key in (pygame.K_UP, pygame.K_w):
                self.sel = (self.sel - 1) % n
            elif ev.key in (pygame.K_LEFT, pygame.K_a):
                if self.sel != self.FONT_ROW:
                    self._change(self.sel, -1)
            elif ev.key in (pygame.K_RIGHT, pygame.K_d, pygame.K_RETURN, pygame.K_SPACE):
                self._change(self.sel, 1)
        elif ev.type == pygame.MOUSEMOTION:
            for i in range(n):
                if self._rect(i).collidepoint(ev.lpos):
                    self.sel = i
        elif ev.type == pygame.MOUSEBUTTONDOWN and ev.button in (1, 4, 5):
            for i in range(n):
                if self._rect(i).collidepoint(ev.lpos):
                    self.sel = i
                    if i == self.FONT_ROW and ev.button != 1:
                        continue
                    self._change(i, -1 if ev.button == 5 else 1)

    # ------------------------------------------------------------ drawing
    def _preview_path(self):
        if self.combo is not None:
            return self.combo['items'][self.combo['sel']][1]
        if self.sel in (self.HIRES_ROW, self.FONT_ROW):
            return self.app.hires.path
        return None

    def _preview_rect(self):
        return pygame.Rect(6, 204, self.A.PAGE_W - 12, 46)

    def draw(self, surf):
        A, app = self.A, self.app
        if app.session:
            app.session.draw(surf)
            dim = pygame.Surface(A.LOGICAL, pygame.SRCALPHA)
            dim.fill((0, 0, 20, 190))
            surf.blit(dim, (0, 0))
        else:
            surf.fill((14, 22, 46))
        A.draw_spine(surf)
        app.text(surf, 'オプション', A.RIGHT + 8, 10, A.GOLD)
        for i, (k, v) in enumerate(self._rows()):
            r = self._rect(i)
            A.panel(surf, r, 200 if i == self.sel else 120)
            if i == self.sel:
                pygame.draw.rect(surf, A.GOLD, r, 1)
            app.text(surf, k, r.x + 4, r.y + 1, A.GOLD if i == self.sel else A.WHITE)
            app.text(surf, v, r.right - 4, r.y + 1, A.WHITE, 'right')
        help_lines = ['操作', '', 'クリック／Ｅｎｔｅｒ　次へ', '右クリック／Ｅｓｃ　メニュー',
                      'ホイール上／↑　履歴', 'Ｂａｃｋｓｐａｃｅ　一行戻る',
                      'Ｃｔｒｌ　スキップ（押す間）', 'Ｔａｂ　スキップ切替',
                      'Ａ　オート', 'Ｈ　文字を隠す', 'Ｆ５　クイックセーブ', 'Ｆ９　クイックロード',
                      'Ｆ１１　全画面']
        for i, line in enumerate(help_lines):
            app.text(surf, line, 10, 10 + i * 15, A.GOLD if i == 0 else A.WHITE)
        if self._preview_path():
            pr = self._preview_rect()
            A.panel(surf, pr, 220)
            app.text(surf, '見本', pr.x + 4, pr.y + 3, A.GOLD)
        if self.combo is not None:
            self._draw_combo(surf)

    def _draw_combo(self, surf):
        A, app, c = self.A, self.app, self.combo
        cr = self._combo_rect()
        A.panel(surf, cr, 240)
        pygame.draw.rect(surf, A.GOLD, cr, 1)
        items = c['items']
        for vis in range(min(self.COMBO_ROWS, len(items))):
            idx = c['top'] + vis
            r = self._combo_item_rect(vis)
            if idx == c['sel']:
                hl = pygame.Surface(r.size, pygame.SRCALPHA)
                hl.fill((50, 90, 150, 220))
                surf.blit(hl, r.topleft)
            label = items[idx][0]
            if len(label) > 14:
                label = label[:13] + '…'
            current = items[idx][1] == app.hires.path
            col = A.GOLD if idx == c['sel'] else (A.SKY if current else A.WHITE)
            app.text(surf, label, r.x + 2, r.y + 2, col)
        if len(items) > self.COMBO_ROWS:   # scrollbar
            track = pygame.Rect(cr.right - 5, cr.y + 3, 3, cr.h - 6)
            pygame.draw.rect(surf, (60, 70, 90), track)
            th = max(6, track.h * self.COMBO_ROWS // len(items))
            ty = track.y + (track.h - th) * c['top'] // max(1, len(items) - self.COMBO_ROWS)
            pygame.draw.rect(surf, A.SKY, (track.x, ty, 3, th))

    def draw_hires(self, window, r, k):
        """Sample text in the previewed font, at window resolution."""
        path = self._preview_path()
        if not path:
            return
        px = max(6, round(12 * k))
        key = (path, px)
        f = self._preview_fonts.get(key)
        if f is None:
            try:
                f = pygame.font.Font(path, px)
            except (pygame.error, OSError):
                return
            self._preview_fonts[key] = f
        pr = self._preview_rect()
        img = f.render('イリヤの空、ＵＦＯの夏。', True, (255, 255, 255))
        maxw = int((pr.w - 8) * k)
        if img.get_width() > maxw:
            img = img.subsurface((0, 0, maxw, img.get_height()))
        window.blit(img, (round(r.x + (pr.x + 4) * k), round(r.y + (pr.y + 20) * k)))

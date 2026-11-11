"""カードリスト: browse collected keyword cards (both games)."""
import pygame


class CardListState:
    COLS, ROWS = 6, 6
    CELL = 30
    GRID_X, GRID_Y = 6, 30

    def __init__(self, app, back, gid=None):
        from . import app as A
        self.A = A
        self.app = app
        self.back = back
        self.gid = gid if gid in app.games else next(iter(app.games))
        self.sel = 0
        self.page = 0
        self._sync()

    def update(self):
        pass

    # ------------------------------------------------------------ data
    def _sync(self):
        self.game = self.app.games[self.gid]
        self.cards = self.game.cards
        self.owned = self.app.owned_cards(self.gid)
        self.per_page = self.COLS * self.ROWS
        self.pages = max(1, (len(self.cards) + self.per_page - 1) // self.per_page)
        self.sel = min(self.sel, len(self.cards) - 1)
        self.page = self.sel // self.per_page

    def _cell_rect(self, k):
        r, c = divmod(k, self.COLS)
        return pygame.Rect(self.A.RIGHT + self.GRID_X + c * self.CELL, self.GRID_Y + r * self.CELL,
                           self.CELL - 2, self.CELL - 2)

    def _tab_rect(self):
        return pygame.Rect(6, self.A.VIEW_H - 22, self.A.PAGE_W - 12, 17)

    def _arrow_rects(self):
        y = self.GRID_Y + self.ROWS * self.CELL + 2
        R = self.A.RIGHT
        return pygame.Rect(R + 6, y, 40, 18), pygame.Rect(R + self.A.PAGE_W - 46, y, 40, 18)

    # ------------------------------------------------------------ card actions
    def _action(self):
        """(label, what) for the selected card if it starts something, as on the DS."""
        card = self.cards[self.sel]
        if card.index not in self.owned or self.gid != 'iriya1':
            return None
        if card.action >= 200:
            codes = [c for c, _ in self.game.chapters]
            n = card.action - 200
            return ('読む', codes[n]) if n < len(codes) else None
        game = {1: 'cards', 2: 'quiz', 3: 'barcap'}.get(card.action)
        return ('遊ぶ', game) if game else None

    def _action_rect(self):
        return pygame.Rect(self.A.PAGE_W - 40, 117, 36, 17)     # beside the picture

    def _run_action(self):
        act = self._action()
        if act is None:
            return
        app, what = self.app, act[1]
        app.ui_sound('enter')
        if what == 'cards':
            from .minigames.cardbattle import CardBattleState
            app.goto(CardBattleState(app, self))
        elif what == 'quiz':
            from .minigames.quiz import QuizState
            app.goto(QuizState(app, self))
        elif what == 'barcap':
            from .minigames.barcap import BarcapState
            app.goto(BarcapState(app, self))
        else:                               # a chapter; the DS returns to the title after it
            app.start_chapter(self.gid, what, single=True)

    # ------------------------------------------------------------ input
    def _move(self, d):
        self.sel = max(0, min(len(self.cards) - 1, self.sel + d))
        self.page = self.sel // self.per_page
        self.app.ui_sound('move')

    def _switch_game(self):
        others = [g for g in self.app.games if g != self.gid]
        if others:
            self.gid = others[0]
            self.sel = 0
            self._sync()
            self.app.ui_sound('move')

    def event(self, ev):
        A = self.A
        if A.is_back(ev):
            self.app.ui_sound('cancel')
            self.app.goto(self.back)
            return
        if ev.type == pygame.KEYDOWN:
            k = ev.key
            if k in (pygame.K_RIGHT, pygame.K_d):
                self._move(-1)
            elif k in (pygame.K_LEFT, pygame.K_a):
                self._move(1)
            elif k in (pygame.K_DOWN, pygame.K_s):
                self._move(self.COLS)
            elif k in (pygame.K_UP, pygame.K_w):
                self._move(-self.COLS)
            elif k == pygame.K_PAGEDOWN:
                self._move(self.per_page)
            elif k == pygame.K_PAGEUP:
                self._move(-self.per_page)
            elif k == pygame.K_TAB:
                self._switch_game()
            elif k in (pygame.K_RETURN, pygame.K_KP_ENTER, pygame.K_SPACE):
                self._run_action()
        elif ev.type == pygame.MOUSEBUTTONDOWN:
            if ev.button == 4:
                self._move(-self.COLS)
                return
            if ev.button == 5:
                self._move(self.COLS)
                return
            if ev.button != 1:
                return
            pos = ev.lpos
            if self._action() and self._action_rect().collidepoint(pos):
                self._run_action()
                return
            if self._tab_rect().collidepoint(pos):
                self._switch_game()
                return
            prev, nxt = self._arrow_rects()
            if prev.collidepoint(pos) and self.page > 0:
                self.page -= 1
                self.sel = self.page * self.per_page
                self.app.ui_sound('move')
                return
            if nxt.collidepoint(pos) and self.page < self.pages - 1:
                self.page += 1
                self.sel = self.page * self.per_page
                self.app.ui_sound('move')
                return
            for k in range(self.per_page):
                idx = self.page * self.per_page + k
                if idx < len(self.cards) and self._cell_rect(k).collidepoint(pos):
                    self.sel = idx
                    self.app.ui_sound('move')
        elif ev.type == pygame.MOUSEMOTION and pygame.mouse.get_pressed()[0]:
            pass

    # ------------------------------------------------------------ drawing
    def draw(self, surf):
        A, app, g = self.A, self.app, self.game
        surf.fill((14, 22, 46))
        for x0 in (0, A.RIGHT):
            for y in range(0, A.VIEW_H, 4):
                c = 14 + y // 24
                pygame.draw.rect(surf, (c, c + 8, 46 + y // 12), (x0, y, A.PAGE_W, 4))
        A.draw_spine(surf)
        n_owned = len([c for c in self.cards if c.index in self.owned])
        app.text(surf, 'カードリスト', A.RIGHT + 8, 8, A.GOLD, game=g)
        total = len(self.cards)
        if self.gid == 'iriya1':        # 0x2024834: covers from the secret codes count only once owned
            total -= sum(1 for i in range(5, 11) if i not in self.owned)
        app.text(surf, f'{n_owned}/{total}', A.RIGHT + A.PAGE_W - 6, 8, A.WHITE, 'right', game=g)
        for k in range(self.per_page):
            idx = self.page * self.per_page + k
            if idx >= len(self.cards):
                break
            r = self._cell_rect(k)
            card = self.cards[idx]
            if idx in self.owned:
                img = g.card_image(card, 's')
                if img is not None:
                    surf.blit(img, (r.centerx - img.get_width() // 2, r.centery - img.get_height() // 2))
            else:
                pygame.draw.rect(surf, (30, 40, 70), r)
                app.text(surf, '？', r.centerx, r.centery - 6, (110, 120, 150), 'center', game=g)
            pygame.draw.rect(surf, A.GOLD if idx == self.sel else (70, 90, 130), r.inflate(2, 2), 1)
        prev, nxt = self._arrow_rects()
        if self.page > 0:
            app.text(surf, '←前', prev.x + 2, prev.y + 3, A.WHITE, game=g)
        if self.page < self.pages - 1:
            app.text(surf, '次→', nxt.right - 2, nxt.y + 3, A.WHITE, 'right', game=g)
        app.text(surf, f'{self.page + 1}/{self.pages}', A.RIGHT + A.PAGE_W // 2, prev.y + 3, A.GREY, 'center', game=g)
        self._draw_detail(surf)
        tr = self._tab_rect()
        A.panel(surf, tr, 150)
        label = A.GAME_TITLES[self.gid]
        if len(app.games) > 1:
            label = '←→ ' + label
        app.text(surf, label, tr.centerx, tr.y + 3, A.WHITE, 'center', game=g)

    def _draw_detail(self, surf):
        A, app, g = self.A, self.app, self.game
        card = self.cards[self.sel]
        owned = card.index in self.owned
        cats = g.card_categories
        cat = cats[card.category] if card.category < len(cats) else ''
        app.text(surf, f'No.{card.index + 1:02d}　{cat}', 8, 8, A.GREY, game=g)
        act = self._action()
        if act:
            ar = self._action_rect()
            A.panel(surf, ar, 170)
            pygame.draw.rect(surf, A.GOLD, ar, 1)
            app.text(surf, act[0], ar.centerx, ar.y + 3, A.WHITE, 'center', game=g)
        frame = pygame.Rect(0, 0, 108, 108)
        frame.midtop = (A.PAGE_W // 2, 26)
        pygame.draw.rect(surf, (8, 12, 26), frame)
        pygame.draw.rect(surf, A.SKY, frame, 1)
        if owned:
            img = g.card_image(card, '')
            if img is not None:
                surf.blit(img, (frame.centerx - img.get_width() // 2, frame.centery - img.get_height() // 2))
            app.text(surf, card.name, A.PAGE_W // 2, 140, A.GOLD, 'center', game=g)
            y = 160
            for line in card.desc.split('\n'):
                app.text(surf, line, 10, y, A.WHITE, game=g)
                y += 15
        else:
            app.text(surf, '？？？', frame.centerx, frame.centery - 6, (110, 120, 150), 'center', game=g)
            app.text(surf, 'まだ手に入れていません', A.PAGE_W // 2, 140, A.GREY, 'center', game=g)

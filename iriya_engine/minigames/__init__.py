"""Mini-games from the two DS games, played in the DS's normal (landscape) orientation."""
import pygame

TOP_H = 192
GAP = 16
BOTTOM_Y = TOP_H + GAP
SCREEN_W = 256
CANVAS = (SCREEN_W, TOP_H * 2 + GAP)


class MiniState:
    """Base for mini-game screens: two stacked 256x192 DS screens."""

    def __init__(self, app, back):
        from .. import app as A
        self.A = A
        self.app = app
        self.back = back
        self.canvas = pygame.Surface(CANVAS).convert()
        self.top = self.canvas.subsurface((0, 0, SCREEN_W, TOP_H))
        self.bottom = self.canvas.subsurface((0, BOTTOM_Y, SCREEN_W, TOP_H))

    # helpers -----------------------------------------------------------
    def bottom_pos(self, pos):
        """Logical canvas position -> bottom-screen pixel, or None if not on it."""
        x, y = pos
        y -= BOTTOM_Y
        if 0 <= x < SCREEN_W and 0 <= y < TOP_H:
            return int(x), int(y)
        return None

    def top_pos(self, pos):
        x, y = pos
        if 0 <= x < SCREEN_W and 0 <= y < TOP_H:
            return int(x), int(y)
        return None

    def draw_gap(self, surf):
        pygame.draw.rect(surf, (18, 18, 22), (0, TOP_H, SCREEN_W, GAP))

    def sfx(self, sid, game):
        self.app.audio.set_game(game)
        self.app.audio.ui_sound(sid)

    def update(self):
        pass

    def event(self, ev):
        pass

    def draw(self, surf):
        pass

    def leave(self):
        self.app.audio.stop_all()
        self.app.goto(self.back)


class PauseMenu:
    """In-game pause menu shared by the shooters: resume, back to the game's own
    title screen, or leave to the mini-game list."""

    ITEMS = [('つづける', 'resume'), ('タイトルにもどる', 'title'), ('ミニゲーム選択にもどる', 'exit')]
    X, Y, W, PITCH = 40, 56, 176, 30

    def __init__(self):
        self.sel = 0

    def rect(self, i):
        return pygame.Rect(self.X, self.Y + i * self.PITCH, self.W, 24)

    def event(self, ev, bottom_pos):
        """Returns 'resume' / 'title' / 'exit' or None. Clicks are on the bottom screen."""
        if ev.type == pygame.KEYDOWN:
            k = ev.key
            if k in (pygame.K_ESCAPE, pygame.K_p):
                return 'resume'
            if k == pygame.K_q:
                return 'title'
            if k in (pygame.K_UP, pygame.K_w):
                self.sel = (self.sel - 1) % len(self.ITEMS)
            elif k in (pygame.K_DOWN, pygame.K_s):
                self.sel = (self.sel + 1) % len(self.ITEMS)
            elif k in (pygame.K_RETURN, pygame.K_SPACE, pygame.K_z, pygame.K_KP_ENTER):
                return self.ITEMS[self.sel][1]
        elif ev.type in (pygame.MOUSEMOTION, pygame.MOUSEBUTTONDOWN):
            if ev.type == pygame.MOUSEBUTTONDOWN and ev.button == 3:
                return 'resume'
            p = bottom_pos(ev.lpos)
            if p is None:
                return None
            for i in range(len(self.ITEMS)):
                if self.rect(i).collidepoint(p):
                    self.sel = i
                    if ev.type == pygame.MOUSEBUTTONDOWN and ev.button == 1:
                        return self.ITEMS[i][1]
        return None

    def draw(self, bottom, text):
        """text(surf, s, x, y, color, align) draws one line."""
        dim = pygame.Surface(bottom.get_size(), pygame.SRCALPHA)
        dim.fill((0, 0, 20, 150))
        bottom.blit(dim, (0, 0))
        for i, (label, _v) in enumerate(self.ITEMS):
            r = self.rect(i)
            box = pygame.Surface(r.size, pygame.SRCALPHA)
            box.fill((40, 70, 120, 210) if i == self.sel else (10, 18, 40, 190))
            bottom.blit(box, r.topleft)
            pygame.draw.rect(bottom, (255, 214, 120) if i == self.sel else (168, 200, 236), r, 1)
            text(bottom, label, r.centerx, r.y + 6, (255, 214, 120) if i == self.sel else (255, 255, 255), 'center')

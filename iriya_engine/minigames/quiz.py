"""イリヤ電撃クイズ (Game I).

Rebuilt from the ROM data and the quiz code in the ARM9 binary:
  * mini/quiz/quiz.buf holds 50 questions as Shift-JIS strings, each followed
    by four answers; the first answer is the correct one. A full-width space
    marks the line break in a question (17 characters per line).
  * Each question is drawn at random from those not yet cleared, its answers
    are shuffled, and correctly answered questions are remembered across plays
    (status 3 in the save). Clearing all 50 awards the bonus card
    「それ以外のこと」 (card 1), which opens the side story.
  * Bottom screen: the Question panel and four answer bars at y = 100 + 23*i,
    x 22..183 (the original hit boxes). Top screen: Iriya with her speech
    bubble, the passes left and the TIME gauge.
  * A wrong answer or running out of time ends the game; passes skip a question.
"""
import random
import pygame

from . import MiniState, BOTTOM_Y, SCREEN_W

BONUS_CARD = 1
PASSES = 5
TIME_LIMIT = 25 * 60          # frames (the DS counts down from 25:00)
SE_SEIKAI, SE_FUSEIKAI, SE_ENTER, SE_MOVE, SE_CANCEL, SE_CARDGET = 9, 8, 2, 0, 1, 5
ANSWER_BARS = [pygame.Rect(22, 100 + 23 * i - 4, 161, 21) for i in range(4)]
PASS_BTN = pygame.Rect(194, 100, 54, 28)
QUIT_BTN = pygame.Rect(198, 156, 46, 26)

INK = (40, 30, 30)
WHITE = (255, 255, 255)
GOLD = (255, 214, 120)


def load_questions(raw):
    parts = [p.decode('cp932', 'replace') for p in raw.split(b'\0')]
    qs = []
    for i in range(0, len(parts) - 4, 5):
        q = parts[i]
        if not q:
            break
        qs.append((q, parts[i + 1:i + 5]))
    return qs


class QuizState(MiniState):
    def __init__(self, app, back):
        super().__init__(app, back)
        self.game = app.games['iriya1']
        g = self.game
        self.questions = load_questions(g.rom.read('mini/quiz/quiz.buf'))
        img = lambda n, rot=False: g.image(f'mini/quiz/quiz_{n}.nnt', rotate=rot)
        self.backU, self.backD = img('backU'), img('backD')
        self.titleU, self.titleD = img('backtitleU'), img('backtitleD')
        self.faces = [img(f'face{i}') for i in range(1, 6)]
        self.asaba = [img(f'vil{i}') for i in range(1, 4)]
        res = img('res')
        # pieces of quiz_res (upright sheet): ○ × 全問正解 正解 不正解 パス GAME OVER
        self.res = {
            'maru': res.subsurface((0, 0, 64, 62)),
            'batsu': res.subsurface((0, 64, 64, 64)),
            'zenmon': res.subsurface((64, 0, 64, 62)),
            'seikai': res.subsurface((6, 130, 52, 27)),
            'fuseikai': res.subsurface((1, 162, 59, 27)),
            'game': res.subsurface((64, 96, 64, 32)),
            'over': res.subsurface((63, 162, 65, 28)),
        }
        btn = img('button')
        self.btn_pass = (btn.subsurface((0, 0, 54, 28)), btn.subsurface((0, 30, 54, 30)))
        self.btn_quit = (btn.subsurface((0, 62, 46, 26)), btn.subsurface((0, 94, 46, 26)))
        self.cardget = img('cardget')
        self.data = app.minigame.setdefault('quiz', {'cleared': []})
        self.phase = 'title'
        self.t = 0
        self.sel = 0
        self.title_sel = 0
        self._new_question = False
        app.audio.set_game(g)
        app.audio.play_bgm(49, 30)       # BGM_LEMON

    # ------------------------------------------------------------ state
    @property
    def cleared(self):
        return set(self.data['cleared'])

    def start(self):
        self.passes = PASSES
        self.streak = 0
        self.pool = [i for i in range(len(self.questions)) if i not in self.cleared]
        self.replay = not self.pool
        if self.replay:
            self.pool = list(range(len(self.questions)))
        random.shuffle(self.pool)
        self.next_question()

    def next_question(self):
        if not self.pool:
            self.finish_all()
            return
        self.qi = self.pool.pop()
        q, answers = self.questions[self.qi]
        order = [0, 1, 2, 3]
        random.shuffle(order)
        self.answers = [answers[k] for k in order]
        self.correct = order.index(0)
        self.timer = TIME_LIMIT
        self.phase = 'ask'
        self.choice = None
        self.t = 0
        self.face = 0

    def answer(self, i):
        self.choice = i
        self.t = 0
        if i == self.correct:
            self.phase = 'right'
            self.face = 1
            self.streak += 1
            self.sfx(SE_SEIKAI, self.game)
            if self.qi not in self.cleared:
                self.data['cleared'] = sorted(self.cleared | {self.qi})
                self.app.save_progress()
        else:
            self.phase = 'wrong'
            self.face = 3
            self.sfx(SE_FUSEIKAI, self.game)

    def use_pass(self):
        if self.passes <= 0 or self.phase != 'ask':
            return
        self.passes -= 1
        self.pool.insert(0, self.qi)       # it can come back later
        self.phase = 'passed'
        self.t = 0
        self.face = 4
        self.sfx(SE_MOVE, self.game)

    def finish_all(self):
        self.phase = 'allclear'
        self.t = 0
        self.face = 1
        self.got_card = (not self.replay) and self.app.give_card('iriya1', BONUS_CARD)
        if self.got_card:
            self.sfx(SE_CARDGET, self.game)

    # ------------------------------------------------------------ input
    def event(self, ev):
        A = self.A
        click = None
        if ev.type == pygame.MOUSEBUTTONDOWN and ev.button == 1:
            click = self.bottom_pos(ev.lpos)
        if A.is_back(ev) and self.phase in ('title', 'help'):
            self.sfx(SE_CANCEL, self.game)
            if self.phase == 'help':
                self.phase = 'title'
            else:
                self.leave()
            return
        if self.phase == 'title':
            items = 3
            if ev.type == pygame.KEYDOWN:
                if ev.key in (pygame.K_UP, pygame.K_w):
                    self.title_sel = (self.title_sel - 1) % items
                elif ev.key in (pygame.K_DOWN, pygame.K_s):
                    self.title_sel = (self.title_sel + 1) % items
                elif ev.key in (pygame.K_RETURN, pygame.K_SPACE, pygame.K_z):
                    self._title_pick(self.title_sel)
            elif click:
                for i in range(items):
                    if ANSWER_BARS[i + 1].collidepoint(click):
                        self._title_pick(i)
            return
        if self.phase == 'help':
            if click or (ev.type == pygame.KEYDOWN and ev.key in (pygame.K_RETURN, pygame.K_SPACE, pygame.K_z)):
                self.phase = 'title'
            return
        if self.phase == 'ask':
            if ev.type == pygame.KEYDOWN:
                if ev.key in (pygame.K_UP, pygame.K_w):
                    self.sel = (self.sel - 1) % 4
                elif ev.key in (pygame.K_DOWN, pygame.K_s):
                    self.sel = (self.sel + 1) % 4
                elif ev.key in (pygame.K_1, pygame.K_2, pygame.K_3, pygame.K_4):
                    self.answer(ev.key - pygame.K_1)
                elif ev.key in (pygame.K_RETURN, pygame.K_SPACE, pygame.K_z):
                    self.answer(self.sel)
                elif ev.key == pygame.K_p:
                    self.use_pass()
                elif ev.key in (pygame.K_ESCAPE, pygame.K_x):
                    self._quit_run()
            elif ev.type == pygame.MOUSEMOTION:
                p = self.bottom_pos(ev.lpos)
                for i, r in enumerate(ANSWER_BARS):
                    if p and r.collidepoint(p):
                        self.sel = i
            elif click:
                for i, r in enumerate(ANSWER_BARS):
                    if r.collidepoint(click):
                        self.answer(i)
                        return
                if PASS_BTN.collidepoint(click):
                    self.use_pass()
                elif QUIT_BTN.collidepoint(click):
                    self._quit_run()
            elif ev.type == pygame.MOUSEBUTTONDOWN and ev.button == 3:
                self._quit_run()
            return
        # result screens: click / key to continue
        if click or (ev.type == pygame.KEYDOWN and ev.key in (pygame.K_RETURN, pygame.K_SPACE, pygame.K_z)):
            self._advance_result()

    def _title_pick(self, i):
        self.sfx(SE_ENTER, self.game)
        if i == 0:
            self.start()
        elif i == 1:
            self.phase = 'help'
        else:
            self.leave()

    def _quit_run(self):
        self.sfx(SE_CANCEL, self.game)
        self.phase = 'title'

    def _advance_result(self):
        if self.t < 20:
            return
        if self.phase == 'right' or self.phase == 'passed':
            self.next_question()
        elif self.phase == 'wrong':
            self.phase = 'over'
            self.t = 0
        elif self.phase in ('over', 'allclear'):
            self.phase = 'title'

    def update(self):
        self.t += 1
        if self.phase == 'ask':
            self.timer -= 1
            if self.timer <= 0:
                self.answer(-1)
        elif self.phase in ('right', 'passed') and self.t > 75:
            self.next_question()

    # ------------------------------------------------------------ drawing
    def text(self, surf, s, x, y, color=INK, outline=False, align='left'):
        g = self.game
        idxs = g.text_glyphs(s)
        w = 12 * len(idxs)
        if align == 'center':
            x -= w // 2
        for i, gi in enumerate(idxs):
            if gi is None or gi == 614:
                continue
            gl = g.glyph(gi, vertical=False)
            if outline:
                self.A.Scene._outlined(surf, gl, x + 12 * i, y, color, (0, 0, 0))
            else:
                surf.blit(self.A.Scene._tinted(gl, color), (x + 12 * i, y))
        return w

    def bubble(self, lines):
        """Iriya's speech bubble on the top screen (x 124..244, y 70..156)."""
        y = 76 + max(0, (4 - len(lines)) * 8)
        for ln in lines[:5]:
            self.text(self.top, ln, 184, y, INK, align='center')
            y += 16

    def draw(self, surf):
        surf.fill((0, 0, 0))
        self.draw_gap(surf)
        if self.phase in ('title', 'help'):
            self._draw_title()
            return
        top, bot = self.top, self.bottom
        top.blit(self.backU, (0, 0))
        blink = self.phase == 'ask' and self.t % 200 > 192
        top.blit(self.faces[4 if blink else self.face], (0, 58))
        # status: passes left, time gauge, Asaba's reaction
        self.text(top, str(self.passes), 206, 19, WHITE, outline=True)
        frac = max(0.0, self.timer / TIME_LIMIT) if self.phase == 'ask' else (self.timer / TIME_LIMIT)
        pygame.draw.rect(top, (40, 30, 30), (168, 42, 72, 13))
        col = (90, 220, 120) if frac > 0.5 else (240, 200, 60) if frac > 0.25 else (240, 80, 60)
        pygame.draw.rect(top, col, (169, 43, int(70 * frac), 11))
        a = {'right': 1, 'wrong': 2, 'over': 2}.get(self.phase, 0)
        top.blit(self.asaba[a], (10, 14), (4, 6, 48, 32))
        self.text(top, f'{len(self.cleared):02d}', 62, 16, WHITE, outline=True)
        self.text(top, '／50', 62, 31, WHITE, outline=True)
        # bubble
        num = f'第{self.streak + 1}問' if self.phase == 'ask' else ''
        msg = {
            'ask': [num, 'こたえて', 'ね。'],
            'right': ['せいかい！', 'すごい。'],
            'wrong': ['ちがう…。', '', 'こたえは', f'{"①②③④"[self.correct]}'],
            'passed': ['パスね。'],
            'over': ['ゲーム', 'オーバー。', f'{self.streak}問', 'せいかい。'],
            'allclear': ['ぜんぶ', 'せいかい！', 'おめでとう。'],
        }[self.phase]
        self.bubble(msg)
        # bottom: question + answers
        bot.blit(self.backD, (0, 0))
        q, _ = self.questions[self.qi]
        for k, line in enumerate(q.split('　')[:4]):
            self.text(bot, line, 16, 34 + 14 * k, WHITE, outline=True)
        for i, ans in enumerate(self.answers):
            r = ANSWER_BARS[i]
            hl = (self.phase == 'ask' and i == self.sel) or (self.phase != 'ask' and i == self.correct)
            if hl:
                pygame.draw.rect(bot, GOLD if self.phase == 'ask' else (120, 230, 140), r.inflate(2, 0), 1)
            label = '①②③④'[i] + ans
            self.text(bot, label[:13], r.x + 6, r.y + 4, GOLD if hl else WHITE, outline=True)
        bot.blit(self.btn_pass[0 if self.passes else 1], PASS_BTN.topleft)
        bot.blit(self.btn_quit[0], QUIT_BTN.topleft)
        # judgement marks
        if self.phase == 'right':
            bot.blit(self.res['maru'], (186, 10))
            top.blit(self.res['seikai'], (140, 160))
        elif self.phase in ('wrong', 'over'):
            bot.blit(self.res['batsu'], (186, 10))
            top.blit(self.res['fuseikai'], (138, 162))
            if self.phase == 'over':
                bot.blit(self.res['game'], (SCREEN_W // 2 - 64, 64))
                bot.blit(self.res['over'], (SCREEN_W // 2 + 2, 66))
        elif self.phase == 'passed':
            self.text(bot, 'パス', 218, 30, GOLD, outline=True, align='center')
        elif self.phase == 'allclear':
            z = self.res['zenmon']
            bot.blit(z, (SCREEN_W // 2 - z.get_width() // 2, 40))
            if self.got_card:
                bot.blit(self.cardget, (SCREEN_W // 2 - self.cardget.get_width() // 2, 110))
                self.text(bot, '「それ以外のこと」', SCREEN_W // 2, 150, WHITE, outline=True, align='center')

    def _draw_title(self):
        top, bot = self.top, self.bottom
        top.blit(self.titleU, (0, 0))
        top.blit(self.faces[0], (0, 58))
        if self.phase == 'title':
            self.bubble(['イリヤ', '電撃クイズ', f'{len(self.cleared)}/50', 'せいかい'])
        else:
            self.bubble(['４つから', 'えらんでね。', 'まちがえたら', 'おしまい。'])
        bot.blit(self.titleD, (0, 0))
        if self.phase == 'help':
            lines = ['ルール', '', '問題に４択で答えます。',
                     '１問でもまちがえるか、時間切れ',
                     'になるとゲームオーバーです。',
                     f'パスは{PASSES}回まで使えます。',
                     '正解した問題は記録され、全問',
                     '正解するとボーナスカードを',
                     '手に入れられます。']
            for k, l in enumerate(lines):
                self.text(bot, l, 18, 22 + 16 * k, GOLD if k == 0 else WHITE, outline=True)
            return
        for i, label in enumerate(['はじめる', 'あそびかた', 'もどる']):
            r = ANSWER_BARS[i + 1]
            hl = i == self.title_sel
            pygame.draw.rect(bot, (20, 50, 30), r)
            pygame.draw.rect(bot, GOLD if hl else (140, 180, 140), r, 1)
            self.text(bot, label, r.centerx, r.y + 4, GOLD if hl else WHITE, outline=True, align='center')
        self.text(bot, 'イリヤ電撃クイズ', SCREEN_W // 2, 40, GOLD, outline=True, align='center')

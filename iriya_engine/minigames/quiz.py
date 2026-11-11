"""イリヤ電撃クイズ (Game I), following the DS quiz code.

  * mini/quiz/quiz.buf holds 50 questions as Shift-JIS strings, each followed
    by four answers; the first answer is the correct one. A full-width space
    marks a line break in a question (17 characters per line).
  * A run asks the questions not yet answered in it, in random order with the
    answers shuffled. 30 seconds per question (the clock starts when the
    question has finished typing), 5 passes, and a wrong answer or a time-up
    ends the run. Answering all 50 in one run (中断 keeps a run for later)
    awards the bonus card 「それ以外のこと」.
  * Each question remembers whether it was last answered right or wrong, which
    picks Iriya's comment when it comes up again.
  * Sprites are cut from the OBJ sheets at the cell offsets and screen
    positions the DS uses.
"""
import random
import pygame

from . import MiniState, ObjSheet

BONUS_CARD = 1
PASSES = 5
TIME_LIMIT = 30000            # milliseconds, 16 off per frame
SE_ENTER, SE_MOVE, SE_WRONG, SE_RIGHT, SE_PASS, SE_SUSPEND, SE_START, SE_TICK, SE_CARDGET = \
    2, 0, 1, 9, 12, 4, 15, 11, 5
BGM_TITLE, BGM_PLAY = 53, 42

WHITE = (255, 255, 255)
NAVY = (33, 41, 115)
FRAME = (255, 255, 255)

# Iriya's comments: text, face sheet (1..5)
MESSAGES = [
    ('しらない問題。\n浅羽、答えて。', 1),
    ('前やったとき\nまちがえた。\n落ち着いて。', 1),
    ('だいじょうぶ。\nこれは練習済。', 2),
    ('あ、あっ！\nだめだめもう時間\nがない！', 3),
    ('グリーンライト', 1),
    ('ビンゴ！', 2),
    ('へいき。\n練習すれば次は\nできる。', 4),
    ('タイムアップ。', 4),
    ('早く早く！', 1),
    ('ミッション。\n問題は５０。\n浅羽ならきっと\nできる。', 2),
]
HELP_TITLE = '電撃イリヤクイズ'
HELP_TOP = ['■基本ルール', '　４択のクイズが出題されます。正解を', '　タッチしてクイズに答えてください。',
            '　正解すると次の問題に進めます。', '■タイム', '　３０秒以内に解答しないと、タイム',
            '　オーバーになります。']
HELP_BOTTOM = ['■ゲームオーバー', '　不正解を選択するか、タイムオーバーに', '　なるとその時点でゲームオーバーです。']

ANSWER_BOX = [pygame.Rect(0x16, 0x64 + 0x17 * i, 0xa1, 0x17) for i in range(4)]
PASS_BOX = pygame.Rect(0xbc, 0x60, 0x32, 0x19)
SUSPEND_BOX = pygame.Rect(0xc5, 9, 0x26, 0x11)
OK_BOX = pygame.Rect(0x60, 0x90, 0x40, 0x20)
CLEARED_BOX = pygame.Rect(0x60, 0x80, 0x40, 0x20)
TITLE_BOX = [pygame.Rect(0x40, 0x20 + 0x30 * i, 0x80, 0x20) for i in range(3)]
YESNO_BOX = [pygame.Rect(0x58, 0x4c + 0x20 * i, 0x90, 0x18) for i in range(2)]
BACK_BOX = pygame.Rect(0x70, 0xa0, 0x28, 0x11)


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
        self.game = g = app.games['iriya1']
        self.questions = load_questions(g.rom.read('mini/quiz/quiz.buf'))
        img = lambda n: g.image(f'mini/quiz/quiz_{n}.nnt', rotate=False)
        sh = lambda n: ObjSheet(g, f'mini/quiz/quiz_{n}.nnt', expanded=True)
        self.backU, self.backD = img('backU'), img('backD')
        self.titleU, self.titleD = img('backtitleU'), img('backtitleD')
        self.s_button, self.s_res, self.s_moji = sh('button'), sh('res'), sh('moji')
        self.s_title, self.s_title2, self.s_number = sh('title'), sh('title2'), sh('number')
        self.s_select, self.s_cardget = sh('selecthion'), sh('cardget')
        self.s_face = [sh(f'face{i}') for i in range(1, 6)]
        self.s_vil = [sh(f'vil{i}') for i in range(1, 4)]
        self.s_ok = ObjSheet(g, 'mini/card/card_button.nnt')
        n = len(self.questions)
        d = app.minigame.setdefault('quiz', {})
        if 'status' not in d or len(d['status']) != n:
            old = set(d.get('cleared', []))
            d.clear()
            d.update(status=[2 if i in old else 0 for i in range(n)], suspend=False, passes=PASSES)
        self.data = d
        self.frame = 0
        self.cursor = 0
        self.msg = 9
        self.vil = None
        self.timer = TIME_LIMIT
        self.passes = PASSES
        self.count = 0
        app.audio.set_game(g)
        self.enter_title()

    # ------------------------------------------------------------ flow
    def se(self, sid):
        self.sfx(sid, self.game)

    def bgm(self, sid):
        self.app.audio.set_game(self.game)
        self.app.audio.play_bgm(sid, 0)

    def enter_title(self):
        if all(s == 3 for s in self.data['status']):     # the game was closed on the all-clear screen
            self.data['done'] = True
            self.app.give_card('iriya1', BONUS_CARD)     # in case that was before the card came up
        self.msg = 9
        self.vil = None
        self.cursor = 0
        self.t = 0
        if self.data.get('done'):
            self.phase = 'cleared'
        elif self.data['suspend']:
            self.phase = 'resume'
        else:
            self.phase = 'title'
        self.bgm(BGM_TITLE)

    def demote(self):
        """A run is over: the questions answered in it count as practised."""
        self.data['status'] = [2 if s == 3 else s for s in self.data['status']]
        self.data['suspend'] = False
        self.app.save_progress()

    def start(self):
        if self.data['suspend']:
            self.passes = self.data.get('passes', PASSES)
            self.data['suspend'] = False
        else:
            self.passes = PASSES
        self.count = sum(1 for s in self.data['status'] if s == 3)
        self.cursor = 0
        self.se(SE_START)
        self.bgm(BGM_PLAY)
        self.next_question()

    def next_question(self):
        pool = [i for i, s in enumerate(self.data['status']) if s != 3]
        self.qi = random.choice(pool)
        q, answers = self.questions[self.qi]
        order = [0, 1, 2, 3]
        random.shuffle(order)
        self.answers = [answers[k] for k in order]
        self.correct = order.index(0)
        self.qlines = q.split('　')
        self.qlen = sum(len(l) for l in self.qlines) + len(self.qlines) - 1
        self.typed = 0
        self.timer = TIME_LIMIT
        self.phase = 'ask'
        self.pick = None
        self.pass_lit = False
        self.t = 0
        self.msg = {1: 1, 2: 2}.get(self.data['status'][self.qi], 0)
        self.vil = 0

    def judge(self, what):
        """what: answer index, 'pass' or 'time'."""
        self.t = 0
        if what == self.correct:
            self.result = 'right'
            self.msg = 5
            self.se(SE_RIGHT)
            self.data['status'][self.qi] = 3
            self.count += 1
            self.vil = 1
            self.phase = 'mark'
        elif what == 'pass':
            self.passes -= 1
            self.pass_lit = True
            self.result = 'pass'
            self.msg = 4
            self.se(SE_PASS)
            self.phase = 'mark'
        else:
            self.result = 'wrong'
            self.msg = 7 if what == 'time' else 6
            self.se(SE_WRONG)
            self.vil = 2
            self.data['status'][self.qi] = 1
            self.demote()
            self.phase = 'over'

    def suspend(self):
        self.se(SE_SUSPEND)
        self.data['suspend'] = True
        self.data['passes'] = self.passes
        self.app.save_progress()
        self.phase = 'suspended'

    def all_clear(self):
        self.phase = 'allclear'
        self.t = 0
        self.got_card = False
        self.data['suspend'] = False
        self.data['done'] = True                 # also when the game is closed on this screen
        self.app.save_progress()
        self.bgm(BGM_TITLE)

    # ------------------------------------------------------------ input
    def event(self, ev):
        tap = None
        if ev.type == pygame.MOUSEBUTTONDOWN and ev.button == 1:
            tap = self.bottom_pos(ev.lpos)
        key = ev.key if ev.type == pygame.KEYDOWN else None
        up = key in (pygame.K_UP, pygame.K_w)
        down = key in (pygame.K_DOWN, pygame.K_s)
        ok = key in (pygame.K_RETURN, pygame.K_SPACE, pygame.K_z, pygame.K_KP_ENTER)
        cancel = key in (pygame.K_ESCAPE, pygame.K_x, pygame.K_BACKSPACE) or \
            (ev.type == pygame.MOUSEBUTTONDOWN and ev.button == 3)
        ph = self.phase

        def move(n):
            if up or down:
                self.cursor = (self.cursor + (1 if down else -1)) % n
                self.se(SE_MOVE)

        def hit(boxes):
            if tap:
                for i, r in enumerate(boxes):
                    if r.collidepoint(tap):
                        return i
            return None

        if ph == 'title':
            move(3)
            i = hit(TITLE_BOX)
            if i is None and ok:
                i = self.cursor
            if i is None and cancel:
                i = 2
            if i is not None:
                self.cursor = i
                if i == 0:
                    self.start()
                elif i == 1:
                    self.se(SE_ENTER)
                    self.phase = 'help'
                else:
                    self.se(SE_ENTER)
                    self.leave()
        elif ph == 'help':
            if ok or cancel or (tap and BACK_BOX.inflate(8, 8).collidepoint(tap)):
                self.se(SE_ENTER)
                self.phase = 'title'
        elif ph == 'resume':
            move(2)
            i = hit(YESNO_BOX)
            if i is None and ok:
                i = self.cursor
            if i == 0:
                self.start()
            elif i == 1:
                self.se(SE_ENTER)
                self.demote()
                self.enter_title()
        elif ph == 'cleared':
            if ok or cancel or (tap and CLEARED_BOX.collidepoint(tap)):
                self.se(SE_ENTER)
                self.data['status'] = [0] * len(self.questions)
                self.data['suspend'] = self.data['done'] = False
                self.app.save_progress()
                self.enter_title()
        elif ph == 'ask':
            if cancel or (tap and SUSPEND_BOX.collidepoint(tap)):
                self.suspend()
                return
            if self.typed < self.qlen or self.pick is not None:
                return
            move(4)
            i = hit(ANSWER_BOX)
            if i is not None:
                self.cursor = self.pick = i          # shown lit for 15 frames, then judged
                self.t = 0
            elif ok:
                self.judge(self.cursor)
            elif key in (pygame.K_1, pygame.K_2, pygame.K_3, pygame.K_4):
                self.cursor = key - pygame.K_1
                self.judge(self.cursor)
            elif (key == pygame.K_p or (tap and PASS_BOX.collidepoint(tap))) and self.passes > 0:
                self.judge('pass')
        elif ph == 'suspended':
            if ok or cancel or (tap and OK_BOX.collidepoint(tap)):
                self.se(SE_ENTER)
                self.leave()
        elif ph == 'over':
            if self.t >= 49 and (tap or ok or cancel):
                self.enter_title()
        elif ph == 'allclear':
            if self.t >= 0x40 and (tap or ok or cancel):
                self.data['done'] = True         # the next visit starts by resetting everything
                self.demote()
                self.enter_title()

    def update(self):
        self.frame += 1
        self.t += 1
        ph = self.phase
        if ph == 'ask':
            if self.typed < self.qlen:
                self.typed += 1
            elif self.pick is not None:
                if self.t >= 15:
                    self.judge(self.pick)
            else:
                before = self.timer
                self.timer -= 16
                if self.timer < 17:
                    self.timer = 0
                    self.judge('time')
                elif self.timer < 5000:
                    self.msg = 3
                    if before // 1000 != self.timer // 1000:
                        self.se(SE_TICK)
                elif self.timer < 25000:
                    self.msg = 8
        elif ph == 'mark' and self.t >= 31:
            if all(s == 3 for s in self.data['status']):
                self.all_clear()
            else:
                self.next_question()
        elif ph == 'allclear' and self.t == 0x40:
            self.got_card = self.app.give_card('iriya1', BONUS_CARD)
            if self.got_card:
                self.se(SE_CARDGET)

    # ------------------------------------------------------------ drawing
    def text(self, surf, s, x, y, color=WHITE, shadow=None):
        """Full-width characters take 12 pixels, ASCII ones 6 (the half-width font)."""
        g = self.game
        for ch in s:
            o = ord(ch)
            half = o < 0x80 or ch == '･'
            if half:
                gl = g.han_glyph(0xa5 if ch == '･' else o) if ch != ' ' else None
            else:
                gi = g.text_glyphs(ch)[0]
                gl = g.glyph(gi, vertical=False) if gi is not None and gi != 614 else None
            if gl is not None:
                if shadow:
                    sh = self.A.Scene._tinted(gl, shadow)
                    for d in ((1, 0), (0, 1), (1, 1)):
                        surf.blit(sh, (x + d[0], y + d[1]))
                surf.blit(self.A.Scene._tinted(gl, color), (x, y))
            x += 6 if half else 12

    def window(self, surf, r):
        pygame.draw.rect(surf, NAVY, r)
        pygame.draw.rect(surf, FRAME, r, 1)
        pygame.draw.rect(surf, FRAME, r.inflate(-4, -4), 1)
        for ox, sx in ((r.x, 1), (r.right - 1, -1)):
            for oy, sy in ((r.y, 1), (r.bottom - 1, -1)):
                for a in range(2, 6):
                    for b in range(2, 6):
                        surf.set_at((ox + sx * a, oy + sy * b), FRAME)
                for a in range(2, 8):
                    surf.set_at((ox + sx * 7, oy + sy * a), FRAME)
                    surf.set_at((ox + sx * a, oy + sy * 7), FRAME)

    def draw_iriya(self):
        top = self.top
        text, face = MESSAGES[self.msg]
        f = self.s_face[face - 1]
        for k, (x, y) in enumerate(((0, 0x3a), (0x40, 0x3a), (0, 0x7a), (0x40, 0x7a))):
            top.blit(f.cell(k * 0x1000, 64, 64), (x, y))
        for k, line in enumerate(text.split('\n')):
            self.text(top, line, 0x88, 0x50 + 17 * k, NAVY)

    def draw_cursor(self, x, y):
        self.bottom.blit(self.s_button.cell(0x2000, 32, 32), (x, y))

    def draw(self, surf):
        surf.fill((0, 0, 0))
        self.draw_gap(surf)
        top, bot = self.top, self.bottom
        ph = self.phase
        if ph == 'help':
            self.draw_help()
            return
        if ph in ('title', 'resume', 'cleared'):
            top.blit(self.titleU, (0, 0))
            self.draw_iriya()
            v = self.frame % 64
            v = v - 0x40 if v > 0x30 else (0x20 - v if v > 0x10 else v)
            for k in range(4):
                top.blit(self.s_title.cell(k * 0x1000, 64, 64), (k * 0x40, v // 2))
            bot.blit(self.titleD, (0, 0))
            if ph == 'title':
                for i in range(3):
                    y = 0x20 + 0x30 * i
                    bot.blit(self.s_title2.cell(i << 12, 64, 32), (0x40, y))
                    bot.blit(self.s_title2.cell((i << 12) + 0x800, 64, 32), (0x80, y))
                self.draw_cursor(0x28, 0x25 + 0x30 * self.cursor)
            elif ph == 'resume':
                self.window(bot, pygame.Rect(16, 8, 240, 176))
                pygame.draw.line(bot, (24, 24, 24), (16, 184), (254, 184))
                self.text(bot, '中断した時のデータがあります', 0x20, 0x20)
                self.text(bot, '。再開しますか？', 0x20, 0x31)
                self.text(bot, '再開する', 0x70, 0x50)
                self.text(bot, '最初からやり直し', 0x70, 0x70)
                self.draw_cursor(0x58, 0x4d + 0x20 * self.cursor)
            else:
                bot.blit(self.s_res.cell(0x3000, 64, 32), (0x40, 0x48))
                bot.blit(self.s_res.cell(0x3800, 64, 32), (0x80, 0x48))
                bot.blit(self.s_ok.cell(0, 64, 32), CLEARED_BOX.topleft)
            return

        # ---- top screen: question number, passes, time, Iriya
        top.blit(self.backU, (0, 0))
        self.draw_iriya()
        mj = self.s_moji
        top.blit(mj.cell(0, 32, 32), (0x12, 0x10))
        top.blit(mj.cell(0x400, 32, 32), (0x56, 0x10))
        n = self.count + 1
        if ph == 'allclear' or (ph == 'mark' and self.result == 'right'):
            n -= 1
        n, x = max(1, n), 0x41
        while n:
            top.blit(mj.cell(((n % 10) << 10) + 0x1000, 16, 32), (x, 0x11))
            n //= 10
            x -= 16
        top.blit(mj.cell((max(0, self.passes) << 10) + 0x1000, 16, 32), (0xcc, 0x0b))
        cs = max(0, self.timer) // 10
        sec, cc = cs // 100, cs % 100
        if sec >= 10:
            top.blit(mj.cell(((sec // 10) << 10) + 0x1200, 16, 32), (0xad, 0x26))
        top.blit(mj.cell(((sec % 10) << 10) + 0x1200, 16, 32), (0xbd, 0x26))
        top.blit(mj.cell(0x3a00, 16, 32), (0xcd, 0x26))
        top.blit(mj.cell(((cc // 10) << 10) + 0x1200, 16, 32), (0xd9, 0x26))
        top.blit(mj.cell(((cc % 10) << 10) + 0x1200, 16, 32), (0xe9, 0x26))

        # ---- bottom screen
        bot.blit(self.backD, (0, 0))
        if ph == 'suspended':
            self.window(bot, pygame.Rect(8, 8, 240, 176))
            pygame.draw.line(bot, (24, 24, 24), (8, 184), (246, 184))
            self.text(bot, '中断しました', 0x1c, 0x20)
            self.text(bot, '出題の順番が中断前から変更されます', 0x1b, 0x3c)
            bot.blit(self.s_ok.cell(0, 64, 32), OK_BOX.topleft)
            return
        left = self.typed
        for k, line in enumerate(self.qlines):
            self.text(bot, line[:max(0, left)], 0x1a, 0x28 + 17 * k)
            left -= len(line) + 1
        done = self.typed >= self.qlen
        if done:
            for i, ans in enumerate(self.answers):
                if self.pick == i:
                    for k in range(3):
                        bot.blit(self.s_select.cell(i * 0x1800 + k * 0x800, 64, 32), (0x16 + 0x40 * k, 0x60 + 0x17 * i))
                self.text(bot, ans, 0x1e, 0x64 + 0x17 * i)
            self.draw_cursor(6, 0x61 + 0x17 * self.cursor)
        b = self.s_button
        bot.blit(b.cell(0x800 if self.pass_lit else 0, 64, 32), (0xbc, 0x60))
        bot.blit(b.cell(0x1000, 64, 32), (0xc5, 9))
        if self.vil is not None:
            v = self.s_vil[self.vil]
            bot.blit(v.cell(0, 64, 64), (0xbc, 0x7a))
            bot.blit(v.cell(0x1000, 32, 16), (0xbc, 0xba))
            bot.blit(v.cell(0x1200, 32, 16), (0xdc, 0xba))
        r = self.s_res
        if ph == 'ask' and done and self.pick is None and self.timer <= 5999:
            d = self.timer // 1000
            bot.blit(self.s_number.cell(d * 0x1000, 64, 64), (0x60, 0x40 + (self.timer % 1000) // 20))
        elif ph == 'mark':
            y = 0x40 + 29 - self.t
            if self.result == 'right':
                bot.blit(r.cell(0, 64, 64), (0x2a, y))
                bot.blit(r.cell(0x2000, 64, 32), (0x6a, y + 0x1a))
            else:
                bot.blit(r.cell(0x4000, 64, 32), (0x2a, y))
                bot.blit(r.cell(0x5000, 64, 32), (0x6a, y))
        elif ph == 'over':
            y = 0x5d - self.t
            if self.t < 79:
                bot.blit(r.cell(0x1000, 64, 64), (0x2a, y))
                bot.blit(r.cell(0x2800, 64, 32), (0x6a, y + 0x1a))
            if self.t >= 49:
                self.draw_banner(0x4800, 0x5800, self.t - 49)
        elif ph == 'allclear':
            self.draw_banner(0x3000, 0x3800, self.t)
            if self.got_card:
                x = max(100, 261 - 16 * (self.t - 0x40))     # slides in from the right
                for k in range(4):
                    bot.blit(self.s_cardget.cell(k * 0x400, 32, 32), (x + 32 * k, 0x80))

    def draw_banner(self, a, b, t):
        if t < 0x40:
            y = 0x80 + (0x20 - t) * 2
        else:
            m = t % 64
            y = 0x40 + (0x40 - m if m > 0x20 else m)
        self.bottom.blit(self.s_res.cell(a, 64, 32), (0x2a, y))
        self.bottom.blit(self.s_res.cell(b, 64, 32), (0x6a, y))

    def draw_help(self):
        top, bot = self.top, self.bottom
        top.fill((107, 148, 189))
        top.fill((189, 214, 239), (0, 0, 256, 6))
        self.text(top, HELP_TITLE, 0x98 - 10 * len(HELP_TITLE), 8)
        self.window(top, pygame.Rect(0, 24, 256, 178))        # one window across both screens
        self.window(bot, pygame.Rect(0, -10, 256, 202))
        for k, l in enumerate(HELP_TOP):
            self.text(top, l, 0xc, 0x20 + 17 * k)
        for k, l in enumerate(HELP_BOTTOM):
            self.text(bot, l, 0xc, 0x10 + 17 * k)
        pygame.draw.rect(bot, (107, 148, 189), (112, 160, 41, 19))
        pygame.draw.rect(bot, FRAME, (112, 160, 41, 19), 1)
        self.text(bot, '戻る', 120, 163, WHITE, shadow=(24, 24, 24))

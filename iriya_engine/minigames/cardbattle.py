"""電撃カード大戦 (Game I card battle), following the DS code (ARM9 0x02029b00-0x02030400).

Single play only: the DS's 対戦プレイ needs DS wireless and is shown but does nothing.

Rules (from the code and the built-in rule text):
  * Single play takes 40 cards from the player's collection: the cards marked in
    カード選択 first, then random owned battle cards (0x2024d1c). They form one
    shuffled pile; each player is dealt 4 and refills after every play.
  * Every round both players play one card, or a COMBO of 3 cards of one
    attribute; a combo's Pt is 3 x its strongest card.
  * Attribute bonus: 熱血 > 泣き > 萌え > 熱血, but only when exactly two
    attributes are on the field (0x20242f4); the winning cards get +200 Pt.
  * The highest Pt attacks; every other player loses (max Pt - own Pt) HP.
    HP starts at 3000. The match ends when one player is left or the pile
    cannot cover the next plays; places score 10 / 5 / 2 / 0 Pt (0x02059604).

Layout, timings and sprite cells are the DS's (see the addresses below).
"""
import random
import pygame

from . import MiniState, ObjSheet

HP_START = 3000
HAND = 4
DECK_SIZE = 20             # cards a player marks in カード選択
PILE_SIZE = 40             # single play pile (0x2024d1c)
BONUS = 200
RANK_POINTS = (10, 5, 2, 0)
BGM_MENU, BGM_GAME = 53, 50
# engine SE index -> sound id (tables set up by 0x203df30 / 0x203dfd0)
SE = {0: 2, 1: 1, 3: 100, 4: 2, 5: 2, 6: 15, 7: 15, 8: 16, 9: 15, 10: 30, 11: 16, 12: 19}
NAMES = ('PLAYER', 'CPU')

# field slots (0x020595e0): x, y, slide-in direction. Self uses slot 3, the CPU slot 0.
SLOTS = ((80, 4, 0, -1), (32, 4, -1, 0), (176, 4, 1, 0), (128, 16, 0, 1))
PLAYER_SLOT = (3, 0)

MENU_ROWS = ({0: 0, 1: 1, 2: 2, 3: 3},      # はじめる / ルール説明 / カード選択 / やめる
             {0: 4, 1: 5, 3: 3})            # 対戦プレイ / ひとりプレイ / やめる
MENU_X, MENU_Y, MENU_PITCH = 0x40, 0x14, 0x28

BUTTON = pygame.Rect(0xbd, 0x50, 0x40, 0x20)
RESULT_OK = pygame.Rect(0x20, 0x80, 0x40, 0x20)
RESULT_END = pygame.Rect(0xa0, 0x80, 0x40, 0x20)
CONFIRM_YES = pygame.Rect(0x30, 0x70, 0x40, 0x20)
CONFIRM_NO = pygame.Rect(0x90, 0x70, 0x40, 0x20)
BACK_BOX = pygame.Rect(0x70, 0xa0, 0x28, 0x11)

WHITE = (255, 255, 255)
NAVY = (33, 41, 115)
RED = (255, 0, 0)

RULES_TITLE = '電撃カード大戦'
RULES_TOP = ['■ゲームの流れ', '①手札から、各プレイヤーは１枚カードを', '　場に出します。',
             '②属性値ボーナス加算し、各プレイヤーの', '　Ptを決定します。',
             '③Ptがもっとも大きいプレイヤーが攻撃、', '　残りは防御となり、防御プレイヤーの',
             '　体力から以下のPtを引きます。', None, '④体力が０になったプレイヤーは負けです。']
RULES_RED = '攻撃側Pt－自分の防御側Pt'
RULES_BOTTOM = ['■コンボ', '　手札に同じ属性のカードが、3枚以上ある', '　場合、3枚同時に場に出せます。この時の',
                '　Ptは、「一番強いカードの3倍」です。', '■カードの配り方',
                '　２０枚ずつ持ち寄ったカードはひとつの', '　ヤマに集められカードが配られます。',
                '■属性ボーナス', '　萌え、泣き、熱血の3属性はジャンケンの',
                '　３すくみ関係にあります。ジャンケンに', '　勝った属性のカードはその勝負＋200Pt',
                '　されます。']


def winning_attr(kinds):
    """0x20242f4: k beats k+1 when exactly two attributes are present; else None."""
    present = set(kinds)
    if len(present) != 2:
        return None
    for k in range(3):
        if k in present and (k + 1) % 3 in present:
            return k
    return None


def cpu_play(hand):
    """0x20211a0: (indices, combo) the CPU plays from its 4-card hand.

    A combo when three cards share an attribute (leaving out the odd card,
    or the last one when all four match). Otherwise the DS takes the
    strongest card whose attribute differs from the overall strongest card's
    and plays the hand slot numbered by that card's attribute (0x20212e4)."""
    a = [c.kind for c in hand]
    m = ((a[0] == a[1]) * 1 | (a[0] == a[2]) * 2 | (a[0] == a[3]) * 4 |
         (a[1] == a[2]) * 8 | (a[3] == a[1]) * 0x10 | (a[3] == a[2]) * 0x20)
    out = {0x3f: 3, 0x0b: 3, 0x15: 2, 0x26: 1, 0x38: 0}.get(m)
    if out is not None:
        return [i for i in range(4) if i != out], True
    best = max(range(4), key=lambda i: (hand[i].power, -i))
    p, pick = 0, None
    for i in range(4):
        if a[i] != a[best] and hand[i].power > p:
            p, pick = hand[i].power, i
    slot = a[pick] if pick is not None else best
    return [min(slot, 3)], False


class Player:
    def __init__(self, name, row, slot):
        self.name, self.row, self.slot = name, row, slot
        self.points = 0
        self.reset()

    def reset(self):
        self.hp = self.hp_shown = HP_START
        self.hand = [None] * HAND
        self.hand_y = [0x7d] * HAND
        self.alive = True
        self.clear_play()
        self.combo_bar = False

    def clear_play(self):
        self.played = []          # hand indices
        self.cards = []           # the cards
        self.combo = False
        self.pt = 0               # shown Pt of the play
        self.dmg = 0              # shown damage number (<= 0)
        self.dmg_to = 0
        self.dmg_pos = None
        self.fx = self.fy = 0     # field position

    @property
    def base(self):
        x, y, dx, dy = SLOTS[self.slot]
        return (x - 16 if self.combo else x), y


class CardBattleState(MiniState):
    def __init__(self, app, back):
        super().__init__(app, back)
        self.game = g = app.games['iriya1']
        sh = lambda n: ObjSheet(g, f'mini/card/card_{n}.nnt', expanded=True)
        self.topbase = g.image('mini/card/card_topbase.nnt')
        self.btmbase = g.image('mini/card/card_btmbase.nnt')
        self.s_num, self.s_button, self.s_full = sh('num'), sh('button'), sh('fullcolor')
        self.s_back, self.s_stat, self.s_title = sh('back'), sh('stat'), sh('title')
        self.s_mid = [sh('mid'), sh('midB'), sh('midP')]
        self.s_midR = sh('midR')
        self.s_topbg = [sh(f'topbg{i}') for i in range(2)]
        self.s_res = {k: sh('res' + k) for k in 'wl'}
        self._dim = {}
        self.data = app.minigame.setdefault('cardbattle', {'deck': [], 'wins': 0, 'plays': 0})
        self.players = [Player(NAMES[i], i, PLAYER_SLOT[i]) for i in range(2)]
        self.match_no = 1
        self.frame = 0
        self.kbd = None            # keyboard cursor (hand index), None until a key is used
        self.flash = 0
        self.winners = []
        self.t = 0
        app.audio.set_game(g)
        self.enter_menu()

    # ------------------------------------------------------------ helpers
    def se(self, idx):
        sid = SE.get(idx)
        if sid is not None:
            self.sfx(sid, self.game)

    def bgm(self, sid):
        self.app.audio.set_game(self.game)
        self.app.audio.play_bgm(sid, 0)

    def owned_battle(self):
        own = self.app.owned_cards('iriya1')
        return [c for c in self.game.cards if c.is_battle and c.index in own]

    def can_play(self):
        return len(self.owned_battle()) >= DECK_SIZE

    @property
    def me(self):
        return self.players[0]

    # ------------------------------------------------------------ menu
    def enter_menu(self, level=0):
        self.phase = 'menu'
        self.level = level
        self.pressed = [False] * 4
        self.slide_in()
        self.bgm(BGM_MENU)

    def slide_in(self):
        self.mstate, self.mt, self.mlen = 'in', 0, 12

    def menu_x(self, row, a):
        """0x202a6f0: buttons slide in from the right one after another."""
        off = 0x18 * a - 10 * (8 - 2 * row)
        return MENU_X if (self.pressed[row] or off < 0) else off + MENU_X

    def menu_touch(self, row):
        """0x202aa18."""
        if self.level == 0:
            act = row
        else:
            act = {0: 4, 1: 5, 3: 3}.get(row)
        if act is None:
            return
        if act == 4:                     # 対戦プレイ: DS wireless only
            self.se(1)
            return
        if act == 0 and not self.can_play():
            self.se(1)
            return
        self.se(0)
        self.pressed = [False] * 4
        self.pressed[{0: 3, 1: 1, 2: 2, 3: 3, 5: 1}[act]] = True
        self.action = act
        self.mstate, self.mt, self.mlen = 'out', 0, 0x18 if act == 5 else 12

    def menu_done(self):
        act = self.action
        if act == 0:
            self.level = 1
            self.slide_in()
        elif act == 1:
            self.phase = 'rules'
        elif act == 2:
            self.open_deck()
        elif act == 3:
            if self.level == 1:
                self.level = 0
                self.slide_in()
            else:
                self.leave()
        elif act == 5:
            self.match_no = 1
            for p in self.players:
                p.points = 0
            self.start_match()

    # ------------------------------------------------------------ match flow
    def build_pile(self):
        """0x2024d1c: marked cards first, then random owned battle cards (reused
        once all have been taken)."""
        own = {c.index: c for c in self.owned_battle()}
        pile = [own[i] for i in self.data.get('deck', []) if i in own][:PILE_SIZE]
        free = [c for c in own.values() if c not in pile]
        while len(pile) < PILE_SIZE:
            if not free:
                free = list(own.values())
            pile.append(free.pop(random.randrange(len(free))))
        random.shuffle(pile)
        return pile

    def start_match(self):
        self.pile = self.build_pile()
        for p in self.players:
            p.reset()
        for i in range(HAND):
            for p in self.players:
                p.hand[i] = self.pile.pop()
                p.hand_y[i] = 0xc0
        self.unplayed = list(self.pile) + [c for p in self.players for c in p.hand]
        self.data['plays'] = self.data.get('plays', 0) + 1
        self.app.save_progress()
        self.bgm(BGM_GAME)
        self.confirm = False
        self.new_round()

    def new_round(self):
        """0x202c7a8: positions reset, the CPU decides, then the prompt shows for 100 frames."""
        for p in self.players:
            p.clear_play()
        cpu = self.players[1]
        if cpu.alive:
            idx, combo = cpu_play(cpu.hand)
            self.play(cpu, idx, combo)
        self.sel = set()
        self.press_t = None
        self.phase, self.t = 'prompt', 0
        self.se(3)

    def play(self, p, idx, combo):
        p.played = list(idx)
        p.cards = [p.hand[i] for i in idx]
        p.combo = combo
        p.combo_bar = combo
        best = max(c.power for c in p.cards)
        p.pt = best

    def all_played(self):
        return all(p.played for p in self.players if p.alive)

    def start_fight(self):
        for p in self.players:
            for i in p.played:
                p.hand[i] = None
        self.phase, self.t = 'fight', 0          # 0x202cc00: ファイト! for 50 frames
        self.fstep = 0

    def next_step(self):
        """0x202ed3c step machine; each step sets a frame counter."""
        alive = [p for p in self.players if p.alive and p.played]
        while True:
            self.fstep += 1
            s = self.fstep
            self.fc = 0
            if s == 1:                                           # 0x202d61c combo
                if any(p.combo for p in alive):
                    self.fc = 100
                    self.se(6)
            elif s == 2:                                         # 0x202d774 attributes
                w = winning_attr([p.cards[0].kind for p in alive])
                self.winners = [p for p in alive if w is not None and p.cards[0].kind == w]
                if self.winners:
                    self.fc = 0x78
                    self.se(8)
            elif s == 3:                                         # 0x202d9b0
                self.fc = 0x18
                self.max_pt = max(p.pt for p in alive)
            elif s == 4:                                         # 0x202dad0 damage numbers
                frames = 0
                losers = False
                for p in alive:
                    if p.pt < self.max_pt:
                        losers = True
                        p.dmg, p.dmg_to = 0, p.pt - self.max_pt
                        x, y = p.fx, p.fy
                        p.dmg_pos = [x + 0x1a, y + 8]
                        frames = max(frames, (-p.dmg_to + 12) // 13)
                self.badge = 0xc00 if losers else 0x2400
                self.fc = frames + 0x14
                self.se(9)
            elif s == 5:                                         # 0x202dd10 numbers fly up
                self.fc = 0x1e
                for p in alive:
                    if p.dmg_to < 0:
                        p.fly_from = list(p.dmg_pos)
                        p.fly_to = (SLOTS[p.slot][0] + 0x12, -(5 - p.row) * 0x28)
            elif s == 6:                                         # 0x202dddc damage
                frames = 0
                for p in alive:
                    if p.dmg_to < 0:
                        p.dmg_pos = None
                        p.hp = max(0, p.hp + p.dmg_to)
                        frames = max(frames, -p.dmg_to // 37)
                self.fc = frames + 0x28
                self.se(10)
            elif s == 7:                                         # 0x202debc cards leave
                self.fc = 0x3c
            else:
                self.end_round()
                return
            if self.fc > 0:
                return

    def end_round(self):
        """0x202df90 / 0x202fc30."""
        alive = [p for p in self.players if p.alive]
        need = sum(len(p.played) for p in alive)
        if need > len(self.pile):
            self.finish()
            return
        for p in alive:
            for c in p.cards:
                if c in self.unplayed:
                    self.unplayed.remove(c)
            for i in p.played:
                p.hand[i] = self.pile.pop()
                p.hand_y[i] = 0xc0
        for p in alive:
            if p.hp <= 0:
                p.alive = False
        if sum(p.alive for p in self.players) <= 1:
            self.finish()
            return
        self.new_round()

    def finish(self):
        """0x202ead8: places by HP, Pt for the places, then 優勝 / あなたの勝ち etc."""
        for p in self.players:
            rank = sum(1 for q in self.players if q.hp > p.hp)
            p.place = rank
            p.points += RANK_POINTS[rank]
        win = self.me.place == 0
        if win:
            self.data['wins'] = self.data.get('wins', 0) + 1
        self.app.save_progress()
        self.result = 'w' if win else 'l'
        self.phase, self.t = 'result', 0
        self.se(11 if win else 12)

    def ranks(self):
        return [sum(1 for q in self.players if q.points > p.points) + 1 for p in self.players]

    # ------------------------------------------------------------ input
    def hand_rect(self, i):
        return pygame.Rect(0x20 + 0x30 * i, self.me.hand_y[i] - (0xd if i in self.sel else 0), 0x30, 0x40)

    def button_state(self):
        n = len(self.sel)
        return 'combo' if n == 3 else ('ok' if n == 1 else 'dim')

    def tap_card(self, i):
        """0x202cd44: same attribute toggles, another attribute starts a new selection."""
        hand = self.me.hand
        if hand[i] is None:
            return
        if i in self.sel:
            self.sel.discard(i)
        elif self.sel and all(hand[j].kind == hand[i].kind for j in self.sel):
            self.sel.add(i)
        else:
            self.sel = {i}
        self.se(5)

    def press_button(self):
        st = self.button_state()
        if st == 'dim' or self.press_t is not None:
            return
        self.se(7)
        self.press_t = 0

    def event(self, ev):
        tap = None
        if ev.type == pygame.MOUSEBUTTONDOWN and ev.button == 1:
            tap = self.bottom_pos(ev.lpos)
        key = ev.key if ev.type == pygame.KEYDOWN else None
        ok = key in (pygame.K_RETURN, pygame.K_SPACE, pygame.K_z, pygame.K_KP_ENTER)
        cancel = key in (pygame.K_ESCAPE, pygame.K_x, pygame.K_BACKSPACE) or \
            (ev.type == pygame.MOUSEBUTTONDOWN and ev.button == 3)
        ph = self.phase

        if ph == 'menu':
            if self.mstate != 'idle':
                return
            rows = MENU_ROWS[self.level]
            if key in (pygame.K_UP, pygame.K_w, pygame.K_DOWN, pygame.K_s):
                keys = sorted(rows)
                c = self.kbd if self.kbd in keys else keys[0]
                c = keys[(keys.index(c) + (1 if key in (pygame.K_DOWN, pygame.K_s) else -1)) % len(keys)]
                self.kbd = c
            elif ok and self.kbd in rows:
                self.menu_touch(self.kbd)
            elif cancel:
                self.menu_touch(3)
            elif tap:
                x, y = tap
                for row in rows:
                    y0 = MENU_Y + MENU_PITCH * row
                    if MENU_X < x < MENU_X + 0x80 and y0 < y < y0 + 0x20:
                        self.menu_touch(row)
            return
        if ph == 'rules':
            if ok or cancel or (tap and BACK_BOX.inflate(8, 8).collidepoint(tap)):
                self.se(0)
                self.enter_menu()
            return
        if ph == 'deck':
            self.deck_event(ev, tap, key, ok, cancel)
            return
        if ph == 'result':
            if self.t < 30:
                return
            if (tap and RESULT_OK.collidepoint(tap)) or ok:
                self.se(0)
                self.match_no += 1
                self.start_match()
            elif (tap and RESULT_END.collidepoint(tap)) or cancel:
                self.se(0)
                self.enter_menu()
            return
        # ---- match
        if self.confirm:
            if (tap and CONFIRM_YES.collidepoint(tap)) or key in (pygame.K_y,):
                self.se(0)
                self.enter_menu()
            elif (tap and CONFIRM_NO.collidepoint(tap)) or cancel or key == pygame.K_n:
                self.se(1)
                self.confirm = False
            return
        if cancel or key == pygame.K_p:
            self.confirm = True
            return
        if ph != 'choose' or self.press_t is not None or not self.me.alive:
            return
        if key in (pygame.K_LEFT, pygame.K_a, pygame.K_RIGHT, pygame.K_d):
            d = -1 if key in (pygame.K_LEFT, pygame.K_a) else 1
            self.kbd = 0 if self.kbd is None else (self.kbd + d) % HAND
        elif key in (pygame.K_UP, pygame.K_w, pygame.K_DOWN, pygame.K_s) and self.kbd is not None:
            self.tap_card(self.kbd)
        elif ok:
            if not self.sel and self.kbd is not None:
                self.tap_card(self.kbd)
            self.press_button()
        elif tap:
            x, y = tap
            if BUTTON.x < x < BUTTON.right and BUTTON.y < y < BUTTON.bottom:
                self.press_button()
                return
            for i in range(HAND):
                if self.hand_rect(i).collidepoint(tap):
                    self.tap_card(i)
                    return

    # ------------------------------------------------------------ frame
    def update(self):
        self.frame += 1
        self.t += 1
        ph = self.phase
        if ph == 'menu':
            if self.mstate in ('in', 'out'):
                self.mt += 1
                if self.mt >= self.mlen:
                    if self.mstate == 'in':
                        self.mstate = 'idle'
                    else:
                        self.mstate = 'gone'
                        self.menu_done()
            return
        if ph not in ('prompt', 'choose', 'fight'):
            return
        if self.confirm:
            return
        for p in self.players:
            for i in range(HAND):
                if p.hand_y[i] > 0x7d:
                    p.hand_y[i] = max(0x7d, p.hand_y[i] - 4)
        if ph == 'prompt':
            if self.t >= 100:
                self.phase, self.t = 'choose', 0
                if not self.me.alive:
                    self.start_fight()
        elif ph == 'choose':
            if self.press_t is not None:
                self.press_t += 1
                if self.press_t >= 10:
                    self.play(self.me, sorted(self.sel), len(self.sel) == 3)
                    self.sel = set()
                    self.press_t = None
                    if self.all_played():
                        self.start_fight()
        elif ph == 'fight':
            self.update_fight()

    def update_fight(self):
        if self.fstep == 0:
            # 50 frames of ファイト!, then 50 frames of the cards sliding in (0x202ea60)
            t = self.t - 50
            for p in self.players:
                if p.played:
                    x, y = p.base
                    _, _, dx, dy = SLOTS[p.slot]
                    k = 4 * max(0, 0x23 - max(0, t))
                    p.fx, p.fy = x + dx * k, y + dy * k
            if self.t >= 100:
                self.next_step()
            return
        self.fc -= 1
        s, c = self.fstep, self.fc
        alive = [p for p in self.players if p.alive and p.played]
        if s == 1 and c == 0x14:
            for p in alive:
                if p.combo:
                    p.pt *= 3
        elif s == 2 and c == 0x23:
            for p in self.winners:
                p.pt += BONUS
            self.flash = 8
        elif s == 4:
            for p in alive:
                if p.dmg > p.dmg_to:
                    p.dmg = max(p.dmg_to, p.dmg - 13)
        elif s == 5:
            k = (0x1e - c) / 0x1e
            for p in alive:
                if p.dmg_to < 0 and p.dmg_pos is not None:
                    (x0, y0), (x1, y1) = p.fly_from, p.fly_to
                    p.dmg_pos = [x0 + (x1 - x0) * k, y0 + (y1 - y0) * k]
        elif s == 7:
            for p in alive:
                if p.fx > -0x40:
                    p.fx -= 0x1e
        for p in self.players:
            if p.hp_shown > p.hp:
                p.hp_shown = max(p.hp, p.hp_shown - 37)
        if self.flash:
            self.flash -= 1
        if self.fc <= 0:
            self.next_step()

    # ------------------------------------------------------------ drawing helpers
    def number(self, surf, x, y, v, pitch=12, base=0):
        """0x2021c70: right-aligned digits, at most 4; negative values use the red set."""
        if v < 0:
            base += 0x1000
            v = -v
        if v == 0:
            surf.blit(self.s_num.cell(base, 16, 16), (x, y))
            return
        for _ in range(4):
            if v <= 0:
                break
            surf.blit(self.s_num.cell(base + ((v % 10) << 8), 16, 16), (x, y))
            v //= 10
            x -= pitch

    def small(self, surf, x, y, v):
        """0x2021e08: Pt on cards."""
        if v >= 0:
            self.number(surf, x, y, v, 10, 0x2000)

    def dimmed(self, cell):
        s = self._dim.get(id(cell))
        if s is None:
            s = cell.copy()
            s.fill((96, 96, 96), special_flags=pygame.BLEND_RGB_MULT)
            self._dim[id(cell)] = s
        return s

    def half_text(self, surf, s, x, y, pitch=8, color=WHITE):
        """The DS's 8x8 BG text (counts) and name font, drawn with the half-width font."""
        for ch in s:
            if ch != ' ':
                gl = self.game.han_glyph(ord(ch))
                if gl is not None:
                    surf.blit(self.A.Scene._tinted(gl, color), (x, y))
            x += pitch

    def text(self, surf, s, x, y, color=WHITE, shadow=None):
        """Full-width characters take 12 pixels, ASCII ones 6."""
        g = self.game
        for ch in s:
            half = ord(ch) < 0x80
            if half:
                gl = g.han_glyph(ord(ch)) if ch != ' ' else None
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
        pygame.draw.rect(surf, WHITE, r, 1)
        pygame.draw.rect(surf, WHITE, r.inflate(-4, -4), 1)
        for ox, sx in ((r.x, 1), (r.right - 1, -1)):
            for oy, sy in ((r.y, 1), (r.bottom - 1, -1)):
                for a in range(2, 6):
                    for b in range(2, 6):
                        surf.set_at((ox + sx * a, oy + sy * b), WHITE)
                for a in range(2, 8):
                    surf.set_at((ox + sx * 7, oy + sy * a), WHITE)
                    surf.set_at((ox + sx * a, oy + sy * 7), WHITE)

    def card(self, surf, cards, combo, pt, x, y):
        """0x202e268: frame, art, Pt."""
        best = max(cards, key=lambda c: c.power)
        surf.blit(self.s_mid[best.kind % 3].cell(0x1000 if combo else 0, 64, 64), (x, y))
        art = self.art(best)
        if art is not None:
            surf.blit(art, (x + 4, y + 4))
        self.small(surf, x + 0x26, y + 0x30, pt)

    def art(self, c):
        p = f'card/{c.image}b.nnt'
        if p in self.game.rom.files:
            return self.game.image(p, rotate=False)
        return None

    # ------------------------------------------------------------ drawing
    def draw(self, surf):
        surf.fill((0, 0, 0))
        self.draw_gap(surf)
        top, bot = self.top, self.bottom
        ph = self.phase
        if ph == 'rules':
            self.draw_rules()
            return
        top.blit(self.topbase, (0, 0))
        bot.blit(self.btmbase, (0, 0))
        if ph == 'menu':
            self.draw_menu()
        elif ph == 'deck':
            self.draw_deck()
        else:
            self.draw_hud()
            if ph == 'result':
                self.draw_result()
            else:
                self.draw_field()
                if self.confirm:
                    self.draw_confirm()

    def draw_menu(self):
        bot = self.bottom
        if self.mstate == 'in':
            a = 12 - self.mt
        elif self.mstate == 'out':
            a = self.mt
        elif self.mstate == 'gone':
            a = 99
        else:
            a = 0
        for row, cell in MENU_ROWS[self.level].items():
            x = self.menu_x(row, a)
            if x >= 256:
                continue
            y = MENU_Y + MENU_PITCH * row
            bot.blit(self.s_title.cell(cell << 12, 64, 32), (x, y))
            bot.blit(self.s_title.cell((cell << 12) + 0x800, 64, 32), (x + 0x40, y))
            if self.kbd == row and self.mstate == 'idle':
                pygame.draw.rect(bot, (255, 214, 120), (x, y, 0x80, 0x20), 1)

    def draw_rules(self):
        top, bot = self.top, self.bottom
        top.fill((107, 148, 189))
        top.fill((189, 214, 239), (0, 0, 256, 6))
        self.text(top, RULES_TITLE, 96, 8)
        self.window(top, pygame.Rect(0, 24, 256, 178))
        self.window(bot, pygame.Rect(0, -10, 256, 202))
        for k, l in enumerate(RULES_TOP):
            if l is None:
                self.text(top, RULES_RED, 45, 37 + 14 * k, RED)
            else:
                self.text(top, l, 11, 37 + 14 * k)
        for k, l in enumerate(RULES_BOTTOM):
            self.text(bot, l, 11, 5 + 14 * k)
        pygame.draw.rect(bot, (107, 148, 189), (112, 160, 41, 19))
        pygame.draw.rect(bot, WHITE, (112, 160, 41, 19), 1)
        self.text(bot, '戻る', 120, 163, WHITE, shadow=(24, 24, 24))

    def draw_hud(self):
        """0x202c2f8."""
        top = self.top
        n = self.s_num
        top.blit(n.cell(0x1c00, 16, 16), (8, 8))
        self.number(top, 0x28, 8, min(self.match_no, 9))
        top.blit(n.cell(0x1d00, 16, 16), (0x38, 8))
        top.blit(n.cell(0x1e00, 16, 16), (0x48, 8))
        if self.phase != 'result':
            counts = [len(self.pile)] + [sum(1 for c in self.unplayed if c.kind == k) for k in range(3)]
            for i in range(3):
                top.blit(self.s_full.cell(0x700 + 0x100 * i, 16, 16), (0x78 + 0x28 * i, 8))
            for i, v in enumerate(counts):
                self.half_text(top, '%2d' % v, 0x60 + 0x28 * i, 0x0e)
        ranks = self.ranks()
        for p in self.players:
            i = p.row
            y = 0x20 + 0x28 * i
            for k in range(4):
                top.blit(self.s_topbg[i].cell(k * 0x800, 64, 32), (0x40 * k, y))
            self.half_text(top, p.name, 0x0e, 0x25 + 0x28 * i, 12, (0, 0, 0))
            yy = 0x30 + 0x28 * i
            self.number(top, 0x18, yy, ranks[i])
            top.blit(n.cell(0xa00, 16, 16), (0x28, yy))
            top.blit(n.cell(0xc00, 16, 16), (0x44, yy))
            self.number(top, 0x5c, yy, p.points)
            top.blit(n.cell(0xb00, 16, 16), (0x68, yy))
            top.blit(n.cell(0xd00, 16, 16), (0x74, yy))
            for k in range(4):
                top.blit(self.s_stat.cell(0x400, 16, 16), (0x96 + 0xc * k, 0x28 + 0x28 * i))
            self.number(top, 0xed, 0x28 + 0x28 * i, max(0, p.hp_shown))

    def draw_field(self):
        bot = self.bottom
        ph = self.phase
        # slot bars
        for p in self.players:
            x, y, _, _ = SLOTS[p.slot]
            w = 64 if p.combo_bar else 48
            if p.combo_bar:
                x -= 16
            fill, edge = ((165, 239, 148), (57, 181, 41)) if p.row == 0 else ((231, 247, 140), (165, 198, 33))
            pygame.draw.rect(bot, fill, (x, y + 0x40, w, 8))
            pygame.draw.line(bot, edge, (x, y + 0x40), (x + w - 1, y + 0x40))
            pygame.draw.line(bot, edge, (x, y + 0x47), (x + w - 1, y + 0x47))
        # played cards
        for p in self.players:
            if not p.played:
                continue
            if ph in ('prompt', 'choose') or (ph == 'fight' and self.fstep == 0 and self.t < 50):
                x, y = p.base
                if p is self.me:
                    self.card(bot, p.cards, p.combo, p.pt, x, y)
                elif ph == 'choose' or ph == 'fight':
                    for k in range(3 if p.combo else 1)[::-1]:
                        bot.blit(self.s_back.cell(0, 64, 64), (x + 8 * k, y))
                continue
            self.card(bot, p.cards, p.combo, p.pt, int(p.fx), int(p.fy))
        if ph == 'fight' and self.fstep:
            self.draw_fight_fx()
        # hand
        me = self.me
        for i in range(HAND):
            c = me.hand[i]
            if c is None or (i in self.sel and self.press_t is not None and self.press_t >= 10):
                continue
            x = 0x20 + 0x30 * i
            y = 0x70 if i in self.sel else me.hand_y[i]
            self.card(bot, [c], False, c.power, x, y)
            if self.kbd == i and ph == 'choose':
                pygame.draw.rect(bot, (255, 214, 120), (x, y, 48, 64), 1)
        # button (0x202cf90)
        st = self.button_state()
        if st == 'combo':
            bot.blit(self.s_button.cell(0x800, 64, 32), BUTTON.topleft)
        elif st == 'ok' and ph == 'choose':
            bot.blit(self.s_button.cell(0, 64, 32), BUTTON.topleft)
        else:
            bot.blit(self.dimmed(self.s_button.cell(0, 64, 32)), BUTTON.topleft)
        # banners (0x202c1c4)
        if ph == 'prompt':
            r = 12 - self.t
            x = 0x60 + 4 * r if r > 0 else 0x60
            bot.blit(self.s_num.cell(0x3800, 64, 32), (x - 0x40, 0x30))
            bot.blit(self.s_num.cell(0x4000, 64, 32), (x, 0x30))
            bot.blit(self.s_num.cell(0x4800, 32, 32), (x + 0x40, 0x30))
        elif ph == 'fight' and self.fstep == 0 and self.t < 50:
            bot.blit(self.s_full.cell(0x1800, 32, 32), (0x50, 0x20))
            bot.blit(self.s_full.cell(0x2000, 32, 32), (0x90, 0x20))
            bot.blit(self.s_num.cell(0x3000, 64, 32), (0x60, 0x20))
            if self.t < 12:
                fl = pygame.Surface((256, 192))
                fl.fill(WHITE)
                fl.set_alpha(int(255 * (12 - self.t) / 12))
                bot.blit(fl, (0, 0))

    def draw_fight_fx(self):
        bot, top = self.bottom, self.top
        s, c = self.fstep, self.fc
        alive = [p for p in self.players if p.alive and p.played]
        if s == 1:                                    # 0x202e19c コンボ発生 band
            for k in range(8):
                bot.blit(self.s_full.cell(0, 32, 32), (0x20 * k, 0x50))
            x = 12 * ((c - 0x14) - 0x28) + 0x40
            if x < 0x2c:
                x = 0x40
            bot.blit(self.s_full.cell(0x2800, 64, 32), (x, 0x50))
            bot.blit(self.s_full.cell(0x3000, 64, 32), (x + 0x40, 0x50))
        elif s == 2:                                  # flames / drops / hearts round the winners
            for p in self.winners:
                cx, cy = p.fx + 24, p.fy + 32
                icon = self.s_full.cell(0x400 + 0x100 * p.cards[0].kind, 16, 16)
                if c > 0x23:
                    import math
                    r = 12 + (c - 0x23) * 0.8
                    for k in range(8):
                        ang = (k / 8 + self.frame / 90) * 2 * math.pi
                        bot.blit(icon, (cx - 8 + r * math.cos(ang), cy - 8 + r * math.sin(ang)))
                else:
                    bot.blit(self.s_midR.cell(0x1000 if p.combo else 0, 64, 64), (p.fx, p.fy),
                             special_flags=pygame.BLEND_RGB_ADD) if c % 8 < 4 else None
            if self.flash:
                fl = pygame.Surface((256, 192))
                fl.fill(WHITE)
                fl.set_alpha(self.flash * 30)
                bot.blit(fl, (0, 0))
        elif s == 3:                                  # losers flash
            if c % 8 < 2:
                for p in alive:
                    if p.pt < self.max_pt:
                        cell = self.s_midR.cell(0x1000 if p.combo else 0, 64, 64)
                        bot.blit(cell, (p.fx, p.fy), special_flags=pygame.BLEND_RGB_ADD)
        if s == 4:
            for p in alive:
                if p.pt == self.max_pt:
                    bot.blit(self.s_full.cell(self.badge, 32, 32), (p.fx, p.fy))
        if s in (4, 5):                               # 0x202e53c damage numbers
            for p in alive:
                if p.dmg_to < 0 and p.dmg_pos is not None:
                    x, y = int(p.dmg_pos[0]), int(p.dmg_pos[1])
                    if y > -0x20:
                        self.number(bot, x, y, p.dmg)
                    else:
                        self.number(top, x, y + 0xe0, p.dmg)

    def draw_result(self):
        bot = self.bottom
        f = self.t % 0xa0
        k = (f // 5) % 16
        y = 0x10 + (2 * k if k > 8 else 2 * (16 - k))
        res = self.s_res[self.result]
        bot.blit(res.cell(0, 64, 64), (0x40, y))
        bot.blit(res.cell(0x1000, 64, 64), (0x80, y))
        bot.blit(self.s_button.cell(0, 64, 32), RESULT_OK.topleft)
        bot.blit(self.s_button.cell(0x1800, 64, 32), RESULT_END.topleft)

    def draw_confirm(self):
        bot = self.bottom
        dim = pygame.Surface((256, 192), pygame.SRCALPHA)
        dim.fill((0, 0, 0, 120))
        bot.blit(dim, (0, 0))
        msg = 'カードゲームを中断しますか？'
        self.text(bot, msg, 128 - 6 * len(msg), 0x40, WHITE, shadow=(16, 16, 16))
        bot.blit(self.s_button.cell(0, 64, 32), CONFIRM_YES.topleft)
        bot.blit(self.s_button.cell(0x1000, 64, 32), CONFIRM_NO.topleft)

    # ------------------------------------------------------------ カード選択
    # The DS opens its card list here; marked cards go into the pile first.
    DECK_COLS, DECK_ROWS = 5, 3

    def open_deck(self):
        self.phase = 'deck'
        self.deck_cards = sorted(self.owned_battle(), key=lambda c: (c.kind, -c.power))
        own = {c.index for c in self.deck_cards}
        self.deck_sel = [i for i in self.data.get('deck', []) if i in own][:DECK_SIZE]
        self.deck_cursor = 0
        self.deck_scroll = 0

    def deck_rect(self, k):
        r, c = divmod(k, self.DECK_COLS)
        return pygame.Rect(8 + c * 48, 4 + r * 58, 48, 56)

    def deck_event(self, ev, tap, key, ok, cancel):
        n = len(self.deck_cards)
        per = self.DECK_COLS * self.DECK_ROWS
        if cancel:
            self.save_deck()
            return
        if key in (pygame.K_LEFT, pygame.K_a):
            self.deck_cursor = max(0, self.deck_cursor - 1)
        elif key in (pygame.K_RIGHT, pygame.K_d):
            self.deck_cursor = max(0, min(n - 1, self.deck_cursor + 1))
        elif key in (pygame.K_UP, pygame.K_w):
            self.deck_cursor = max(0, self.deck_cursor - self.DECK_COLS)
        elif key in (pygame.K_DOWN, pygame.K_s):
            self.deck_cursor = max(0, min(n - 1, self.deck_cursor + self.DECK_COLS))
        elif ok:
            self.toggle_deck(self.deck_cursor)
        elif ev.type == pygame.MOUSEBUTTONDOWN and ev.button in (4, 5):
            self.deck_scroll = max(0, min(self.deck_scroll + (-1 if ev.button == 4 else 1),
                                          max(0, (n - 1) // self.DECK_COLS - self.DECK_ROWS + 1)))
            return
        elif tap:
            if pygame.Rect(0xbd, 0xa0, 0x40, 0x20).collidepoint(tap):
                self.save_deck()
                return
            if pygame.Rect(0x08, 0xa8, 0x18, 0x14).collidepoint(tap):
                self.deck_scroll = max(0, self.deck_scroll - self.DECK_ROWS)
                return
            if pygame.Rect(0x28, 0xa8, 0x18, 0x14).collidepoint(tap):
                last = max(0, (n - 1) // self.DECK_COLS - self.DECK_ROWS + 1)
                self.deck_scroll = min(last, self.deck_scroll + self.DECK_ROWS)
                return
            for k in range(per):
                idx = self.deck_scroll * self.DECK_COLS + k
                if idx < n and self.deck_rect(k).collidepoint(tap):
                    self.deck_cursor = idx
                    self.toggle_deck(idx)
            return
        row = self.deck_cursor // self.DECK_COLS
        if row < self.deck_scroll:
            self.deck_scroll = row
        elif row >= self.deck_scroll + self.DECK_ROWS:
            self.deck_scroll = row - self.DECK_ROWS + 1

    def toggle_deck(self, idx):
        if not 0 <= idx < len(self.deck_cards):          # no battle cards to choose from
            return
        c = self.deck_cards[idx]
        if c.index in self.deck_sel:
            self.deck_sel.remove(c.index)
        elif len(self.deck_sel) < DECK_SIZE:
            self.deck_sel.append(c.index)
        else:
            self.se(1)
            return
        self.se(5)

    def save_deck(self):
        self.data['deck'] = list(self.deck_sel)
        self.app.save_progress()
        self.se(0)
        self.enter_menu()

    def draw_deck(self):
        top, bot = self.top, self.bottom
        n = len(self.deck_cards)
        cur = self.deck_cards[self.deck_cursor] if n else None
        if cur is not None:
            self.card(top, [cur], False, cur.power, 0x18, 0x60)
            self.text(top, cur.name[:14], 0x50, 0x68, WHITE, shadow=(16, 16, 16))
        msg = f'選択 {len(self.deck_sel)}/{DECK_SIZE}'
        self.text(top, msg, 0x50, 0x88, WHITE, shadow=(16, 16, 16))
        per = self.DECK_COLS * self.DECK_ROWS
        for k in range(per):
            idx = self.deck_scroll * self.DECK_COLS + k
            if idx >= n:
                break
            c = self.deck_cards[idx]
            r = self.deck_rect(k)
            self.card(bot, [c], False, c.power, r.x, r.y - 6)
            if c.index in self.deck_sel:
                tag = self.s_full.cell(0x700 + 0x100 * c.kind, 16, 16)
                bot.blit(tag, (r.right - 14, r.y - 6))
                pygame.draw.rect(bot, (255, 214, 120), (r.x, r.y - 6, 48, 64), 2)
            if idx == self.deck_cursor:
                pygame.draw.rect(bot, WHITE, (r.x - 1, r.y - 7, 50, 66), 1)
        pygame.draw.polygon(bot, WHITE, [(0x1c, 0xab), (0x1c, 0xb9), (0x10, 0xb2)])
        pygame.draw.polygon(bot, WHITE, [(0x2c, 0xab), (0x2c, 0xb9), (0x38, 0xb2)])
        bot.blit(self.s_button.cell(0, 64, 32), (0xbd, 0xa0))

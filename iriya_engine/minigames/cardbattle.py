"""電撃カード大戦 (Game I card battle).

Rules, from the game's ARM9 code and its built-in rule text:
  * Each player brings 20 battle cards; all are shuffled into one pile and
    every player is dealt a hand of 4 (dealt again after each play).
  * Each round every player puts one card on the field.
  * COMBO: with 3 or more cards of one attribute in hand, a player may play 3
    of them at once; their Pt is 3 x the strongest of the three (0x202d69c).
  * Attribute bonus: 熱血 / 泣き / 萌え are rock-paper-scissors. Attribute k
    beats k+1 (mod 3), but only when exactly two attributes are on the field
    (0x20242f4). Cards of the winning attribute get +200 Pt (0x202d868).
  * The highest Pt attacks; everyone else loses (attacker Pt - own Pt) HP
    (0x202dad0, 0x203e7f8). HP starts at 3000 (0x202bf4c). 0 HP is out.
  * The last player standing wins; others are ranked by when they dropped
    out (優勝 / 準優勝 / 3位 / 4位).
"""
import random
import pygame

from . import MiniState, SCREEN_W, TOP_H
from ..cards import ATTR_NAMES

HP_START = 3000
HAND = 4
DECK_SIZE = 20
BONUS = 200
SE_MOVE, SE_CANCEL, SE_ENTER = 0, 1, 2
SE_COMBO_ENTER, SE_COMBO_SELECT = 6, 7
SE_HIT = (15, 16, 17, 18)
WHITE = (255, 255, 255)
GOLD = (255, 214, 120)
ATTR_COL = ((235, 70, 60), (70, 130, 240), (90, 200, 90))

CARD_W, CARD_H = 48, 64
HAND_X0, HAND_Y = 14, 36
BTN_OK = pygame.Rect(126, 150, 60, 28)
BTN_COMBO = pygame.Rect(190, 150, 60, 28)
BTN_END = pygame.Rect(204, 6, 48, 18)


class Player:
    def __init__(self, name, cpu, deck):
        self.name, self.cpu = name, cpu
        self.deck = deck
        self.hp = HP_START
        self.shown_hp = HP_START
        self.hand = []
        self.alive = True
        self.out_round = None
        self.played = []
        self.pt = 0
        self.combo = False
        self.damage = 0


def combo_attrs(hand):
    counts = {}
    for c in hand:
        counts[c.kind] = counts.get(c.kind, 0) + 1
    return [k for k, n in counts.items() if n >= 3]


def winning_attr(kinds):
    """0x20242f4: k beats k+1 when exactly two attributes are present; else None."""
    present = set(kinds)
    for k in range(3):
        if k in present and (k + 1) % 3 in present and (k + 2) % 3 not in present:
            return k
    return None


class CardBattleState(MiniState):
    def __init__(self, app, back):
        super().__init__(app, back)
        self.game = g = app.games['iriya1']
        img = lambda n: g.image(f'mini/card/card_{n}.nnt')
        self.topbase, self.btmbase = img('topbase'), img('btmbase')
        self.bars = [img(f'topbg{i}') for i in range(4)]
        self.frames = [img('mid').subsurface((0, 0, CARD_W, CARD_H)),
                       img('midB').subsurface((0, 0, CARD_W, CARD_H)),
                       img('midP').subsurface((0, 0, CARD_W, CARD_H))]
        self.back_img = img('back')
        btn = img('button')
        self.btn = {'ok': btn.subsurface((0, 0, 64, 32)), 'cancel': btn.subsurface((64, 0, 64, 32)),
                    'combo': btn.subsurface((0, 32, 64, 32)), 'end': btn.subsurface((64, 32, 64, 32))}
        self.res = {r: img(f'res{r}') for r in ('1', '2', '3', '4', 'w', 'l')}
        self.total = img('total')
        self.data = app.minigame.setdefault('cardbattle', {'deck': [], 'wins': 0, 'plays': 0})
        self.phase = 'menu'
        self.menu_sel = 0
        self.t = 0
        self.msg = ''
        self.n_players = 4
        app.audio.set_game(g)
        app.audio.play_bgm(44, 30)        # BGM_TOUSOU

    # ------------------------------------------------------------ cards
    def owned_battle(self):
        own = self.app.owned_cards('iriya1')
        return [c for c in self.game.cards if c.is_battle and c.index in own]

    def all_battle(self):
        return [c for c in self.game.cards if c.is_battle]

    def player_deck(self):
        own = {c.index: c for c in self.owned_battle()}
        deck = [own[i] for i in self.data['deck'] if i in own]
        if len(deck) < DECK_SIZE:   # fill with the strongest owned cards
            rest = sorted((c for c in own.values() if c not in deck), key=lambda c: -c.power)
            deck += rest[:DECK_SIZE - len(deck)]
        return deck[:DECK_SIZE]

    def can_play(self):
        return len(self.owned_battle()) >= DECK_SIZE

    def card_art(self, card, size='b'):
        return self.game.card_image(card, size)

    def draw_card(self, surf, card, x, y, hl=False, dim=False, show_power=True):
        surf.blit(self.frames[card.kind % 3], (x, y))
        art = self.card_art(card, 'b')
        if art is not None:
            surf.blit(art, (x + 4, y + 4), (0, 0, 40, 40))
        if show_power:
            self.text(surf, str(card.power), x + CARD_W // 2, y + 46, WHITE, True, 'center', half=True)
        if dim:
            sh = pygame.Surface((CARD_W, CARD_H), pygame.SRCALPHA)
            sh.fill((0, 0, 0, 140))
            surf.blit(sh, (x, y))
        if hl:
            pygame.draw.rect(surf, GOLD, (x - 1, y - 1, CARD_W + 2, CARD_H + 2), 2)

    # ------------------------------------------------------------ match
    def start_match(self):
        names = ['あなた', 'ＣＯＭ１', 'ＣＯＭ２', 'ＣＯＭ３'][:self.n_players]
        pool = self.all_battle()
        self.players = [Player(names[0], False, self.player_deck())]
        for n in names[1:]:
            self.players.append(Player(n, True, random.sample(pool, DECK_SIZE)))
        self.pile = [c for p in self.players for c in p.deck]
        random.shuffle(self.pile)
        self.discard = []
        for p in self.players:
            self.deal(p)
        self.round = 0
        self.out_order = []
        self.sel = 0
        self.combo_mode = False
        self.combo_pick = []
        self.data['plays'] = self.data.get('plays', 0) + 1
        self.new_round()

    def deal(self, p):
        while len(p.hand) < HAND:
            if not self.pile:
                self.pile, self.discard = self.discard, []
                random.shuffle(self.pile)
                if not self.pile:
                    return
            p.hand.append(self.pile.pop())

    def new_round(self):
        self.round += 1
        for p in self.players:
            p.played, p.pt, p.combo, p.damage = [], 0, False, 0
        self.phase = 'choose'
        self.t = 0
        self.msg = 'カードを出して下さい'
        self.sel = min(self.sel, len(self.me.hand) - 1)
        self.combo_mode = False
        self.combo_pick = []

    @property
    def me(self):
        return self.players[0]

    def cpu_choose(self, p):
        hand = p.hand
        combos = combo_attrs(hand)
        best_single = max(hand, key=lambda c: c.power)
        if combos:
            k = combos[0]
            cards = sorted([c for c in hand if c.kind == k], key=lambda c: -c.power)[:3]
            if cards[0].power * 3 > best_single.power + BONUS or random.random() < 0.7:
                return cards, True
        weights = [c.power + random.randint(-150, 150) for c in hand]
        return [hand[weights.index(max(weights))]], False

    def commit(self, cards, combo):
        """The player has chosen; CPUs choose and the field is resolved over time."""
        self.me.played, self.me.combo = list(cards), combo
        for p in self.players[1:]:
            if p.alive:
                p.played, p.combo = self.cpu_choose(p)
        for p in self.players:
            if not p.alive:
                continue
            for c in p.played:
                p.hand.remove(c)
            self.discard.extend(p.played)
            p.pt = max(c.power for c in p.played) * (3 if p.combo else 1)
        kinds = [p.played[0].kind for p in self.players if p.alive]
        self.win_attr = winning_attr(kinds)
        self.phase = 'reveal'
        self.t = 0
        self.msg = 'ファイト！'
        self.sfx(SE_COMBO_ENTER if any(p.combo for p in self.players) else SE_ENTER, self.game)

    def resolve_step(self):
        """Timed sequence like the DS: combo x3 (20), attribute bonus (35), attack (60)."""
        t = self.t
        alive = [p for p in self.players if p.alive]
        if t == 20 and any(p.combo for p in alive):
            self.msg = 'コンボ発生！'
            self.sfx(SE_COMBO_SELECT, self.game)
        if t == 35:
            if self.win_attr is not None:
                for p in alive:
                    if p.played[0].kind == self.win_attr:
                        p.pt += BONUS
                self.msg = f'{ATTR_NAMES[self.win_attr]}の勝ち！　＋{BONUS}'
            else:
                self.msg = '属性はあいこ'
        if t == 60:
            top = max(p.pt for p in alive)
            self.attacker = [p for p in alive if p.pt == top]
            for p in alive:
                p.damage = top - p.pt
                p.hp -= p.damage
            self.msg = '、'.join(p.name for p in self.attacker) + 'の攻撃！'
            self.sfx(random.choice(SE_HIT), self.game)
        if t > 60:
            for p in self.players:
                if p.shown_hp > p.hp:
                    p.shown_hp = max(p.hp, p.shown_hp - 40)
        if t == 110:
            newly = [p for p in alive if p.hp <= 0]
            newly.sort(key=lambda p: p.hp, reverse=True)
            for p in newly:
                p.alive = False
                p.out_round = self.round
            self.out_order.extend(reversed(newly))   # worst first
            if newly:
                self.msg = '、'.join(p.name for p in newly) + 'の負け'
        if t >= 140:
            alive = [p for p in self.players if p.alive]
            if len(alive) <= 1 or not self.me.alive:
                self.finish()
            else:
                for p in alive:
                    self.deal(p)
                self.new_round()

    def finish(self):
        alive = sorted([p for p in self.players if p.alive], key=lambda p: -p.hp)
        ranking = alive + list(reversed(self.out_order))
        self.rank = ranking.index(self.me) + 1
        if self.rank == 1:
            self.data['wins'] = self.data.get('wins', 0) + 1
        self.app.save_progress()
        self.phase = 'result'
        self.t = 0
        self.app.audio.set_game(self.game)
        self.app.audio.play_bgm(57 if self.rank == 1 else 47, 0)   # KANDOU_SHORT / KONWAKU

    # ------------------------------------------------------------ input
    def hand_rect(self, i):
        return pygame.Rect(HAND_X0 + i * (CARD_W + 10), HAND_Y, CARD_W, CARD_H)

    def event(self, ev):
        A = self.A
        click = None
        if ev.type == pygame.MOUSEBUTTONDOWN and ev.button == 1:
            click = self.bottom_pos(ev.lpos)
        key = ev.key if ev.type == pygame.KEYDOWN else None
        confirm = key in (pygame.K_RETURN, pygame.K_SPACE, pygame.K_z)

        if self.phase == 'menu':
            items = self.menu_items()
            if key in (pygame.K_UP, pygame.K_w):
                self.menu_sel = (self.menu_sel - 1) % len(items)
            elif key in (pygame.K_DOWN, pygame.K_s):
                self.menu_sel = (self.menu_sel + 1) % len(items)
            elif confirm:
                self.menu_pick(items[self.menu_sel][1])
            elif A.is_back(ev):
                self.sfx(SE_CANCEL, self.game)
                self.leave()
            elif click:
                for i in range(len(items)):
                    if self.menu_rect(i).collidepoint(click):
                        self.menu_pick(items[i][1])
            return
        if self.phase == 'rules':
            if click or confirm or A.is_back(ev):
                self.phase = 'menu'
            return
        if self.phase == 'deck':
            self.deck_event(ev, click, key, confirm)
            return
        if self.phase == 'result':
            if (click or confirm or A.is_back(ev)) and self.t > 30:
                self.phase = 'menu'
                self.app.audio.play_bgm(44, 30)
            return
        if self.phase != 'choose':
            if A.is_back(ev) and self.phase == 'reveal':
                pass
            return
        # ---- choosing
        hand = self.me.hand
        combos = combo_attrs(hand)
        if A.is_back(ev) or (click and BTN_END.collidepoint(click)):
            if self.combo_mode:
                self.combo_mode, self.combo_pick = False, []
                self.sfx(SE_CANCEL, self.game)
            else:
                self.sfx(SE_CANCEL, self.game)
                self.phase = 'menu'
            return
        if key in (pygame.K_LEFT, pygame.K_a):
            self.sel = (self.sel - 1) % len(hand)
            self.sfx(SE_MOVE, self.game)
        elif key in (pygame.K_RIGHT, pygame.K_d):
            self.sel = (self.sel + 1) % len(hand)
            self.sfx(SE_MOVE, self.game)
        elif key == pygame.K_c or (click and BTN_COMBO.collidepoint(click)):
            if combos and not self.combo_mode:
                self.combo_mode = True
                self.combo_pick = []
                self.sfx(SE_COMBO_SELECT, self.game)
            elif self.combo_mode and len(self.combo_pick) == 3:
                self.commit([hand[i] for i in self.combo_pick], True)
            return
        elif confirm or (click and BTN_OK.collidepoint(click)):
            if self.combo_mode:
                if len(self.combo_pick) == 3:
                    self.commit([hand[i] for i in self.combo_pick], True)
                else:
                    self.toggle_combo(self.sel)
            else:
                self.commit([hand[self.sel]], False)
            return
        if click:
            for i in range(len(hand)):
                if self.hand_rect(i).collidepoint(click):
                    if self.combo_mode:
                        self.toggle_combo(i)
                    elif self.sel == i:
                        self.commit([hand[i]], False)
                    else:
                        self.sel = i
                        self.sfx(SE_MOVE, self.game)

    def toggle_combo(self, i):
        hand = self.me.hand
        if i in self.combo_pick:
            self.combo_pick.remove(i)
        elif len(self.combo_pick) < 3:
            if not self.combo_pick or hand[self.combo_pick[0]].kind == hand[i].kind:
                self.combo_pick.append(i)
        self.sfx(SE_MOVE, self.game)

    def menu_items(self):
        ok = self.can_play()
        return [('はじめる（' + '２３４'[self.n_players - 2] + '人）', 'start', ok),
                ('人数を変える', 'players', True),
                ('デッキ編成', 'deck', ok),
                ('ルール説明', 'rules', True),
                ('もどる', 'back', True)]

    def menu_rect(self, i):
        return pygame.Rect(48, 30 + i * 28, 160, 22)

    def menu_pick(self, v):
        items = dict((b, c) for a, b, c in self.menu_items())
        if not items.get(v, True):
            self.sfx(SE_CANCEL, self.game)
            return
        self.sfx(SE_ENTER, self.game)
        if v == 'start':
            self.start_match()
        elif v == 'players':
            self.n_players = 2 + (self.n_players - 1) % 3
        elif v == 'deck':
            self.open_deck()
        elif v == 'rules':
            self.phase = 'rules'
        elif v == 'back':
            self.leave()

    # ------------------------------------------------------------ deck builder
    def open_deck(self):
        self.phase = 'deck'
        self.deck_cards = sorted(self.owned_battle(), key=lambda c: (c.kind, -c.power))
        self.deck_sel = set(c.index for c in self.player_deck())
        self.deck_cursor = 0
        self.deck_scroll = 0

    DECK_COLS, DECK_ROWS = 7, 4

    def deck_rect(self, k):
        r, c = divmod(k, self.DECK_COLS)
        return pygame.Rect(6 + c * 35, 24 + r * 38, 32, 34)

    def deck_event(self, ev, click, key, confirm):
        n = len(self.deck_cards)
        per = self.DECK_COLS * self.DECK_ROWS
        if self.A.is_back(ev):
            self.save_deck()
            return
        if key in (pygame.K_LEFT, pygame.K_a):
            self.deck_cursor = max(0, self.deck_cursor - 1)
        elif key in (pygame.K_RIGHT, pygame.K_d):
            self.deck_cursor = min(n - 1, self.deck_cursor + 1)
        elif key in (pygame.K_UP, pygame.K_w):
            self.deck_cursor = max(0, self.deck_cursor - self.DECK_COLS)
        elif key in (pygame.K_DOWN, pygame.K_s):
            self.deck_cursor = min(n - 1, self.deck_cursor + self.DECK_COLS)
        elif confirm:
            self.toggle_deck(self.deck_cursor)
        elif ev.type == pygame.MOUSEBUTTONDOWN and ev.button in (4, 5):
            self.deck_scroll = max(0, min(self.deck_scroll + (-1 if ev.button == 4 else 1),
                                          max(0, (n - 1) // self.DECK_COLS - self.DECK_ROWS + 1)))
        elif click:
            if BTN_OK.collidepoint(click):
                self.save_deck()
                return
            for k in range(per):
                idx = self.deck_scroll * self.DECK_COLS + k
                if idx < n and self.deck_rect(k).collidepoint(click):
                    self.deck_cursor = idx
                    self.toggle_deck(idx)
        row = self.deck_cursor // self.DECK_COLS
        if row < self.deck_scroll:
            self.deck_scroll = row
        elif row >= self.deck_scroll + self.DECK_ROWS:
            self.deck_scroll = row - self.DECK_ROWS + 1

    def toggle_deck(self, idx):
        c = self.deck_cards[idx]
        if c.index in self.deck_sel:
            self.deck_sel.discard(c.index)
        elif len(self.deck_sel) < DECK_SIZE:
            self.deck_sel.add(c.index)
        self.sfx(SE_MOVE, self.game)

    def save_deck(self):
        if len(self.deck_sel) == DECK_SIZE:
            self.data['deck'] = sorted(self.deck_sel)
            self.app.save_progress()
            self.sfx(SE_ENTER, self.game)
        else:
            self.sfx(SE_CANCEL, self.game)
        self.phase = 'menu'

    # ------------------------------------------------------------ frame
    def update(self):
        self.t += 1
        if self.phase == 'reveal':
            self.resolve_step()

    # ------------------------------------------------------------ drawing
    def text(self, surf, s, x, y, color=WHITE, outline=True, align='left', half=False):
        g = self.game
        idxs = g.text_glyphs(s)
        step = 7 if half else 12
        w = step * len(idxs)
        if align == 'center':
            x -= w // 2
        elif align == 'right':
            x -= w
        for i, gi in enumerate(idxs):
            if gi is None or gi == 614:
                continue
            if half and s[i].isascii():
                gl = g.han_glyph(ord(s[i]))
            else:
                gl = g.glyph(gi, vertical=False)
            self.A.Scene._outlined(surf, gl, x + step * i, y, color, (0, 0, 0))
        return w

    def draw(self, surf):
        surf.fill((0, 0, 0))
        self.draw_gap(surf)
        top, bot = self.top, self.bottom
        top.blit(self.topbase, (0, 0))
        bot.blit(self.btmbase, (0, 0))
        if self.phase == 'menu':
            self.draw_menu(top, bot)
        elif self.phase == 'rules':
            self.draw_rules(top, bot)
        elif self.phase == 'deck':
            self.draw_deck(top, bot)
        else:
            self.draw_match(top, bot)

    def draw_menu(self, top, bot):
        own = len(self.owned_battle())
        self.text(top, f'所持カード　{own}枚', 128, 150, GOLD, align='center')
        if not self.can_play():
            self.text(top, f'あと{DECK_SIZE - own}枚でプレイできます', 128, 168, WHITE, align='center')
        else:
            w = self.data.get('wins', 0)
            self.text(top, f'優勝　{w}回', 128, 168, WHITE, align='center')
        for i, (label, v, en) in enumerate(self.menu_items()):
            r = self.menu_rect(i)
            hl = i == self.menu_sel
            pygame.draw.rect(bot, (20, 40, 30), r)
            pygame.draw.rect(bot, GOLD if hl else (150, 170, 120), r, 1)
            self.text(bot, label, r.centerx, r.y + 5, (GOLD if hl else WHITE) if en else (120, 120, 120),
                      align='center')

    def draw_rules(self, top, bot):
        lines = ['ゲームの流れ',
                 '各プレイヤーは手札から１枚出す。',
                 '属性ボーナスを加えてＰｔを決定。',
                 'Ｐｔが最大のプレイヤーが攻撃。',
                 '他は（攻撃Ｐｔ－自分Ｐｔ）の',
                 'ダメージ。体力０で負け。']
        lines2 = ['属性ボーナス',
                  '熱血→泣き→萌え→熱血の順に強い。',
                  '場に２属性だけなら勝った属性に＋200',
                  'コンボ',
                  '同じ属性が３枚あれば３枚同時に出せ、',
                  'Ｐｔは一番強いカードの３倍。',
                  'デッキは２０枚。体力は3000。']
        sh = pygame.Surface((SCREEN_W, TOP_H), pygame.SRCALPHA)
        sh.fill((0, 0, 0, 170))
        top.blit(sh, (0, 0))
        for i, l in enumerate(lines):
            self.text(top, l, 10, 10 + 22 * i, GOLD if i == 0 else WHITE)
        for i, l in enumerate(lines2):
            self.text(bot, l, 8, 12 + 22 * i, GOLD if i in (0, 3) else WHITE)

    def draw_deck(self, top, bot):
        n = len(self.deck_cards)
        cur = self.deck_cards[self.deck_cursor] if n else None
        if cur is not None:
            big = self.card_art(cur, '')
            if big is not None:
                top.blit(big, (12, 40))
            self.text(top, cur.name, 128, 44, GOLD)
            self.text(top, f'{ATTR_NAMES[cur.kind]}　{cur.power}Ｐｔ', 128, 64, ATTR_COL[cur.kind])
        self.text(top, f'デッキ　{len(self.deck_sel)}／{DECK_SIZE}', 128, 150, WHITE)
        per = self.DECK_COLS * self.DECK_ROWS
        for k in range(per):
            idx = self.deck_scroll * self.DECK_COLS + k
            if idx >= n:
                break
            c = self.deck_cards[idx]
            r = self.deck_rect(k)
            pygame.draw.rect(bot, ATTR_COL[c.kind], r)
            art = self.card_art(c, 's')
            if art is not None:
                bot.blit(art, (r.x + 3, r.y + 2))
            if c.index not in self.deck_sel:
                sh = pygame.Surface(r.size, pygame.SRCALPHA)
                sh.fill((0, 0, 0, 150))
                bot.blit(sh, r.topleft)
            if idx == self.deck_cursor:
                pygame.draw.rect(bot, GOLD, r.inflate(2, 2), 2)
        ok = len(self.deck_sel) == DECK_SIZE
        bot.blit(self.btn['ok'], BTN_OK.topleft, (0, 0, BTN_OK.w + 8, BTN_OK.h + 6))
        if not ok:
            self.text(bot, '２０枚えらんでね', 8, 176, WHITE)

    def draw_match(self, top, bot):
        # top: four HP bars with each player's field card
        for i, p in enumerate(self.players):
            y = 2 + i * 36
            bar = self.bars[i % 4]
            top.blit(bar, (0, y))
            col = bar.get_at((10, 16))
            frac = max(0.0, p.shown_hp / HP_START)
            pygame.draw.rect(top, (250, 250, 250), (60, y + 8, 120, 14))
            pygame.draw.rect(top, col, (60, y + 8, int(120 * frac), 14))
            pygame.draw.rect(top, (40, 40, 40), (60, y + 8, 120, 14), 1)
            self.text(top, p.name, 4, y + 9, WHITE if p.alive else (130, 130, 130))
            self.text(top, str(max(0, p.shown_hp)), 176, y + 10, WHITE, align='right', half=True)
            if p.played and self.phase in ('reveal', 'result'):
                c = p.played[0]
                art = self.card_art(c, 's')
                pygame.draw.rect(top, ATTR_COL[c.kind], (184, y + 2, 30, 30))
                if art is not None:
                    top.blit(art, (186, y + 4))
                if p.combo:
                    self.text(top, '×3', 200, y + 22, GOLD, half=True)
                if self.t >= 20:
                    self.text(top, str(p.pt), 252, y + 10, GOLD if p in getattr(self, 'attacker', []) and self.t >= 60
                              else WHITE, align='right', half=True)
                if self.t >= 60 and p.damage:
                    self.text(top, f'-{p.damage}', 176, y + 22, (255, 120, 120), align='right', half=True)
            if not p.alive:
                sh = pygame.Surface((256, 34), pygame.SRCALPHA)
                sh.fill((0, 0, 0, 110))
                top.blit(sh, (0, y))
        self.text(top, self.msg, 128, 154, GOLD, align='center')
        self.text(top, f'第{self.round}戦　山{len(self.pile)}枚', 128, 172, WHITE, align='center')
        # bottom: hand
        hand = self.me.hand
        for i, c in enumerate(hand):
            r = self.hand_rect(i)
            picked = i in self.combo_pick
            dim = self.combo_mode and self.combo_pick and c.kind != hand[self.combo_pick[0]].kind
            yo = -8 if (picked or (not self.combo_mode and i == self.sel and self.phase == 'choose')) else 0
            self.draw_card(bot, c, r.x, r.y + yo, hl=(i == self.sel and self.phase == 'choose'), dim=dim)
        cur = hand[self.sel] if hand and self.phase == 'choose' else None
        if cur is not None:
            self.text(bot, cur.name, 8, 116, WHITE)
            self.text(bot, ATTR_NAMES[cur.kind], 8, 132, ATTR_COL[cur.kind])
        if self.phase == 'choose':
            bot.blit(self.btn['ok'], BTN_OK.topleft, (0, 0, BTN_OK.w + 8, BTN_OK.h + 6))
            if combo_attrs(hand):
                bot.blit(self.btn['combo'], BTN_COMBO.topleft, (0, 0, BTN_COMBO.w + 8, BTN_COMBO.h + 6))
            bot.blit(self.btn['end'], BTN_END.topleft, (0, 0, BTN_END.w + 8, BTN_END.h + 6))
            if self.combo_mode:
                self.text(bot, f'コンボ：３枚えらぶ（{len(self.combo_pick)}）', 8, 6, GOLD)
        if self.phase == 'result':
            img = self.res['w' if self.rank == 1 else 'l'] if len(self.players) == 2 else self.res[str(self.rank)]
            bot.blit(img, (SCREEN_W // 2 - img.get_width() // 2, 110))

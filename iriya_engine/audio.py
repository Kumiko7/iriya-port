"""BGM / SE playback on pygame.mixer with loop points and volume tweens."""
import threading

import numpy as np
import pygame

from .alt_bgm import DS_LOUDNESS, MAX_GAIN_DB

ALT_CACHE = 6               # decoded Himawari tracks kept (stereo, up to ~4 min each)

SE_CHANNELS = 4


class _Voice:
    def __init__(self, channel):
        self.ch = channel
        self.intro = None
        self.loop = None
        self.id = None
        self.vol = 1.0
        self.vol_from = 1.0
        self.vol_to = 1.0
        self.t = 0
        self.dur = 0
        self.stop_after = False
        self.pending = None  # loaded but not started (SE)

    def set_volume(self, target, frames):
        if frames <= 0:
            self.vol = self.vol_to = target
            self.dur = 0
        else:
            self.vol_from, self.vol_to, self.t, self.dur = self.vol, target, 0, frames

    def update(self, master):
        if self.dur:
            self.t += 1
            k = min(1.0, self.t / self.dur)
            self.vol = self.vol_from + (self.vol_to - self.vol_from) * k
            if k >= 1:
                self.dur = 0
                if self.stop_after:
                    self.stop()
        self.ch.set_volume(max(0.0, min(1.0, self.vol * master)))
        if self.loop is not None and self.ch.get_busy() and self.ch.get_queue() is None:
            self.ch.queue(self.loop)

    def start(self, snd_tuple, vol=1.0):
        self.stop()
        if snd_tuple is None:
            return
        intro, loop = snd_tuple
        self.intro, self.loop = intro, loop
        self.vol = self.vol_to = vol
        self.dur = 0
        self.stop_after = False
        self.ch.play(intro)
        if loop is not None:
            self.ch.queue(loop)

    def resume_with(self, snd_tuple):
        """Begin playing a sound that finished loading, keeping the volume state."""
        if snd_tuple is None:
            return
        self.intro, self.loop = snd_tuple
        self.ch.play(self.intro)
        if self.loop is not None:
            self.ch.queue(self.loop)

    def stop(self):
        self.ch.stop()
        self.intro = self.loop = None
        self.id = None
        self.stop_after = False
        self.dur = 0

    def busy(self):
        return self.ch.get_busy()


class Audio:
    def __init__(self):
        self.ok = False
        try:
            pygame.mixer.pre_init(44100, -16, 2, 1024)
            pygame.mixer.init(44100, -16, 2, 1024)
            pygame.mixer.set_num_channels(SE_CHANNELS + 2)
            self.ok = True
        except pygame.error:
            return
        self.bgm = _Voice(pygame.mixer.Channel(0))
        self.se = [_Voice(pygame.mixer.Channel(1 + i)) for i in range(SE_CHANNELS)]
        self.ui = pygame.mixer.Channel(SE_CHANNELS + 1)
        self.bgm_volume = 0.8
        self.se_volume = 0.9
        self._cache = {}
        self.game = None
        self.muted = False
        # DS BGM model (0x201c634..): 96 prepares a track silently (state 1), 90
        # fades it in from 0 (state 2), 91 fades it out; 93 sets a separate master
        # volume that 96 resets to full.
        self.bgm_state = 0
        self.master = _Voice.__new__(_Voice)
        self.master.vol = self.master.vol_to = 1.0
        self.master.vol_from, self.master.t, self.master.dur = 1.0, 0, 0
        self.alt = None             # AltBGM (alternative soundtrack) when extracted
        self.alt_on = False
        self._alt_cache = {}
        self._alt_jobs = {}         # (track, ds name) -> decode job running on a thread
        self._bgm_wait = None       # key the BGM voice should play once decoded
        self._decoder = None
        self.bgm_sid = None         # the DS track the script asked for
        self.bgm_ctx = None

    def set_game(self, game):
        self.game = game

    def _sound(self, sid):
        key = (self.game.id, sid)
        r = self._cache.get(key)
        if r is None:
            p = self.game.pcm(sid)
            if p is None:
                return None
            pcm, loop, _name = p
            st = np.repeat(pcm[:, None], 2, axis=1)
            if loop is not None and 0 < loop < len(pcm) - 64:
                intro = pygame.sndarray.make_sound(np.ascontiguousarray(st[:loop])) if loop > 0 else None
                body = pygame.sndarray.make_sound(np.ascontiguousarray(st[loop:]))
                r = (intro or body, body)
            elif loop == 0:
                body = pygame.sndarray.make_sound(np.ascontiguousarray(st))
                r = (body, body)
            else:
                r = (pygame.sndarray.make_sound(np.ascontiguousarray(st)), None)
            if len(self._cache) > 48:
                self._cache.pop(next(iter(self._cache)))
            self._cache[key] = r
        return r

    # ------------------------------------------------------------------ BGM
    def _ds_name(self, sid):
        names = getattr(getattr(self.game, 'sdat', None), 'names', None) or []
        return str(names[sid]) if 0 <= sid < len(names) else None

    def _bgm_key(self, sid, ctx):
        """What actually plays for DS track sid: ('h', track) or (game, sid)."""
        if self.alt_on and self.alt is not None:
            t = self.alt.pick(self._ds_name(sid), ctx)
            if t is not None:
                return ('h', t, self._ds_name(sid))
        return (self.game.id if self.game else None, sid)

    def _bgm_sound(self, key, block=False):
        """The sound tuple for key; for Himawari tracks None while still decoding."""
        if key[0] != 'h':
            return self._sound(key[1])
        ck = (key[1], key[2])
        r = self._alt_cache.get(ck)
        if r is None and block:
            r = self._decode_alt(*ck)
            self._alt_store(ck, r)
        elif r is None and ck not in self._alt_jobs:
            # decoding a long track takes up to a second; do it off the main thread
            # (pygame releases the GIL while it decodes)
            job = {'result': None, 'done': False}
            self._alt_jobs[ck] = job

            def work(job=job, ck=ck):
                job['result'] = self._decode_alt(*ck)
                job['done'] = True
            threading.Thread(target=work, daemon=True).start()
        return r

    def _alt_store(self, ck, r):
        if r is None:
            return
        if len(self._alt_cache) >= ALT_CACHE:
            self._alt_cache.pop(next(iter(self._alt_cache)))
        self._alt_cache[ck] = r

    def _decode_alt(self, track, ds_name):
        mix_rate = (pygame.mixer.get_init() or (44100,))[0]
        if self._decoder is None:
            from .alt_bgm import Decoder
            self._decoder = Decoder(mix_rate)
        raw = self._decoder.decode(self.alt.path(track))
        if raw is None:
            if not self._decoder.failed:
                return None
            # no helper process: decode here (freezes for a moment, but works)
            try:
                raw = pygame.mixer.Sound(file=self.alt.path(track)).get_raw()
            except (OSError, pygame.error):
                return None
        y = np.frombuffer(raw, np.int16).reshape(-1, 2).astype(np.float32)
        probe = y[::7]
        db = 20 * np.log10(np.sqrt(np.mean(probe * probe)) / 32768 + 1e-9)
        target = DS_LOUDNESS.get(ds_name, -16.0)
        gain = 10 ** (max(-MAX_GAIN_DB, min(MAX_GAIN_DB, target - db)) / 20)
        y = np.clip(y * gain, -32768, 32767).astype(np.int16)
        loop, rate = self.alt.loop(track)
        if loop:
            k = mix_rate / rate
            end, start = min(len(y), int(loop[0] * k)), int(loop[1] * k)
            if 0 <= start < end - 64:
                body = pygame.mixer.Sound(buffer=y[start:end].tobytes())
                intro = pygame.mixer.Sound(buffer=y[:start].tobytes()) if start > 64 else None
                return (intro or body, body)
        body = pygame.mixer.Sound(buffer=y.tobytes())
        return (body, body)

    def prefetch(self, sids, ctx):
        """Start decoding what the next scenes will play (alternative soundtrack only)."""
        if not (self.ok and self.alt_on and self.alt is not None):
            return
        for t, n in self.alt.next_after([self._ds_name(x) for x in sids], ctx)[:2]:
            self._bgm_sound(('h', t, n))

    def _bgm_start(self, key, vol):
        """Start key on the BGM voice at vol (silently pending if still decoding)."""
        snd = self._bgm_sound(key)
        self.bgm.start(snd, vol)
        if snd is None:              # start() leaves volume alone without a sound
            self.bgm.vol = self.bgm.vol_to = vol
            self.bgm.dur = 0
            self.bgm.stop_after = False
        self.bgm.id = key
        self._bgm_wait = key if snd is None else None

    def _bgm_on(self, key):
        return self.bgm.id == key and (self.bgm.busy() or self._bgm_wait == key)

    def _poll_alt(self):
        for ck, job in list(self._alt_jobs.items()):
            if not job['done']:
                continue
            del self._alt_jobs[ck]
            self._alt_store(ck, job['result'])
            w = self._bgm_wait
            if w is not None and (w[1], w[2]) == ck and self.bgm.id == w:
                self._bgm_wait = None
                snd = job['result']
                if snd is None:                      # unreadable: fall back to the DS track
                    snd = self._sound(self.bgm_sid) if self.bgm_sid is not None else None
                self.bgm.resume_with(snd)

    def set_alt(self, on):
        """Switch soundtracks; whatever is playing restarts in the other set."""
        on = bool(on and self.alt is not None)
        if on == self.alt_on:
            return
        self.alt_on = on
        if not self.ok or self.bgm_sid is None or not self._bgm_on(self.bgm.id):
            return
        key = self._bgm_key(self.bgm_sid, self.bgm_ctx)
        if key != self.bgm.id:
            vol, stop_after = self.bgm.vol_to, self.bgm.stop_after
            self._bgm_start(key, 0.0)
            self.bgm.set_volume(vol, 20)
            self.bgm.stop_after = stop_after

    def play_bgm(self, sid, fade_frames=0, ctx=None):
        if not self.ok:
            return
        self.bgm_state = 2
        self.master_to(1.0, 0)
        key = self._bgm_key(sid, ctx)
        self.bgm_sid, self.bgm_ctx = sid, ctx
        if self._bgm_on(key):
            self.bgm.stop_after = False
            self.bgm.set_volume(1.0, fade_frames)
            return
        self._bgm_start(key, 0.0 if fade_frames else 1.0)
        if fade_frames:
            self.bgm.set_volume(1.0, fade_frames)

    def bgm_prepare(self, sid, ctx=None):
        """Op 96: start the track silently; op 90 fades it in."""
        if not self.ok:
            return
        key = self._bgm_key(sid, ctx)
        self.bgm_sid, self.bgm_ctx = sid, ctx
        if not self._bgm_on(key):
            self._bgm_start(key, 0.0)
        self.bgm.set_volume(0.0, 0)
        self.bgm.stop_after = False
        self.master_to(1.0, 0)
        self.bgm_state = 1

    def bgm_fade_in(self, frames):
        """Op 90: only acts on a freshly prepared track."""
        if not self.ok or self.bgm_state != 1:
            return
        self.bgm_state = 2
        self.bgm.set_volume(0.0, 0)
        self.bgm.set_volume(1.0, frames if frames >= 1 else 0)

    def bgm_fade_out(self, frames):
        """Op 91: only acts on a playing track; under 1 frame stops at once."""
        if not self.ok or self.bgm_state != 2:
            return
        if frames < 1:
            self.bgm.stop()
            self.bgm_state = 0
        else:
            self.bgm.set_volume(0.0, frames)
            self.bgm.stop_after = True

    def master_to(self, target, frames):
        m = self.master
        if frames <= 0:
            m.vol = m.vol_to = target
            m.dur = 0
        else:
            m.vol_from, m.vol_to, m.t, m.dur = m.vol, target, 0, frames

    def bgm_fade(self, target, frames, stop=False):
        if not self.ok:
            return
        self.bgm.set_volume(target, frames)
        self.bgm.stop_after = stop
        if stop and frames <= 0:
            self.bgm.stop()

    # ------------------------------------------------------------------- SE
    def load_se(self, ch, sid):
        if not self.ok or not (0 <= ch < SE_CHANNELS):
            return
        v = self.se[ch]
        v.pending = sid

    def play_se(self, ch, fade_frames=0):
        if not self.ok or not (0 <= ch < SE_CHANNELS):
            return
        v = self.se[ch]
        # The DS plays only a sound loaded since the last play (0x201cafc checks the
        # channel is in its 'loaded' state): a play after a stop or a second play is a no-op.
        sid, v.pending = v.pending, None
        if sid is None:
            return
        v.start(self._sound(sid), 0.0 if fade_frames else 1.0)
        v.id = sid
        if fade_frames:
            v.set_volume(1.0, fade_frames)

    def skip_se(self, ch):
        """A play passed over while skipping: the loaded sound counts as used."""
        if self.ok and 0 <= ch < SE_CHANNELS:
            self.se[ch].pending = None

    def stop_se(self, ch, frames=0):
        if not self.ok or not (0 <= ch < SE_CHANNELS):
            return
        v = self.se[ch]
        if frames <= 0:
            v.stop()
        else:
            v.set_volume(0.0, frames)
            v.stop_after = True

    def se_volume_to(self, ch, vol, frames):
        if self.ok and 0 <= ch < SE_CHANNELS:
            self.se[ch].set_volume(vol, frames)

    def stop_all(self):
        if not self.ok:
            return
        self.bgm.stop()
        self.bgm_state = 0
        self.master_to(1.0, 0)
        for v in self.se:
            v.stop()
            v.pending = None

    def ui_sound(self, sid):
        if not self.ok or self.game is None:
            return
        s = self._sound(sid)
        if s:
            self.ui.set_volume(self.se_volume * 0.8)
            self.ui.play(s[0])

    def update(self):
        if not self.ok:
            return
        m = 0.0 if self.muted else 1.0
        mv = self.master
        if mv.dur:
            mv.t += 1
            k = min(1.0, mv.t / mv.dur)
            mv.vol = mv.vol_from + (mv.vol_to - mv.vol_from) * k
            if k >= 1:
                mv.dur = 0
        if self._alt_jobs:
            self._poll_alt()
        self.bgm.update(self.bgm_volume * m * mv.vol)
        if self.bgm_state == 2 and self.bgm.id is None:
            self.bgm_state = 0
        for v in self.se:
            v.update(self.se_volume * m)

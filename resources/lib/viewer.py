# -*- coding: utf-8 -*-
"""Full-screen viewer: Ken Burns pan and zoom, crossfades, captions and inline video."""
import math
import os
import random
import re
import shutil
import threading
import time

import xbmc
import xbmcaddon
import xbmcgui
import xbmcvfs

from . import items, kodi, proxy, thumbhash
from .api import ApiError, ImmichClient, from_asset

SCREEN = 1920 / 1080
FILL_SLACK = 0.1                         # how far from 16:9 a photo may be and still fill the screen; others sit on the backdrop
PAN_WIDE = 2.4                           # panoramas this wide pan across the screen instead
PAN_SPEED = 0.015                        # screen widths a second
LOAD_WAIT = 10                           # seconds a photo stays up waiting for the next to arrive
AHEAD = 3                                # photos whose details are fetched in the background
EASE_PEAK = 0.3                          # motion is fastest here: quick to start, long to settle
CLOSE = {9, 10, 13, 92}                  # parent dir, previous menu, stop, back
PAUSE = {7, 12, 79, 229}                 # select, pause, play, play/pause
NEXT = {2, 14, 77}                       # right, next item, fast forward
PREV = {1, 15, 78}                       # left, previous item, rewind
INFO = {11}
DRIFT = (16, 8)                          # how far the screensaver's text wanders, against OLED burn-in
CORNER_TIME = 300                        # seconds the screensaver's date and clock stay in their corners
CORNERS = (('', ''), ('top', ''), ('top', 'right'), ('', 'right'))   # the date's edge and side; the clock takes the opposite
# Player.stop() and pause() hold the GIL until Kodi is done, and closing a file asks the proxy for its date
STOP = 'PlayerControl(Stop)'
PLAY_PAUSE = 'PlayerControl(Play)'
UNDECODABLE = {'image/avif'}             # originals LibreELEC's Kodi can't show over http
UPRIGHT = {'0', '1'}                     # others: newer FFmpeg leaves Kodi showing originals unrotated
ASSET_ID = re.compile(r'[0-9a-f-]+')     # safe in a file name


_slowdown = None


def skin_slowdown():
    """Kodi multiplies every animation time by the skin's effectslowdown (0.22 in Confluence ZEITGEIST)."""
    global _slowdown
    if _slowdown is None:
        _slowdown = 1.0
        try:
            path = xbmcvfs.translatePath(xbmcaddon.Addon(xbmc.getSkinDir()).getAddonInfo('path'))
            with open(os.path.join(path, 'addon.xml'), encoding='utf-8') as f:
                found = re.search(r'effectslowdown="([0-9.]+)"', f.read())
            if found and float(found.group(1)) > 0:
                _slowdown = float(found.group(1))
        except (RuntimeError, OSError, ValueError) as e:
            kodi.log('skin effectslowdown unknown: {}'.format(e), xbmc.LOGWARNING)
    return _slowdown


def cover_size(ratio):
    """A photo cropped to fill the screen, in skin pixels."""
    return (1920, 1920 / ratio) if ratio < SCREEN else (1080 * ratio, 1080)


def fit_size(ratio):
    """A photo fitted inside the screen, in skin pixels."""
    return (1920, 1920 / ratio) if ratio > SCREEN else (1080 * ratio, 1080)


def gui_height():
    """The interface height in real pixels; 1080 when Kodi doesn't say."""
    try:
        return int(xbmc.getInfoLabel('System.ScreenHeight')) or 1080
    except ValueError:
        return 1080


def ease(done, easing):
    """Kodi's sine tweens at a share of their time; skew is sine in up to EASE_PEAK, then sine out."""
    if easing == 'out':
        return math.sin(done * math.pi / 2)
    if easing == 'skew':
        p = EASE_PEAK
        if done <= p:
            return p * (1 - math.cos(done / p * math.pi / 2))
        return p + (1 - p) * math.sin((done - p) / (1 - p) * math.pi / 2)
    return 0.5 - 0.5 * math.cos(done * math.pi)


def anim(effect, **attrs):
    attrs.setdefault('condition', 'true')
    for k in ('time', 'delay'):
        if attrs.get(k):
            attrs[k] = int(attrs[k] / skin_slowdown())      # real milliseconds on any skin
    return ('conditional', 'effect={} {}'.format(effect, ' '.join('{}={}'.format(k, v) for k, v in attrs.items())))


class Waker(xbmc.Monitor):
    """Closes the screensaver when Kodi wakes up."""
    def __init__(self, window):
        super().__init__()
        self.window = window

    def onScreensaverDeactivated(self):
        self.window.closed = True


class Viewer(xbmcgui.WindowXMLDialog):
    def __init__(self, *args, **kwargs):
        super().__init__(*args)
        self.client = kwargs['client']
        self.assets = kwargs['assets']
        self.index = kwargs.get('start', 0)
        self.playing = kwargs.get('autoplay', True)
        self.more = kwargs.get('more')              # fetches another batch for endless shuffle
        self.screensaver = kwargs.get('screensaver', False)
        self.seen = {a['id'] for a in self.assets}
        self.actions = []
        self.closed = False

    def onInit(self):
        pass

    def onAction(self, action):
        self.actions.append(action.getId())

    # ---------------------------------------------------------------- setup
    def setup(self):
        self.stay = max(kodi.setting_int('slide_time'), 2)
        self.fade = max(int(kodi.setting_number('fade_time') * 1000), 200)
        self.kenburns = kodi.setting_bool('kenburns') or self.screensaver
        self.zoom = min(max(kodi.setting_int('zoom'), 100), 110)
        self.scale = self.zoom / 100.0 if self.kenburns else 1.0   # how far photo controls outsize the screen
        self.size = 'preview'
        if kodi.setting_bool('hires'):
            try:
                perms = set((self.client.key_info() or {}).get('permissions') or [])
            except ApiError:
                perms = set()
            if perms & {'all', 'asset.download'}:   # fullsize redirects to the original
                self.size = 'fullsize'
            else:
                kodi.notify(kodi.L(30702))
        self.info = 1 if kodi.setting_bool('captions') else 0   # 0 nothing, 1 caption, 2 caption and details
        self.raws = {}
        self.quick = ImmichClient(self.client.base_url, self.client.api_key, timeout=5)
        self.monitor = Waker(self) if self.screensaver else xbmc.Monitor()
        self.show_info()
        self.setProperty('immich.clock', 'true' if kodi.setting_bool('clock') else 'false')
        self.layers = [[self.getControl(base + k) for k in range(4)] for base in (100, 200)]
        for layer in self.layers:
            layer[0].setAnimations([anim('fade', start=0, end=0, time=0)])
        self.shown = None                   # layer on screen
        self.prepared = {}                  # layer -> (asset index, fills screen)
        self.waiting = {}                   # layer -> (photo the proxy will announce, since when)
        self.ahead = None                   # thread fetching the next photos' details
        self.player = xbmc.Player()
        self.video = None
        self.next_at = None
        self.kb = None
        self.main = None
        self.left = self.stay                       # slide time still to run while paused

    # --------------------------------------------------------------- slides
    def free_layer(self):
        return 1 if self.shown == 0 else 0

    def prepare(self, index, layer):
        """Load an image into a hidden layer so it is ready before it fades in."""
        asset = self.assets[index]
        group, back, cover, fit = self.layers[layer]
        group.setAnimations([anim('fade', start=0, end=0, time=0)])   # may still be fading out
        size = asset['shown_size'] = self.photo_size(asset)
        url = self.client.thumb_url(asset['id'], size)
        proxy.forget_cached(url)                    # Kodi 22 caches it anyway, at 720p
        for control in (back, cover, fit):          # Kodi shows the old texture until the new one loads
            control.setImage('')
        pan = self.pans(asset)
        if pan:
            self.span(fit, asset['ratio'])
        elif self.scale > 1:
            for control in (cover, fit):            # Kodi decodes to the control's size
                self.outsize(control)
        self.monitor.waitForAbort(0.05)
        full = abs(asset['ratio'] - SCREEN) < FILL_SLACK and not pan
        if pan:
            cover.setImage('')
            back.setImage('')
            fit.setImage(url, False)
        elif full:
            fit.setImage('')
            back.setImage('')
            cover.setImage(url, False)
        else:
            cover.setImage('')
            back.setImage(self.backdrop(asset), False)
            fit.setImage(url, False)
        self.prepared[layer] = (index, full)
        self.waiting[layer] = ('{}:{}'.format(asset['id'], size), time.time())

    def refill(self):
        try:
            batch = self.more()
        except ApiError as e:
            kodi.log('shuffle refill failed: {}'.format(e), xbmc.LOGWARNING)
            return
        if self.screensaver or not kodi.setting_bool('videos'):
            batch = [a for a in batch if a['image']]
        new = [a for a in batch if a['id'] not in self.seen]
        if not new:                                 # everything shown once; allow repeats
            self.seen.clear()
            new = batch
        self.seen.update(a['id'] for a in new)
        self.assets += new

    def backdrop(self, asset):
        """A blurred copy from the asset's thumbhash; the small thumbnail looks blocky when stretched."""
        if asset.get('thumbhash') and ASSET_ID.fullmatch(asset['id']):
            path = os.path.join(backdrop_dir(), asset['id'] + '.png')
            if not os.path.exists(path):
                try:
                    thumbhash.write_png(path, *thumbhash.decode(asset['thumbhash']))
                except (ValueError, IndexError, OSError) as e:
                    kodi.log('thumbhash failed: {}'.format(e), xbmc.LOGWARNING)
                    return self.client.thumb_url(asset['id'])
            return path
        return self.client.thumb_url(asset['id'])

    def raw(self, asset):
        """The full asset, fetched once and quickly; None on failure so it's tried again later."""
        if asset['id'] not in self.raws:
            try:
                self.raws[asset['id']] = self.quick.asset_raw(asset['id'])
            except ApiError as e:
                kodi.log('asset lookup failed: {}'.format(e), xbmc.LOGWARNING)
                return None
        return self.raws[asset['id']]

    def ready(self, layer, now):
        """Whether the photo prepared in layer has arrived, or has been waited on long enough."""
        name, since = self.waiting.get(layer, (None, 0))
        if name is None or now - since >= LOAD_WAIT:
            return True
        for entry in xbmcgui.Window(10000).getProperty(proxy.DONE).split():
            done, _, at = entry.rpartition('@')
            if done == name and float(at) >= since - 1:
                return True
        return False

    def look_ahead(self):
        """Fetch the next photos' details in the background, so preparing them doesn't wait on the network."""
        if self.size != 'fullsize' or (self.ahead and self.ahead.is_alive()):
            return
        todo = [a for a in (self.assets[(self.index + k) % len(self.assets)] for k in range(1, AHEAD + 1))
                if a['image'] and a['id'] not in self.raws and (a.get('mime') is None or a.get('orientation') is None)]
        if todo:
            self.ahead = threading.Thread(target=lambda: [self.raw(a) for a in todo], daemon=True)
            self.ahead.start()

    def photo_size(self, asset):
        """fullsize redirects to web-safe originals; Kodi may not decode AVIF ones or turn rotated ones upright."""
        if self.size != 'fullsize' or not asset['image']:
            return 'preview'
        if asset.get('mime') is None or asset.get('orientation') is None:   # timeline months carry neither
            raw = self.raw(asset)
            if raw is None:
                return 'preview'
            asset['mime'] = raw.get('originalMimeType') or ''
            asset['orientation'] = str((raw.get('exifInfo') or {}).get('orientation') or 1)
            if not asset.get('height'):
                shaped = from_asset(raw)
                asset['width'], asset['height'] = shaped['width'], shaped['height']
        return 'fullsize' if asset['mime'] not in UNDECODABLE and asset['orientation'] in UPRIGHT else 'preview'

    def display(self, index, fade=None):
        if self.more and index >= len(self.assets) - 3:
            self.refill()
        self.index = index % len(self.assets)
        asset = self.assets[self.index]
        fade = fade or self.fade
        self.stop_video()
        self.caption(asset)
        if self.shown is not None:
            self.layers[self.shown][0].setAnimations([anim('fade', start=100, end=0, time=fade)])
        layer = self.free_layer()
        if self.prepared.get(layer, (None,))[0] != self.index:
            self.prepare(self.index, layer)
        group, back, cover, fit = self.layers[layer]
        main = cover if self.prepared[layer][1] else fit
        self.kb = None
        self.main = main if asset['image'] else None
        if self.kenburns and self.playing and asset['image']:
            self.pan_zoom(main, fade)
        elif self.pans(asset):
            rest = self.shrink(100)
            main.setAnimations([anim('zoom', start=rest, end=rest, center='960,540', time=0)])
        elif self.scale > 1:
            self.outsize(main)
            main.setAnimations([anim('zoom', start=self.shrink(100), end=self.shrink(100), center='960,540', time=0)])
        else:
            main.setAnimations([])
        group.setAnimations([anim('fade', start=0, end=100, time=fade, tween='sine', easing='inout')])
        self.shown = layer
        self.prepared.pop(layer, None)
        self.waiting.pop(layer, None)
        self.look_ahead()
        self.shown_at = time.time()
        self.preload_at = self.shown_at + fade / 1000.0 + 0.3
        self.next_at = self.shown_at + self.stay + fade / 1000.0
        if not self.playing:
            self.left = self.stay + fade / 1000.0
        if not asset['image']:
            # start once the poster is up
            self.video = {'id': asset['id'], 'at': self.shown_at + fade / 1000.0 + 0.3, 'started': False}
            self.next_at = None

    def pan_zoom(self, main, fade):
        """Slow zoom in or out around a random focal point, spanning the fades on both ends."""
        if self.pans(self.assets[self.index]):
            return self.pan(main, fade)
        top = 100 * self.top(self.assets[self.index], cover=any(main is layer[2] for layer in self.layers))
        start, end = (100, top) if random.random() < 0.6 else (top, 100)
        cx, cy = random.randint(480, 1440), random.randint(270, 810)
        if self.scale > 1:
            self.outsize(main, cx, cy)
        self.kb = {'ctrl': main, 'zoom': (start, end), 'center': '{},{}'.format(cx, cy), 'f': 0.0,
                   'easing': 'skew', 't0': time.time(), 'time': int((self.stay * 1000 + 2 * fade) * 1.1)}
        self.move()

    def pans(self, asset):
        """Panoramas fill the screen and pan across, if the original is tall enough; the rest sit on the backdrop."""
        if not (self.kenburns and asset['image'] and asset['ratio'] >= PAN_WIDE):
            return False
        tall = self.source_height(asset)
        return tall is None or tall >= gui_height()

    def source_height(self, asset):
        """The shown image's height in pixels, or None when unknown."""
        w, h = asset.get('width'), asset.get('height')
        if not (w and h):
            return None
        if asset.get('shown_size') == 'preview':      # 1440 on the short side
            h *= min(1.0, 1440.0 / min(w, h))
        return h

    def top(self, asset, cover):
        """The most this photo may zoom: the setting, but never past one original pixel per screen pixel."""
        want = self.zoom / 100.0
        h = self.source_height(asset)
        if h is None:
            return want
        shown = (cover_size if cover else fit_size)(asset['ratio'])[1] * gui_height() / 1080.0
        return min(want, max(1.0, h / shown))

    def span(self, control, ratio):
        """The whole photo cropped to fill the screen, at the pan's largest: decoded that big, only ever shrunk."""
        w, h = cover_size(ratio)
        control.setPosition(round(960 - w * self.scale / 2), round(540 - h * self.scale / 2))
        control.setWidth(round(w * self.scale))
        control.setHeight(round(h * self.scale))

    def pan(self, main, fade):
        """One eased pan at a calm speed across what cropping hides, with a gentle zoom."""
        w, h = cover_size(self.assets[self.index]['ratio'])
        ms = int((self.stay * 1000 + 2 * fade) * 1.2)
        reach = PAN_SPEED * 1920 * ms / 2000.0            # half the travel
        dx, dy = min(max(w - 1920, 0) / 2, reach), min(max(h - 1080, 0) / 2, reach)
        sign = random.choice((-1, 1))
        self.kb = {'ctrl': main, 'pan': ((-sign * dx, -sign * dy), (sign * dx, sign * dy)), 'center': '960,540',
                   'top': self.top(self.assets[self.index], cover=True),
                   'f': 0.0, 'easing': 'skew', 't0': time.time(), 'time': ms}
        self.move()

    def state(self, f):
        """The control's zoom, and slide when panning, at share f of the way."""
        kb = self.kb
        if 'pan' in kb:
            (x0, y0), (x1, y1) = kb['pan']
            return (self.shrink(100 * (1 + (kb['top'] - 1) * f)),
                    (round(x0 + (x1 - x0) * f, 3), round(y0 + (y1 - y0) * f, 3)))
        start, end = kb['zoom']
        return self.shrink(start + (end - start) * f), None

    def move(self):
        """The rest of the way: a fresh start rises fast and settles slowly, a resume just settles."""
        kb = self.kb
        if kb['easing'] == 'skew':                  # two sine legs whose speeds meet at EASE_PEAK
            split = round(kb['time'] * EASE_PEAK)
            legs = [(0.0, EASE_PEAK, 0, split, 'in'), (EASE_PEAK, 1.0, split, kb['time'] - split, 'out')]
        else:
            legs = [(kb['f'], 1.0, 0, kb['time'], kb['easing'])]
        anims = []
        for n, (a, b, delay, ms, easing) in enumerate(legs):
            (za, sa), (zb, sb) = self.state(a), self.state(b)
            if n:                                   # later legs carry on from where the first stopped
                za, zb = 100, round(100 * zb / za, 3)
                if sb is not None:
                    sa, sb = (0, 0), (round(sb[0] - sa[0], 3), round(sb[1] - sa[1], 3))
            extra = {'delay': delay} if delay else {}
            anims.append(anim('zoom', start=za, end=zb, center=kb['center'], time=ms, tween='sine', easing=easing,
                              **extra))
            if sb is not None:
                anims.append(anim('slide', start='{},{}'.format(*sa), end='{},{}'.format(*sb), time=ms,
                                  tween='sine', easing=easing, **extra))
        kb['ctrl'].setAnimations(anims)

    def outsize(self, control, cx=960, cy=540):
        """The screen scaled by the zoom around (cx, cy): the photo is decoded that big and only ever shrunk."""
        control.setPosition(round(cx * (1 - self.scale)), round(cy * (1 - self.scale)))
        control.setWidth(round(1920 * self.scale))
        control.setHeight(round(1080 * self.scale))

    def shrink(self, percent):
        """An on-screen zoom as a zoom of the outsized control."""
        return round(percent / self.scale, 3)

    def freeze(self):
        """Kodi can't pause an animation, so hold it at where the motion has got to."""
        kb = self.kb
        if not kb:
            return
        done = min((time.time() - kb['t0']) * 1000 / max(kb['time'], 1), 1.0)
        kb['f'] += (1 - kb['f']) * ease(done, kb['easing'])
        kb['time'] *= 1 - done
        kb['easing'] = 'out'                        # carries on decelerating from here
        zoom, slide = self.state(kb['f'])
        held = [anim('zoom', start=zoom, end=zoom, center=kb['center'], time=0)]
        if slide is not None:
            held.append(anim('slide', start='{},{}'.format(*slide), end='{},{}'.format(*slide), time=0))
        kb['ctrl'].setAnimations(held)

    def thaw(self):
        if self.kb is None:
            if self.kenburns and self.main is not None:   # shown while paused: start moving now
                self.pan_zoom(self.main, self.fade)
            return
        if self.kb['time'] > 0:
            self.kb['t0'] = time.time()
            self.move()

    def show_info(self):
        self.setProperty('immich.captions', 'true' if self.info else 'false')
        self.setProperty('immich.details', 'true' if self.info == 2 else '')
        if self.info == 2 and self.assets:
            self.fill_details(self.assets[self.index])

    def fill_details(self, asset):
        """Fetched only while the panel is open."""
        raw = self.raw(asset)
        if raw is None:
            self.setProperty('immich.detail', '')
            return
        lines = items.details(raw)
        rows = sum(1 + len(line) // 42 for line in lines)        # rough wrap at the panel's width
        self.getControl(301).setHeight(min(60 + 42 * rows, 560))
        self.setProperty('immich.detail', '[CR]'.join(lines))

    def caption(self, asset):
        self.setProperty('immich.date', items.when(asset['taken']))
        self.setProperty('immich.place', items.place(asset))
        total = '' if self.more else ' / {}'.format(len(self.assets))   # endless shuffle has no total
        if self.info == 2:
            self.fill_details(asset)
        self.setProperty('immich.position', '' if self.screensaver else '{}{}'.format(self.index + 1, total))
        if self.screensaver:
            self.wander()

    def wander(self):
        edge, side = CORNERS[int(time.time() // CORNER_TIME) % len(CORNERS)]   # by the clock, so short runs share them too
        self.setProperty('immich.edge', edge)
        self.setProperty('immich.side', side)
        for group in (310, 320):                    # caption and clock
            self.getControl(group).setPosition(random.randint(-DRIFT[0], DRIFT[0]), random.randint(-DRIFT[1], DRIFT[1]))

    # ---------------------------------------------------------------- video
    def play_video(self):
        url = items.video_path(self.video['id'])
        li = xbmcgui.ListItem(path=url, offscreen=True)
        li.setMimeType('video/mp4')
        li.setContentLookup(False)
        self.player.play(url, li)
        self.video.update(at=None, deadline=time.time() + 20)
        self.setProperty('immich.video', 'true')
        self.setProperty('immich.status', '')

    def stop_video(self):
        if self.video:
            started = self.video['at'] is None
            self.video = None
            self.setProperty('immich.video', '')
            self.setProperty('immich.status', '' if self.playing else kodi.L(30700))
            if started and self.player.isPlaying():
                xbmc.executebuiltin(STOP)

    def poll_video(self):
        if self.video['at'] is not None:
            if time.time() >= self.video['at']:
                self.play_video()
        elif self.player.isPlayingVideo():
            self.video['started'] = True
        elif self.video['started'] or time.time() > self.video['deadline']:
            self.stop_video()
            if self.playing:
                self.display(self.index + 1)
            else:
                self.setProperty('immich.status', kodi.L(30700))

    # ----------------------------------------------------------------- loop
    def handle(self, action):
        if action in CLOSE or self.screensaver:     # any key wakes the screensaver
            self.closed = True
        elif action in NEXT:
            self.display(self.index + 1, 500)
        elif action in PREV:
            self.display(self.index - 1, 500)
        elif action in PAUSE and self.video:
            if self.video['at'] is None:                # started; pause the player itself
                xbmc.executebuiltin(PLAY_PAUSE)
                self.video['paused'] = not self.video.get('paused')
                self.setProperty('immich.status', kodi.L(30700) if self.video['paused'] else '')
        elif action in PAUSE:
            now = time.time()
            self.playing = not self.playing
            self.setProperty('immich.status', '' if self.playing else kodi.L(30700))
            if self.playing:
                self.thaw()
                self.next_at = now + max(self.left, 1.0)
            else:
                self.freeze()
                self.left = (self.next_at - now) if self.next_at else self.stay
        elif action in INFO:
            self.info = (self.info + 1) % 3
            self.show_info()

    def run(self):
        self.setup()
        if self.player.isPlayingVideo() and not self.screensaver:
            xbmc.executebuiltin(STOP)
        if not self.playing:
            self.setProperty('immich.status', kodi.L(30700))
        monitor = self.monitor
        self.prepare(self.index, 0)
        monitor.waitForAbort(0.8)            # give the first image a head start
        self.display(self.index)
        while not self.closed and not monitor.abortRequested():
            if monitor.waitForAbort(0.1):
                break
            while self.actions and not self.closed:
                self.handle(self.actions.pop(0))
            if self.closed:
                break
            now = time.time()
            if self.video:
                self.poll_video()
            elif self.playing and self.next_at and now >= self.next_at and self.ready(self.free_layer(), now):
                self.display(self.index + 1)
            if self.shown is not None and self.preload_at and now >= self.preload_at:
                self.preload_at = None
                nxt = (self.index + 1) % len(self.assets)
                self.prepare(nxt, self.free_layer())
        self.stop_video()


def backdrop_dir():
    return kodi.ensure_dir(os.path.join(kodi.PROFILE, 'backdrops'))


def play(client, assets, start=0, autoplay=True, more=None, screensaver=False):
    if screensaver or not kodi.setting_bool('videos'):
        current = assets[start]['id'] if 0 <= start < len(assets) else None
        assets = [a for a in assets if a['image']]
        start = next((i for i, a in enumerate(assets) if a['id'] == current), 0)
    if not assets:
        if not screensaver:
            kodi.notify(kodi.L(30701))
        return
    window = Viewer('script-immich-viewer.xml', kodi.ADDON_PATH, 'default', '1080i', client=client, assets=assets,
                    start=start, autoplay=autoplay, more=more, screensaver=screensaver)
    if not screensaver:
        xbmc.executebuiltin('InhibitScreensaver(true)')
    window.show()
    try:
        window.run()
    finally:
        window.close()
        window.monitor = None                       # breaks the Waker cycle, so Kodi frees the window
        del window
        if not screensaver:
            xbmc.executebuiltin('InhibitScreensaver(false)')
        shutil.rmtree(backdrop_dir(), ignore_errors=True)

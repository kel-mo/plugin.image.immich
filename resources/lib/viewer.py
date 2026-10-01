# -*- coding: utf-8 -*-
"""Full-screen viewer: Ken Burns pan and zoom, crossfades, captions and inline video."""
import os
import random
import re
import shutil
import time

import xbmc
import xbmcaddon
import xbmcgui
import xbmcvfs

from . import items, kodi, thumbhash
from .api import ApiError, ImmichClient

SCREEN = 1920 / 1080
CLOSE = {9, 10, 13, 92}                  # parent dir, previous menu, stop, back
PAUSE = {7, 12, 79, 229}                 # select, pause, play, play/pause
NEXT = {2, 14, 77}                       # right, next item, fast forward
PREV = {1, 15, 78}                       # left, previous item, rewind
INFO = {11}
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


def anim(effect, **attrs):
    attrs.setdefault('condition', 'true')
    if attrs.get('time'):
        attrs['time'] = int(attrs['time'] / skin_slowdown())   # real milliseconds on any skin
    return ('conditional', 'effect={} {}'.format(effect, ' '.join('{}={}'.format(k, v) for k, v in attrs.items())))


class Viewer(xbmcgui.WindowXMLDialog):
    def __init__(self, *args, **kwargs):
        super().__init__(*args)
        self.client = kwargs['client']
        self.assets = kwargs['assets']
        self.index = kwargs.get('start', 0)
        self.playing = kwargs.get('autoplay', True)
        self.more = kwargs.get('more')              # fetches another batch for endless shuffle
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
        self.kenburns = kodi.setting_bool('kenburns')
        self.zoom = min(max(kodi.setting_int('zoom'), 100), 125)
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
        self.monitor = xbmc.Monitor()
        self.show_info()
        self.setProperty('immich.clock', 'true' if kodi.setting_bool('clock') else 'false')
        self.layers = [[self.getControl(base + k) for k in range(4)] for base in (100, 200)]
        for layer in self.layers:
            layer[0].setAnimations([anim('fade', start=0, end=0, time=0)])
        self.shown = None                   # layer on screen
        self.prepared = {}                  # layer -> (asset index, fills screen)
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
        url = self.client.thumb_url(asset['id'], self.photo_size(asset))
        for control in (back, cover, fit):          # Kodi shows the old texture until the new one loads
            control.setImage('')
        if self.scale > 1:
            for control in (cover, fit):            # Kodi decodes to the control's size
                self.outsize(control)
        self.monitor.waitForAbort(0.05)
        full = abs(asset['ratio'] - SCREEN) < 0.3
        if full:
            fit.setImage('')
            back.setImage('')
            cover.setImage(url, False)
        else:
            cover.setImage('')
            back.setImage(self.backdrop(asset), False)
            fit.setImage(url, False)
        self.prepared[layer] = (index, full)

    def refill(self):
        try:
            batch = self.more()
        except ApiError as e:
            kodi.log('shuffle refill failed: {}'.format(e), xbmc.LOGWARNING)
            return
        if not kodi.setting_bool('videos'):
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
        elif self.scale > 1:
            self.outsize(main)
            main.setAnimations([anim('zoom', start=self.shrink(100), end=self.shrink(100), center='960,540', time=0)])
        else:
            main.setAnimations([])
        group.setAnimations([anim('fade', start=0, end=100, time=fade, tween='sine', easing='inout')])
        self.shown = layer
        self.prepared.pop(layer, None)
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
        start, end = (100, self.zoom) if random.random() < 0.6 else (self.zoom, 100)
        cx, cy = random.randint(480, 1440), random.randint(270, 810)
        if self.scale > 1:
            self.outsize(main, cx, cy)
        self.kb = {'ctrl': main, 'start': start, 'end': end, 't0': time.time(),
                   'center': '{},{}'.format(cx, cy), 'time': int((self.stay * 1000 + 2 * fade) * 1.1)}
        self.zoom_to(self.kb['start'], self.kb['time'])

    def outsize(self, control, cx=960, cy=540):
        """The screen scaled by the zoom around (cx, cy): the photo is decoded that big and only ever shrunk."""
        control.setPosition(round(cx * (1 - self.scale)), round(cy * (1 - self.scale)))
        control.setWidth(round(1920 * self.scale))
        control.setHeight(round(1080 * self.scale))

    def shrink(self, percent):
        """An on-screen zoom as a zoom of the outsized control."""
        return round(percent / self.scale, 3)

    def zoom_to(self, start, ms):
        kb = self.kb
        kb['ctrl'].setAnimations([anim('zoom', start=self.shrink(start), end=self.shrink(kb['end']),
                                       center=kb['center'], time=int(ms), tween='linear')])

    def freeze(self):
        """Kodi can't pause an animation, so hold it at where the linear zoom has got to."""
        kb = self.kb
        if not kb:
            return
        done = min((time.time() - kb['t0']) * 1000 / max(kb['time'], 1), 1.0)
        kb['start'] += (kb['end'] - kb['start']) * done
        kb['time'] *= 1 - done
        kb['ctrl'].setAnimations([anim('zoom', start=self.shrink(kb['start']), end=self.shrink(kb['start']),
                                       center=kb['center'], time=0)])

    def thaw(self):
        if self.kb is None:
            if self.kenburns and self.main is not None:   # shown while paused: start moving now
                self.pan_zoom(self.main, self.fade)
            return
        if self.kb['time'] > 0:
            self.kb['t0'] = time.time()
            self.zoom_to(self.kb['start'], self.kb['time'])

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
        self.setProperty('immich.position', '{}{}'.format(self.index + 1, total))

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
        if action in CLOSE:
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
        if self.player.isPlayingVideo():
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
            elif self.playing and self.next_at and now >= self.next_at:
                self.display(self.index + 1)
            if self.shown is not None and self.preload_at and now >= self.preload_at:
                self.preload_at = None
                nxt = (self.index + 1) % len(self.assets)
                self.prepare(nxt, self.free_layer())
        self.stop_video()


def backdrop_dir():
    return kodi.ensure_dir(os.path.join(kodi.PROFILE, 'backdrops'))


def play(client, assets, start=0, autoplay=True, more=None):
    if not kodi.setting_bool('videos'):
        current = assets[start]['id'] if 0 <= start < len(assets) else None
        assets = [a for a in assets if a['image']]
        start = next((i for i, a in enumerate(assets) if a['id'] == current), 0)
    if not assets:
        kodi.notify(kodi.L(30701))
        return
    window = Viewer('script-immich-viewer.xml', kodi.ADDON_PATH, 'default', '1080i',
                    client=client, assets=assets, start=start, autoplay=autoplay, more=more)
    xbmc.executebuiltin('InhibitScreensaver(true)')
    window.show()
    try:
        window.run()
    finally:
        window.close()
        del window
        xbmc.executebuiltin('InhibitScreensaver(false)')
        shutil.rmtree(backdrop_dir(), ignore_errors=True)

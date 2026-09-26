# -*- coding: utf-8 -*-
"""Full-screen viewer: Ken Burns pan and zoom, crossfades, captions and inline video."""
import os
import random
import shutil
import time

import xbmc
import xbmcgui

from . import items, kodi, thumbhash
from .api import ApiError

SCREEN = 1920 / 1080
CLOSE = {9, 10, 13, 92}                  # parent dir, previous menu, stop, back
PAUSE = {7, 12, 79, 229}                 # select, pause, play, play/pause
NEXT = {2, 14, 77}                       # right, next item, fast forward
PREV = {1, 15, 78}                       # left, previous item, rewind
INFO = {11}
PORTABLE = {'image/jpeg', 'image/png', 'image/gif', 'image/webp', 'image/bmp'}   # every Kodi decodes these


def anim(effect, **attrs):
    attrs.setdefault('condition', 'true')
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
        self.setProperty('immich.captions', 'true' if kodi.setting_bool('captions') else 'false')
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
        if asset.get('thumbhash'):
            path = os.path.join(backdrop_dir(), asset['id'] + '.png')
            if not os.path.exists(path):
                try:
                    thumbhash.write_png(path, *thumbhash.decode(asset['thumbhash']))
                except (ValueError, IndexError, OSError) as e:
                    kodi.log('thumbhash failed: {}'.format(e), xbmc.LOGWARNING)
                    return self.client.thumb_url(asset['id'])
            return path
        return self.client.thumb_url(asset['id'])

    def photo_size(self, asset):
        """fullsize redirects to the original, which Kodi may not decode (AVIF on LibreELEC)."""
        if self.size != 'fullsize' or not asset['image']:
            return 'preview'
        if 'mime' not in asset:                     # timeline months don't carry the file type
            try:
                asset['mime'] = self.client.asset(asset['id']).get('mime')
            except ApiError:
                asset['mime'] = None
        return 'fullsize' if asset['mime'] in PORTABLE else 'preview'

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
        self.kb = {'ctrl': main, 'start': start, 'end': end, 't0': time.time(),
                   'center': '{},{}'.format(random.randint(480, 1440), random.randint(270, 810)),
                   'time': int((self.stay * 1000 + 2 * fade) * 1.1)}
        self.zoom_to(self.kb['start'], self.kb['time'])

    def zoom_to(self, start, ms):
        kb = self.kb
        kb['ctrl'].setAnimations([anim('zoom', start=round(start, 3), end=kb['end'], center=kb['center'],
                                       time=int(ms), tween='linear')])

    def freeze(self):
        """Kodi can't pause an animation, so hold it at where the linear zoom has got to."""
        kb = self.kb
        if not kb:
            return
        done = min((time.time() - kb['t0']) * 1000 / max(kb['time'], 1), 1.0)
        kb['start'] += (kb['end'] - kb['start']) * done
        kb['time'] *= 1 - done
        kb['ctrl'].setAnimations([anim('zoom', start=round(kb['start'], 3), end=round(kb['start'], 3),
                                       center=kb['center'], time=0)])

    def thaw(self):
        if self.kb is None:
            if self.kenburns and self.main is not None:   # shown while paused: start moving now
                self.pan_zoom(self.main, self.fade)
            return
        if self.kb['time'] > 0:
            self.kb['t0'] = time.time()
            self.zoom_to(self.kb['start'], self.kb['time'])

    def caption(self, asset):
        self.setProperty('immich.date', items.when(asset['taken']))
        self.setProperty('immich.place', items.place(asset))
        total = '' if self.more else ' / {}'.format(len(self.assets))   # endless shuffle has no total
        self.setProperty('immich.position', '{}{}'.format(self.index + 1, total))

    # ---------------------------------------------------------------- video
    def play_video(self):
        url = self.client.video_url(self.video['id'])
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
            if started and self.player.isPlaying():
                self.player.stop()

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
        elif action in PAUSE and not self.video:
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
            on = self.getProperty('immich.captions') != 'true'
            self.setProperty('immich.captions', 'true' if on else 'false')

    def run(self):
        self.setup()
        if self.player.isPlaying():
            self.player.stop()
        if not self.playing:
            self.setProperty('immich.status', kodi.L(30700))
        monitor = xbmc.Monitor()
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

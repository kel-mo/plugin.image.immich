# -*- coding: utf-8 -*-
"""Background service: runs the media proxy while Kodi is up, and draws the tiles each day."""
import traceback

import xbmc

from . import kodi, proxy, tiles
from .api import ImmichClient, clean_url

TILE_START = 30                          # seconds after start before drawing tiles, so Kodi settles first
TILE_CHECK = 60                          # seconds between checks for a new day or the setting turned on
TILE_RETRY = 900                         # seconds to wait after the server gave no photos
TILE_TIMEOUT = 4                         # seconds per tile request: one under way ends inside Kodi's five-second wait at exit


class Monitor(xbmc.Monitor):
    def __init__(self, server):
        super().__init__()
        self.server = server
        self.onSettingsChanged()

    def onSettingsChanged(self):
        self.server.upstream = (clean_url(kodi.fresh_setting('server_url')), kodi.fresh_setting('api_key'))


def run():
    server = proxy.start()
    if server is None:
        return
    monitor = Monitor(server)
    try:
        proxy.forget_keyed_textures()
        proxy.forget_cached_photos()
        try:
            tiles.repair_favourites(monitor)
        except Exception:
            kodi.log('favourites not repaired: {}'.format(traceback.format_exc()), xbmc.LOGWARNING)
        wait = TILE_START
        while not monitor.waitForAbort(wait):
            wait = TILE_CHECK
            url, key = kodi.fresh_setting('server_url'), kodi.fresh_setting('api_key')
            if url and key and tiles.due():
                try:
                    if not tiles.refresh(ImmichClient(url, key, timeout=TILE_TIMEOUT), monitor):
                        wait = TILE_RETRY
                except Exception:                   # never take the proxy down with it
                    kodi.log('tiles failed: {}'.format(traceback.format_exc()), xbmc.LOGWARNING)
                    wait = TILE_RETRY
    finally:
        proxy.stop(server)

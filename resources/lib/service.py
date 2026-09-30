# -*- coding: utf-8 -*-
"""Background service: runs the media proxy while Kodi is up."""
import xbmc

from . import kodi, proxy
from .api import clean_url


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
        monitor.waitForAbort()
    finally:
        proxy.stop(server)

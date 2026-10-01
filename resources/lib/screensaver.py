# -*- coding: utf-8 -*-
"""Ken Burns screensaver: the slideshow viewer, photos only, until Kodi wakes up."""
import random

import xbmc

from . import kodi, plugin, viewer
from .api import ApiError, ImmichClient

SOURCES = ('memories', 'random', 'favourites')


def photos(client, source, **params):
    return [a for a in plugin.source_assets(client, dict(params, source=source)) if a['image']]


def run():
    try:
        client = ImmichClient()
        source = SOURCES[min(max(kodi.setting_int('screensaver_source'), 0), len(SOURCES) - 1)]
        assets = photos(client, source)
        if not assets and source != 'random':      # nothing on this day or no favourites
            source, assets = 'random', photos(client, 'random')
    except ApiError as e:
        kodi.log('screensaver: {}'.format(e), xbmc.LOGWARNING)
        return
    random.shuffle(assets)
    viewer.play(client, assets, more=client.random if source == 'random' else None, screensaver=True)

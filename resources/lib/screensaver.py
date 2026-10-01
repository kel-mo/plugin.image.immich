# -*- coding: utf-8 -*-
"""Ken Burns screensaver: the slideshow viewer, photos only, until Kodi wakes up."""
import random

import xbmc

from . import kodi, plugin, viewer
from .api import ApiError, ImmichClient

SOURCES = ('memories', 'random', 'favourites', 'search')


def photos(client, source, **params):
    return [a for a in plugin.source_assets(client, dict(params, source=source)) if a['image']]


def run():
    try:
        client = ImmichClient()
        source = SOURCES[min(max(kodi.setting_int('screensaver_source'), 0), len(SOURCES) - 1)]
        query = kodi.setting('screensaver_query').strip()
        assets = photos(client, source, query=query) if source != 'search' or query else []
        if not assets and source != 'random':      # nothing on this day, no favourites or no matches
            source, assets = 'random', photos(client, 'random')
    except ApiError as e:
        kodi.log('screensaver: {}'.format(e), xbmc.LOGWARNING)
        return
    random.shuffle(assets)
    viewer.play(client, assets, more=client.random if source == 'random' else None, screensaver=True)

# -*- coding: utf-8 -*-
"""Ken Burns screensaver: the slideshow viewer, photos only, until Kodi wakes up."""
import random

import xbmc

from . import kodi, plugin, viewer
from .api import ApiError, ImmichClient

SOURCES = ('memories', 'random', 'favourites', 'search', 'albums', 'random_album')


def photos(client, source, **params):
    return [a for a in plugin.source_assets(client, dict(params, source=source)) if a['image']]


def album_photos(client):
    found = {}
    for album_id in filter(None, kodi.setting('screensaver_albums').split(',')):
        try:
            found.update((a['id'], a) for a in photos(client, 'album', album_id=album_id))
        except ApiError as e:                   # album gone since it was picked
            kodi.log('screensaver: album {}: {}'.format(album_id, e), xbmc.LOGWARNING)
    return list(found.values())


def random_album_photos(client):
    albums = [a for a in client.albums() if a.get('assetCount')]
    random.shuffle(albums)
    for a in albums:                            # some hold only videos
        found = photos(client, 'album', album_id=a['id'])
        if found:
            kodi.debug('screensaver: album {}'.format(a.get('albumName')))
            return found
    return []


def run():
    try:
        client = ImmichClient()
        source = SOURCES[min(max(kodi.setting_int('screensaver_source'), 0), len(SOURCES) - 1)]
        query = kodi.setting('screensaver_query').strip()
        if source == 'albums':
            assets = album_photos(client)
        elif source == 'random_album':
            assets = random_album_photos(client)
        else:
            assets = photos(client, source, query=query) if source != 'search' or query else []
        if not assets and source != 'random':      # nothing on this day, no favourites, matches or albums
            source, assets = 'random', photos(client, 'random')
    except ApiError as e:
        kodi.log('screensaver: {}'.format(e), xbmc.LOGWARNING)
        return
    random.shuffle(assets)
    viewer.play(client, assets, more=client.random if source == 'random' else None, screensaver=True)

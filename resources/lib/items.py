# -*- coding: utf-8 -*-
"""ListItems for Immich assets."""
import xbmc
import xbmcgui

_formats = {}


def region(key):
    if key not in _formats:
        _formats[key] = xbmc.getRegion(key) or {'dateshort': '%d/%m/%Y', 'time': '%H:%M'}.get(key, '')
    return _formats[key]


def when(taken):
    if not taken:
        return ''
    clock = region('time').replace(':%S', '')
    return taken.strftime('{} {}'.format(region('dateshort'), clock))


def place(asset):
    return ', '.join(p for p in (asset.get('city'), asset.get('country')) if p)


def asset_item(client, asset, path=None):
    """path: what Kodi opens on click; defaults to the image or video itself."""
    label = when(asset['taken']) or asset['id']
    thumb = client.thumb_url(asset['id'])
    if asset['image']:
        url = client.thumb_url(asset['id'], 'preview')
    else:
        url = client.video_url(asset['id'])
    li = xbmcgui.ListItem(label, place(asset), path=path or url, offscreen=True)
    li.setArt({'thumb': thumb, 'icon': thumb})
    li.setContentLookup(False)
    if asset['taken']:
        li.setDateTime(asset['taken'].strftime('%Y-%m-%dT%H:%M:%S'))
    if asset['image']:
        li.setMimeType('image/jpeg')
        tag = li.getPictureInfoTag()
        if asset['taken']:
            tag.setDateTimeTaken(asset['taken'].strftime('%Y-%m-%dT%H:%M:%S'))
    else:
        li.setMimeType('video/mp4')
        tag = li.getVideoInfoTag()
        tag.setTitle(label)
        tag.setMediaType('video')
        if asset['duration']:
            tag.setDuration(asset['duration'])
    li.setProperty('immich.id', asset['id'])
    return li, url

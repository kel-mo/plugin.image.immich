# -*- coding: utf-8 -*-
"""ListItems for Immich assets."""
import xbmc
import xbmcgui

from . import kodi

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


def video_path(asset_id):
    """Kodi skips its file date check for plugin paths; answering it from the proxy can stall Kodi."""
    return 'plugin://{}/?action=video&id={}'.format(kodi.ADDON_ID, asset_id)


def place(asset):
    return ', '.join(p for p in (asset.get('city'), asset.get('country')) if p)


def asset_item(client, asset, path=None):
    """path: what Kodi opens on click; defaults to the image or video itself."""
    label = when(asset['taken']) or asset['id']
    thumb = client.thumb_url(asset['id'])
    if asset['image']:
        url = client.thumb_url(asset['id'], 'preview')
    else:
        url = video_path(asset['id'])
    li = xbmcgui.ListItem(label, place(asset), path=path or url, offscreen=True)
    li.setArt({'thumb': thumb, 'icon': thumb})
    li.setContentLookup(False)
    li.setProperty('immich.id', asset['id'])
    if asset['taken']:
        li.setDateTime(asset['taken'].strftime('%Y-%m-%dT%H:%M:%S'))
    if asset['image']:
        if path:
            return li, url                      # not a picture to Kodi, or its slideshow grabs it
        li.setMimeType('image/jpeg')
        tag = li.getPictureInfoTag()
        if asset['taken']:
            tag.setDateTimeTaken(asset['taken'].strftime('%Y-%m-%dT%H:%M:%S'))
    else:
        li.setMimeType('video/mp4')
        li.setProperty('IsPlayable', 'true')
        tag = li.getVideoInfoTag()
        tag.setTitle(label)
        tag.setMediaType('video')
        if asset['duration']:
            tag.setDuration(asset['duration'])
    return li, url


def _num(value):
    return ('{:.1f}'.format(value)).rstrip('0').rstrip('.')


def details(raw):
    """Lines for the viewer's info panel from a full /assets/{id} response."""
    exif = raw.get('exifInfo') or {}
    lines = []
    where = ', '.join(p for p in (exif.get('city'), exif.get('state'), exif.get('country')) if p)
    if where:
        lines.append(where)
    names = [p['name'] for p in raw.get('people') or [] if p.get('name')]
    if names:
        lines.append(', '.join(names))
    make, model = (exif.get('make') or '').strip(), (exif.get('model') or '').strip()
    camera = model if model.lower().startswith(make.lower()) else ' '.join(p for p in (make, model) if p)
    if camera:
        lines.append(camera)
    lens = (exif.get('lensModel') or '').strip()
    for prefix in (camera, model):
        if prefix and lens.lower().startswith(prefix.lower()):
            lens = lens[len(prefix):].strip()      # 'Pixel 9 Pro XL back camera' -> 'back camera'
            break
    if lens and lens not in camera:
        lines.append(lens)
    shot = []
    if exif.get('fNumber'):
        shot.append('\u0192/' + _num(exif['fNumber']))
    if exif.get('exposureTime'):
        shot.append('{} s'.format(exif['exposureTime']))
    if exif.get('iso'):
        shot.append('ISO {}'.format(exif['iso']))
    if exif.get('focalLength'):
        shot.append('{} mm'.format(_num(exif['focalLength'])))
    if shot:
        lines.append(' \u00b7 '.join(shot))
    width, height = exif.get('exifImageWidth') or raw.get('width'), exif.get('exifImageHeight') or raw.get('height')
    size = []
    if width and height:
        size.append('{} \u00d7 {} ({} MP)'.format(width, height, _num(width * height / 1e6)))
    if exif.get('fileSizeInByte'):
        size.append('{} MB'.format(_num(exif['fileSizeInByte'] / 1e6)))
    if size:
        lines.append(' \u00b7 '.join(size))
    if raw.get('originalFileName'):
        lines.append(raw['originalFileName'])
    if (exif.get('description') or '').strip():
        lines += ['', exif['description'].strip()]
    return lines

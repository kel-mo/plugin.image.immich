# -*- coding: utf-8 -*-
"""Menu tiles: a darkened photo with the folder's name over it, drawn daily."""
import io
import os
import random
import re
import time
import xml.etree.ElementTree as ET
from datetime import date

import xbmcvfs

from . import kodi
from .api import ApiError, from_asset

SIZE = 512                               # square, as Estuary shows folders
DIM = 0.5                                # photo brightness behind the name
SHADOW = 200                             # darkness of the halo round the name, of 255
FOLDERS = {'timeline': 30000, 'memories': 30017, 'people': 30012, 'places': 30014,
           'favourites': 30021, 'albums': 30001, 'search_menu': 30022, 'shuffle': 30026}   # shuffle: Shuffle everything
FONT_DIRS = ('special://skin/fonts', 'special://home/media/Fonts', 'special://xbmc/media/Fonts')


def tile_dir():
    return os.path.join(kodi.PROFILE, 'tiles')


def drawn(key):
    """Every drawing of a tile, oldest first; each has its own name, as Kodi keeps showing a file it has loaded."""
    try:
        names = os.listdir(tile_dir())
    except OSError:
        return []
    return sorted(os.path.join(tile_dir(), n) for n in names if re.fullmatch(re.escape(key) + r'-\d+\.jpg', n))


def current(key):
    found = drawn(key)
    return found[-1] if found else None


def art(key):
    """The tile when there is one, else the add-on icon."""
    tile = current(key)
    return tile if tile and kodi.setting_bool('tiles') else kodi.ICON


# ------------------------------------------------------------------ photos
def memories(client):
    return [a for _, assets in client.memories(date.today()) for a in assets]


def favourites(client):
    return client.random(size=50, isFavorite=True)


def anything(client):
    return client.random(size=50)


def latest(client):
    """Photos from the most recent day in the timeline."""
    found = [a for a in client.search_page(1, 50, visibility='timeline')[0] if a.get('image') and a.get('taken')]
    newest = max((a['taken'].date() for a in found), default=None)
    return [a for a in found if a['taken'].date() == newest]


def album(client):
    """The newest album's cover."""
    albums = [a for a in client.albums() if a.get('albumThumbnailAssetId') and a.get('assetCount')]
    albums.sort(key=lambda a: a.get('endDate') or a.get('updatedAt') or '', reverse=True)
    return [{'id': albums[0]['albumThumbnailAssetId'], 'image': True}] if albums else []


def person(client):
    """Photos of the first person Immich lists."""
    people = client.people()
    return client.search_page(1, 50, personIds=[people[0]['id']])[0] if people else []


def places(client):
    return [from_asset(a) for a in client.cities()]


SOURCES = {'memories': (memories, anything), 'favourites': (favourites, anything), 'albums': (album, anything),
           'people': (person, anything), 'places': (places, anything), 'timeline': (latest, anything)}   # the rest: anything


def pick(client, key, day, skip=()):
    """A photo for the tile from the first source with one no other tile shows, else a shared one;
    the same one all day where it can."""
    rnd = random.Random('{}:{}'.format(day, key))
    spare = None
    for source in SOURCES.get(key, (anything,)):
        try:
            found = sorted((a for a in source(client) if a.get('image')), key=lambda a: a['id'])
        except ApiError as e:
            kodi.debug('tile {}: {} failed: {}'.format(key, source.__name__, e))
            continue
        fresh = [a for a in found if a['id'] not in skip]
        if fresh:
            return rnd.choice(fresh)
        spare = spare or found
    return rnd.choice(spare) if spare else None


# ----------------------------------------------------------------- drawing
def font_file():
    """The current skin's font, bold if it ships one: (path or None, bold)."""
    skin = xbmcvfs.translatePath('special://skin/')
    fonts = []
    for sub in sorted(os.listdir(skin)) if os.path.isdir(skin) else []:
        try:
            sets = ET.parse(os.path.join(skin, sub, 'Font.xml')).getroot().findall('fontset')
        except (ET.ParseError, OSError):
            continue
        chosen = next((s for s in sets if s.get('id', '').lower() == 'default'), sets[0] if sets else None)
        if chosen is not None:
            fonts = [(f.findtext('name') or '', f.findtext('filename') or '') for f in chosen.iter('font')]
            break
    files = [fn for _, fn in fonts if fn]
    tries = [(fn, True) for fn in files if re.search('bold|black|heavy', fn, re.I)]
    tries += [(fn, False) for name, fn in fonts if name == 'font13' and fn] + [(fn, False) for fn in files]
    for fn, bold in tries + [('arial.ttf', False)]:
        for d in FONT_DIRS:
            found = os.path.join(xbmcvfs.translatePath(d), fn)
            if os.path.isfile(found):
                return found, bold
    return None, False


def font(size):
    """The skin's font at this size, and the stroke that thickens a regular one."""
    from PIL import ImageFont
    file, bold = font_file()
    return (ImageFont.truetype(file, size) if file else ImageFont.load_default(size)), (0 if bold else max(1, size // 24))


def lines(draw, label, face, stroke):
    """The label as it fits 80 % of the width: one line, else two split where the longer is shortest; None if not."""
    width = lambda text: max(draw.textbbox((0, 0), t, font=face, stroke_width=stroke)[2] for t in text.split('\n'))
    words = label.split()
    tries = [label] + [' '.join(words[:i]) + '\n' + ' '.join(words[i:]) for i in range(1, len(words))]
    best = min(tries, key=lambda text: (width(text) > SIZE * 0.8, text.count('\n'), width(text)))
    return best if width(best) <= SIZE * 0.8 else None


def text_size(labels):
    """One size for every tile, so the names match: the largest at which each fits, wrapped onto two lines if need be."""
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return SIZE // 5
    draw = ImageDraw.Draw(Image.new('L', (1, 1)))
    size = SIZE // 5
    while size > 12:
        face, stroke = font(size)
        if all(lines(draw, label, face, stroke) for label in labels):
            break
        size = int(size * 0.9)
    return size


def halo(xy, text, face, stroke, size):
    """A soft dark halo, so bright pictures stay readable: the text as drawn, widened, then blurred.
    Widened afterwards, as a thicker stroke would space wrapped lines further apart than the text's."""
    from PIL import Image, ImageDraw, ImageFilter
    mask = Image.new('L', (SIZE, SIZE))
    ImageDraw.Draw(mask).text(xy, text, font=face, fill=SHADOW, stroke_width=stroke, stroke_fill=SHADOW, align='center')
    return mask.filter(ImageFilter.MaxFilter(2 * (size // 12) + 1)).filter(ImageFilter.GaussianBlur(size / 6))


def render(data, label, dest, size=SIZE // 5):
    """A square crop, darkened, with the label centred; the plain photo when Kodi's Python lacks PIL."""
    tmp = dest + '.tmp'
    try:
        from PIL import Image, ImageDraw, ImageEnhance, ImageOps
    except ImportError:
        with open(tmp, 'wb') as f:
            f.write(data)
        os.replace(tmp, dest)
        return
    im = ImageOps.exif_transpose(Image.open(io.BytesIO(data))).convert('RGB')
    im = ImageEnhance.Brightness(ImageOps.fit(im, (SIZE, SIZE), Image.LANCZOS)).enhance(DIM)
    draw = ImageDraw.Draw(im)
    face, stroke = font(size)
    text = lines(draw, label, face, stroke) or label
    left, top, right, bottom = draw.textbbox((0, 0), text, font=face, stroke_width=stroke, align='center')
    xy = ((SIZE - (right - left)) / 2 - left, (SIZE - (bottom - top)) / 2 - top)
    im.paste((0, 0, 0), mask=halo(xy, text, face, stroke, size))
    draw.text(xy, text, font=face, fill=(255, 255, 255), stroke_width=stroke, stroke_fill=(255, 255, 255), align='center')
    im.save(tmp, 'JPEG', quality=88)
    os.replace(tmp, dest)


# ------------------------------------------------------------------- refresh
def stamp():
    return os.path.join(tile_dir(), 'drawn')


def drawing(day=None):
    """What the stamp records: the day, and the version, so an update redraws at once."""
    return '{} {}'.format(day or date.today().isoformat(), kodi.ADDON_VERSION)


def due():
    """Tiles on and not yet drawn today by this version."""
    if kodi.fresh_setting('tiles') != 'true':
        return False
    try:
        with open(stamp()) as f:
            return f.read().strip() != drawing()
    except OSError:
        return True


def refresh(client, monitor):
    """Draw every tile anew and drop old drawings; how many were drawn."""
    kodi.ensure_dir(tile_dir())
    day = date.today().isoformat()
    name = int(time.time())
    done, used = 0, set()
    labels = [(k, kodi.L(i)) for k, i in FOLDERS.items()]
    if not all(label for _, label in labels):           # Kodi has this version's strings only after a restart
        kodi.debug('tiles: names not loaded yet')
        return 0
    size = text_size([label for _, label in labels])
    for key, label in labels:
        if monitor.abortRequested():
            return done
        asset = pick(client, key, day, used)
        if asset is None:
            continue
        used.add(asset['id'])
        try:
            render(client.image(asset['id']), label, os.path.join(tile_dir(), '{}-{}.jpg'.format(key, name)), size)
            done += 1
        except (ApiError, OSError, ValueError) as e:        # PIL raises OSError for unreadable images
            kodi.debug('tile {}: {}'.format(key, e))
    if done:
        with open(stamp(), 'w') as f:
            f.write(drawing(day))                         # the day it started, should it run past midnight
        tidy()
    kodi.log('tiles: drew {} of {}'.format(done, len(FOLDERS)))
    return done


def tidy():
    """Delete drawings no longer shown: all but each folder's newest, and those of tiles since dropped."""
    keep = {current(key) for key in FOLDERS} | {stamp()}
    for name in os.listdir(tile_dir()):
        if os.path.join(tile_dir(), name) not in keep:
            os.remove(os.path.join(tile_dir(), name))
    forget()


def forget():
    """Drop Kodi's cached copies of our tiles; it keeps them for a day and would never look at deleted ones again."""
    found = kodi.jsonrpc('Textures.GetTextures', properties=['url'],                 # Kodi keys plain files by path
                         filter={'field': 'url', 'operator': 'contains', 'value': '{}/tiles/'.format(kodi.ADDON_ID)})
    for t in (found or {}).get('textures') or []:
        kodi.jsonrpc('Textures.RemoveTexture', textureid=t['textureid'])


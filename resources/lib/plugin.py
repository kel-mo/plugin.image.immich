# -*- coding: utf-8 -*-
"""plugin:// router and directory listings."""
import json
import os
import random
import traceback
from datetime import date, datetime
from urllib.parse import parse_qsl, urlencode

import xbmc
import xbmcgui
import xbmcplugin

from . import items, kodi, signin, tiles, viewer
from .api import ApiError, AuthError, ImmichClient

BASE = 'plugin://{}/'.format(kodi.ADDON_ID)
HANDLE = -1
SEARCH_PAGE = 100                        # matches get weaker further down
RECENT = 10
PROBE_TIMEOUT = 5                        # root menu checks; the lists themselves wait longer
TIMELINE = 'timeline'                    # leaves out archived photos and live-photo clips
NO_COUNTRY = '-'                         # empty values drop out of plugin URLs


def url_for(action, **params):
    params['action'] = action
    return BASE + '?' + urlencode({k: v for k, v in params.items() if v is not None and v != ''})


def run_plugin(action, **params):
    return 'RunPlugin({})'.format(url_for(action, **params))


def end(succeeded=True, cache_to_disc=False):
    if HANDLE >= 0:                              # RunPlugin calls have no listing
        xbmcplugin.endOfDirectory(HANDLE, succeeded, cacheToDisc=cache_to_disc)


def folder(label, action, icon=None, art=None, context=None, label2='', **params):
    li = xbmcgui.ListItem(label, label2, offscreen=True)
    art = dict(art or {})
    if icon:
        art.setdefault('icon', icon)
        art.setdefault('thumb', icon)
    if art:
        li.setArt(art)
    if context:
        li.addContextMenuItems(context)
    xbmcplugin.addDirectoryItem(HANDLE, url_for(action, **params), li, isFolder=True)


def count(n, one=30009, many=30010):
    return kodi.L(one) if n == 1 else kodi.L(many, n)


def photo_art(client, asset_id):
    """A photo as a folder's thumbnail and backdrop; nothing without one, so the icon stands in."""
    if not asset_id:
        return {}
    thumb = client.thumb_url(asset_id)
    return {'thumb': thumb, 'icon': thumb, 'fanart': client.fanart_url(asset_id)}


def covers(client, wanted, buckets):
    """A photo for each wanted timeline bucket, its newest: kept in a cache keyed on the bucket's count,
    so a listing fetches only buckets that have changed; buckets gone from the timeline are dropped."""
    path = os.path.join(kodi.PROFILE, 'covers.json')
    known = kodi.read_json(path, {})
    found, fetched = {}, False
    for b in wanted:
        key = b['timeBucket']
        if not known.get(key) or known[key][0] != b['count']:
            known[key] = [b['count'], next((a['id'] for a in client.timeline_bucket(key) if a.get('image')), None)]
            fetched = True
        found[key] = known[key][1]
    if fetched:
        kodi.write_json(path, {b['timeBucket']: known[b['timeBucket']] for b in buckets if b['timeBucket'] in known})
    return found


def action_item(label, action, icon=None, **params):
    li = xbmcgui.ListItem(label, offscreen=True)
    li.setArt({'icon': icon or kodi.ICON, 'thumb': icon or kodi.ICON})
    xbmcplugin.addDirectoryItem(HANDLE, url_for(action, **params), li, isFolder=False)


# ------------------------------------------------------------------ listings
def root():
    if not signin.is_signed_in() or not signin.check():
        action_item(kodi.L(30002), 'signin')
        action_item(kodi.L(30003), 'settings')
        return end()
    folder(kodi.L(30000), 'timeline', tiles.art('timeline'))
    client = ImmichClient(timeout=PROBE_TIMEOUT)
    probe = Probe()
    memories = probe(lambda: client.memories(date.today()), [], [])
    if memories:
        folder(kodi.L(30017), 'memories', tiles.art('memories'), label2=count(sum(len(m[1]) for m in memories)),
               context=slideshow_menu(source='memories'))
    if probe(lambda: client.has_people(), False, True):
        folder(kodi.L(30012), 'people', tiles.art('people'))
    folder(kodi.L(30014), 'places', tiles.art('places'))
    if probe(lambda: bool(client.search_page(1, 1, isFavorite=True, visibility=TIMELINE)[0]), True, True):
        folder(kodi.L(30021), 'favourites', tiles.art('favourites'), context=slideshow_menu(source='favourites'))
    if probe(lambda: any(a.get('assetCount') for a in client.albums()), True, True):
        folder(kodi.L(30001), 'albums', tiles.art('albums'))
    folder(kodi.L(30022), 'search_menu', tiles.art('search_menu'))
    action_item(kodi.L(30006), 'play', tiles.art('shuffle'), source='random', shuffle='1')
    action_item(kodi.L(30003), 'settings')
    end()


class Probe:
    """Checks whether a root entry has anything; once the server is unreachable, stop asking."""
    def __init__(self):
        self.offline = False

    def __call__(self, check, denied, failed):
        """denied: result for a key without the permission; failed: for any other error."""
        if self.offline:
            return failed
        try:
            return check()
        except AuthError:
            return denied
        except ApiError as e:
            self.offline = e.status is None
            return failed


def today_memories(client):
    try:
        return in_order(client.memories(date.today()))
    except ApiError:                            # includes a key without memory.read
        return []


def month_name(month):
    return xbmc.getLocalizedString(20 + month)                  # Kodi strings 21-32


def slideshow_menu(**source):
    return [(kodi.L(30004), run_plugin('play', shuffle='0', **source)),
            (kodi.L(30005), run_plugin('play', shuffle='1', **source))]


def timeline(client, year=None):
    buckets = client.timeline_buckets()
    if not year:
        years, newest = {}, {}
        for b in buckets:
            y = b['timeBucket'][:4]
            years[y] = years.get(y, 0) + b['count']
            if y not in newest or b['timeBucket'] > newest[y]['timeBucket']:
                newest[y] = b
        cover = covers(client, newest.values(), buckets)
        for y in sorted(years, reverse=True):
            folder(y, 'timeline', kodi.ICON, art=photo_art(client, cover[newest[y]['timeBucket']]), label2=count(years[y]),
                   context=slideshow_menu(source='year', year=y), year=y)
    else:
        xbmcplugin.setPluginCategory(HANDLE, year)
        months = sorted((b for b in buckets if b['timeBucket'].startswith(year)), key=lambda b: b['timeBucket'],
                        reverse=newest_first())
        cover = covers(client, months, buckets)
        for b in months:
            name = '{} {}'.format(month_name(int(b['timeBucket'][5:7])), year)
            folder(name, 'bucket', kodi.ICON, art=photo_art(client, cover[b['timeBucket']]), label2=count(b['count']),
                   context=slideshow_menu(source='bucket', bucket=b['timeBucket']),
                   bucket=b['timeBucket'], name=name)
    end()


def albums(client):
    found = [a for a in client.albums() if a.get('assetCount')]
    found.sort(key=lambda a: a.get('endDate') or a.get('updatedAt') or '', reverse=True)
    for a in found:
        name = a.get('albumName') or a['id']
        source = {'source': 'album', 'album_id': a['id']}
        folder(name, 'album', kodi.ICON, art=photo_art(client, a.get('albumThumbnailAssetId')), label2=count(a['assetCount']),
               context=slideshow_menu(**source), album_id=a['id'], name=name)
    xbmcplugin.addSortMethod(HANDLE, xbmcplugin.SORT_METHOD_NONE)
    xbmcplugin.addSortMethod(HANDLE, xbmcplugin.SORT_METHOD_LABEL)
    end()


def screensaver_albums(client):
    """Pick the screensaver's albums; their ids drive it, their names show in the settings."""
    found = sorted((a for a in client.albums() if a.get('assetCount')), key=lambda a: (a.get('albumName') or '').lower())
    chosen = set(kodi.setting('screensaver_albums').split(','))
    names = [a.get('albumName') or a['id'] for a in found]
    rows = []
    for a, name in zip(found, names):
        li = xbmcgui.ListItem(name, count(a['assetCount']), offscreen=True)
        li.setArt(photo_art(client, a.get('albumThumbnailAssetId')) or {'thumb': kodi.ICON})
        rows.append(li)
    picked = xbmcgui.Dialog().multiselect(kodi.L(30409), rows, preselect=[i for i, a in enumerate(found) if a['id'] in chosen],
                                          useDetails=True)
    if picked is None:
        return
    kodi.set_setting('screensaver_albums', ','.join(found[i]['id'] for i in picked))
    kodi.set_setting('screensaver_album_names', ', '.join(names[i] for i in picked))


def people(client):
    found = client.people()
    found.sort(key=lambda p: (not p.get('name'), (p.get('name') or '').lower()))   # named first
    for p in found:
        name = p.get('name') or kodi.L(30013)
        face = client.person_thumb_url(p['id'])
        folder(name, 'person', art={'thumb': face, 'icon': face, 'fanart': client.person_fanart_url(p['id'])},
               context=slideshow_menu(source='person', person_id=p['id']), person_id=p['id'], name=name)
    xbmcplugin.addSortMethod(HANDLE, xbmcplugin.SORT_METHOD_NONE)
    end()


def places(client, country=None):
    """Countries, then their cities; a single country goes straight to its cities."""
    found = []
    for a in client.cities():
        exif = a.get('exifInfo') or {}
        if exif.get('city'):
            found.append((exif.get('country') or '', exif.get('state') or '', exif['city'], a['id']))
    countries = sorted({c for c, _, _, _ in found})
    if country == NO_COUNTRY:
        country = ''
    if country is None and len(countries) > 1:
        for c in countries:
            mine = sorted((x for x in found if x[0] == c), key=lambda x: x[2].lower())
            folder(c or kodi.L(30015), 'places', kodi.ICON, art=photo_art(client, mine[0][3]), label2=count(len(mine), 30020, 30016),
                   country=c or NO_COUNTRY, name=c)
        return end()
    if country is not None:
        xbmcplugin.setPluginCategory(HANDLE, country or kodi.L(30015))
    for c, state, city, asset_id in sorted(found, key=lambda x: x[2].lower()):
        if country is not None and c != country:
            continue
        where = {'city': city, 'country': c}       # state is often missing on some of a city's photos
        folder(city, 'place', art=photo_art(client, asset_id), label2=state, context=slideshow_menu(source='place', **where),
               name=city, **where)
    end()


def memories(client, year=None):
    found = today_memories(client)
    if year:
        assets = next((a for y, a in found if str(y) == year), [])
        return list_assets(client, assets, {'source': 'memories', 'year': year}, kodi.L(30017))
    if found:
        action_item(kodi.L(30004), 'play', source='memories')
    now = date.today().year
    for y, assets in found:
        cover = assets[0]['id']
        ago = kodi.L(30019) if now - y == 1 else kodi.L(30018, now - y)
        folder(ago, 'memories', art=photo_art(client, cover), label2='{} · {}'.format(y, count(len(assets))),
               context=slideshow_menu(source='memories', year=y), year=y)
    end()


def recent_searches():
    return kodi.read_json(kodi.profile_file('searches.json'), [])


def search_menu():
    action_item(kodi.L(30023), 'new_search')
    for query in recent_searches():
        folder(query, 'search', kodi.ICON, context=slideshow_menu(source='search', query=query), query=query)
    end()


def new_search():
    query = xbmcgui.Dialog().input(kodi.L(30024)).strip()
    if not query:
        return
    kodi.write_json(kodi.profile_file('searches.json'),
                    ([query] + [q for q in recent_searches() if q.lower() != query.lower()])[:RECENT])
    xbmc.executebuiltin('Container.Update({})'.format(url_for('search', query=query)))


def search(client, params):
    page = int(params.get('page') or 1)
    found, has_more = client.smart_page(params['query'], page, SEARCH_PAGE)
    more = dict(params, page=page + 1) if has_more else None
    list_assets(client, found, {'source': 'search', 'query': params['query'], 'upto': page}, params['query'], more)


def page_size():
    return min(max(kodi.setting_int('page_size'), 100), 1000)   # Immich pages hold 1000 at most


def newest_first():
    return kodi.setting_int('photo_order') == 1


def order():
    return 'desc' if newest_first() else 'asc'


def in_order(found):
    """Memories, [(year, assets)]: years stay newest first, each day's photos in the chosen order."""
    return [(y, sorted(assets, key=lambda a: a['taken'] or datetime.min, reverse=newest_first())) for y, assets in found]


def list_assets(client, assets, source, category=None, more=None, play=True):
    """source: params that let the viewer fetch the same assets again; more: next page params."""
    xbmcplugin.setContent(HANDLE, 'images')
    if category:
        xbmcplugin.setPluginCategory(HANDLE, category)
    if assets and play:
        action_item(kodi.L(30004), 'play', **source)
    own_viewer = kodi.setting_bool('viewer')
    for asset in assets:
        path = url_for('play', start=asset['id'], autoplay='0', shuffle='0', **source) \
            if own_viewer and asset['image'] else None
        li, url = items.asset_item(client, asset, path)
        li.addContextMenuItems([(kodi.L(30004), run_plugin('play', start=asset['id'], shuffle='0', **source))])
        xbmcplugin.addDirectoryItem(HANDLE, path or url, li, isFolder=False)
    if more:
        more = dict(more)
        folder(kodi.L(30011), more.pop('action'), kodi.ICON, **more)
    xbmcplugin.addSortMethod(HANDLE, xbmcplugin.SORT_METHOD_NONE)
    xbmcplugin.addSortMethod(HANDLE, xbmcplugin.SORT_METHOD_DATE)
    end()


def bucket(client, params):
    size, offset = page_size(), int(params.get('offset') or 0)
    found = client.timeline_bucket(params['bucket'], order=order())
    more = None
    if offset + size < len(found):
        more = dict(params, offset=offset + size)
    list_assets(client, found[offset:offset + size], {'source': 'bucket', 'bucket': params['bucket']},
                params.get('name'), more)


def searched(client, params, source, **filters):
    """A page of /search/metadata results with a Next page entry."""
    page = int(params.get('page') or 1)          # a page number stays right if the page size changes
    found, has_more = client.search_page(page, page_size(), order(), **filters)
    list_assets(client, found, source, params.get('name'), dict(params, page=page + 1) if has_more else None)


def album(client, params):
    source = {'source': 'album', 'album_id': params['album_id']}
    searched(client, params, source, albumIds=[params['album_id']])


def person(client, params):
    searched(client, params, {'source': 'person', 'person_id': params['person_id']}, personIds=[params['person_id']],
             visibility=TIMELINE)


def place_filters(params):
    return {k: params[k] for k in ('city', 'country') if params.get(k)}


def place(client, params):
    searched(client, params, dict(place_filters(params), source='place'), visibility=TIMELINE, **place_filters(params))


def source_assets(client, params):
    kind = params.get('source')
    if kind == 'bucket':
        return client.timeline_bucket(params['bucket'], order=order())
    if kind == 'year':
        found = []
        for b in sorted(client.timeline_buckets(), key=lambda b: b['timeBucket'], reverse=newest_first()):
            if b['timeBucket'].startswith(params['year']):
                found += client.timeline_bucket(b['timeBucket'], order=order())
        return found
    if kind == 'album':
        return client.search(order=order(), albumIds=[params['album_id']])
    if kind == 'memories':
        return [a for y, found in in_order(client.memories(date.today()))
                if not params.get('year') or str(y) == params['year'] for a in found]
    if kind == 'search':
        last = max(2, int(params.get('upto') or 1))    # reach the page a photo was picked from
        return [a for page in range(1, last + 1) for a in client.smart_page(params['query'], page, SEARCH_PAGE)[0]]
    if kind == 'favourites':
        return client.search(order(), isFavorite=True, visibility=TIMELINE)
    if kind == 'place':
        return client.search(order(), visibility=TIMELINE, **place_filters(params))
    if kind == 'person':
        return client.search(order(), personIds=[params['person_id']], visibility=TIMELINE)
    if kind == 'random':
        return client.random()
    return []


def video(client, asset_id):
    li = xbmcgui.ListItem(path=client.video_url(asset_id), offscreen=True)
    li.setMimeType('video/mp4')
    li.setContentLookup(False)
    xbmcplugin.setResolvedUrl(HANDLE, True, li)


def play(client, params):
    xbmc.executebuiltin('ActivateWindow(busydialognocancel)')
    try:
        assets = source_assets(client, params)
    finally:
        xbmc.executebuiltin('Dialog.Close(busydialognocancel)')
    shuffle = params.get('shuffle')
    if shuffle == '1' or (shuffle is None and kodi.setting_bool('shuffle')):
        random.shuffle(assets)
    start = next((i for i, a in enumerate(assets) if a['id'] == params.get('start')), 0)
    more = client.random if params.get('source') == 'random' else None
    viewer.play(client, assets, start, autoplay=params.get('autoplay') != '0', more=more)


# --------------------------------------------------------------------- main
def run(argv):
    global HANDLE
    HANDLE = int(argv[1])
    params = dict(parse_qsl(argv[2].lstrip('?')))
    action = params.get('action', 'root')
    kodi.debug('action={} params={}'.format(action, json.dumps(params)))
    try:
        dispatch(action, params)
    except AuthError as e:
        kodi.error(str(e))
        end(False)
    except ApiError as e:
        kodi.log('request failed: {}'.format(e), xbmc.LOGERROR)
        kodi.error(str(e))
        end(False)
    except Exception as e:  # keep Kodi from waiting on a listing that never ends
        kodi.log(traceback.format_exc(), xbmc.LOGERROR)
        kodi.error(str(e))
        end(False)


def dispatch(action, params):
    if action == 'root':
        root()
    elif action == 'settings':
        kodi.ADDON.openSettings()
        xbmc.executebuiltin('Container.Refresh')   # Kodi doesn't refresh plugin lists itself
    elif action == 'signin':
        if signin.sign_in():
            xbmc.executebuiltin('Container.Refresh')
    elif action == 'signout':
        signin.sign_out()
        xbmc.executebuiltin('Container.Refresh')
    elif not signin.is_signed_in():
        kodi.error(kodi.L(30615))
        end(False)
    elif action == 'timeline':
        timeline(ImmichClient(), params.get('year'))
    elif action == 'bucket':
        bucket(ImmichClient(), params)
    elif action == 'albums':
        albums(ImmichClient())
    elif action == 'album':
        album(ImmichClient(), params)
    elif action == 'screensaver_albums':
        screensaver_albums(ImmichClient())
    elif action == 'memories':
        memories(ImmichClient(), params.get('year'))
    elif action == 'today':                     # one flat row of today's memories, for home screen widgets
        client = ImmichClient()
        list_assets(client, [a for _, found in today_memories(client) for a in found], {'source': 'memories'},
                    kodi.L(30017), play=False)
    elif action == 'search_menu':
        search_menu()
    elif action == 'new_search':
        new_search()
    elif action == 'search':
        search(ImmichClient(), params)
    elif action == 'favourites':
        searched(ImmichClient(), dict(params, name=kodi.L(30021)), {'source': 'favourites'}, isFavorite=True,
                 visibility=TIMELINE)
    elif action == 'places':
        places(ImmichClient(), params.get('country'))
    elif action == 'place':
        place(ImmichClient(), params)
    elif action == 'people':
        people(ImmichClient())
    elif action == 'person':
        person(ImmichClient(), params)
    elif action == 'play':
        play(ImmichClient(), params)
    elif action == 'video':
        video(ImmichClient(), params['id'])
    else:
        kodi.log('unknown action {}'.format(action), xbmc.LOGWARNING)
        end(False)

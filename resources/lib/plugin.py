# -*- coding: utf-8 -*-
"""plugin:// router and directory listings."""
import json
import random
import traceback
from urllib.parse import parse_qsl, urlencode

import xbmc
import xbmcgui
import xbmcplugin

from . import items, kodi, signin, viewer
from .api import ApiError, AuthError, ImmichClient

BASE = 'plugin://{}/'.format(kodi.ADDON_ID)
HANDLE = -1


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


def action_item(label, action, **params):
    li = xbmcgui.ListItem(label, offscreen=True)
    li.setArt({'icon': kodi.ICON, 'thumb': kodi.ICON})
    xbmcplugin.addDirectoryItem(HANDLE, url_for(action, **params), li, isFolder=False)


# ------------------------------------------------------------------ listings
def root():
    if not signin.is_signed_in() or not signin.check():
        action_item(kodi.L(30002), 'signin')
        action_item(kodi.L(30003), 'settings')
        return end()
    folder(kodi.L(30000), 'timeline', kodi.ICON)
    folder(kodi.L(30001), 'albums', kodi.ICON)
    action_item(kodi.L(30006), 'play', source='random', shuffle='1')
    action_item(kodi.L(30003), 'settings')
    end()


def month_name(month):
    return xbmc.getLocalizedString(20 + month)                  # Kodi strings 21-32


def slideshow_menu(**source):
    return [(kodi.L(30004), run_plugin('play', shuffle='0', **source)),
            (kodi.L(30005), run_plugin('play', shuffle='1', **source))]


def timeline(client, year=None):
    buckets = client.timeline_buckets()
    if not year:
        years = {}
        for b in buckets:
            years[b['timeBucket'][:4]] = years.get(b['timeBucket'][:4], 0) + b['count']
        for y in sorted(years, reverse=True):
            folder(y, 'timeline', kodi.ICON, label2=kodi.L(30010, years[y]),
                   context=slideshow_menu(source='year', year=y), year=y)
    else:
        xbmcplugin.setPluginCategory(HANDLE, year)
        for b in buckets:
            if b['timeBucket'].startswith(year):
                name = '{} {}'.format(month_name(int(b['timeBucket'][5:7])), year)
                folder(name, 'bucket', kodi.ICON, label2=kodi.L(30010, b['count']),
                       context=slideshow_menu(source='bucket', bucket=b['timeBucket']),
                       bucket=b['timeBucket'], name=name)
    end()


def albums(client):
    found = [a for a in client.albums() if a.get('assetCount')]
    found.sort(key=lambda a: a.get('endDate') or a.get('updatedAt') or '', reverse=True)
    for a in found:
        art = {}
        if a.get('albumThumbnailAssetId'):
            art = {'thumb': client.thumb_url(a['albumThumbnailAssetId']),
                   'fanart': client.thumb_url(a['albumThumbnailAssetId'], 'preview')}
        name = a.get('albumName') or a['id']
        source = {'source': 'album', 'album_id': a['id'], 'order': a.get('order')}
        folder(name, 'album', kodi.ICON, art=art, label2=kodi.L(30010, a['assetCount']),
               context=slideshow_menu(**source), album_id=a['id'], name=name, order=a.get('order'))
    xbmcplugin.addSortMethod(HANDLE, xbmcplugin.SORT_METHOD_NONE)
    xbmcplugin.addSortMethod(HANDLE, xbmcplugin.SORT_METHOD_LABEL)
    end()


def page_size():
    return min(max(kodi.setting_int('page_size'), 100), 1000)   # Immich pages hold 1000 at most


def list_assets(client, assets, source, category=None, more=None):
    """source: params that let the viewer fetch the same assets again; more: next page params."""
    xbmcplugin.setContent(HANDLE, 'images')
    if category:
        xbmcplugin.setPluginCategory(HANDLE, category)
    if assets:
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
    found = client.timeline_bucket(params['bucket'])
    more = None
    if offset + size < len(found):
        more = dict(params, offset=offset + size)
    list_assets(client, found[offset:offset + size], {'source': 'bucket', 'bucket': params['bucket']},
                params.get('name'), more)


def album(client, params):
    size, offset = page_size(), int(params.get('offset') or 0)
    source = {'source': 'album', 'album_id': params['album_id'], 'order': params.get('order')}
    found, has_more = client.search_page(offset // size + 1, size, params.get('order') or 'desc',
                                         albumIds=[params['album_id']])
    list_assets(client, found, source, params.get('name'), dict(params, offset=offset + size) if has_more else None)


def source_assets(client, params):
    kind = params.get('source')
    if kind == 'bucket':
        return client.timeline_bucket(params['bucket'])
    if kind == 'year':
        found = []
        for b in client.timeline_buckets():
            if b['timeBucket'].startswith(params['year']):
                found += client.timeline_bucket(b['timeBucket'])
        return found
    if kind == 'album':
        return client.search(order=params.get('order') or 'desc', albumIds=[params['album_id']])
    if kind == 'random':
        return client.random(500)
    return []


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
    viewer.play(client, assets, start, autoplay=params.get('autoplay') != '0')


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
    elif action == 'play':
        play(ImmichClient(), params)
    else:
        kodi.log('unknown action {}'.format(action), xbmc.LOGWARNING)
        end(False)

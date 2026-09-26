# -*- coding: utf-8 -*-
"""plugin:// router and directory listings."""
import json
import traceback
from urllib.parse import parse_qsl, urlencode

import xbmc
import xbmcgui
import xbmcplugin

from . import items, kodi, signin
from .api import ApiError, AuthError, ImmichClient

BASE = 'plugin://{}/'.format(kodi.ADDON_ID)
HANDLE = -1


def url_for(action, **params):
    params['action'] = action
    return BASE + '?' + urlencode({k: v for k, v in params.items() if v is not None and v != ''})


def run_plugin(action, **params):
    return 'RunPlugin({})'.format(url_for(action, **params))


def end(succeeded=True, cache_to_disc=False):
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
    if not signin.is_signed_in():
        action_item(kodi.L(30002), 'signin')
        action_item(kodi.L(30003), 'settings')
        return end()
    folder(kodi.L(30000), 'timeline', kodi.ICON)
    folder(kodi.L(30001), 'albums', kodi.ICON)
    action_item(kodi.L(30003), 'settings')
    end()


def month_name(month):
    return xbmc.getLocalizedString(20 + month)                  # Kodi strings 21-32


def timeline(client, year=None):
    buckets = client.timeline_buckets()
    if not year:
        years = {}
        for b in buckets:
            years[b['timeBucket'][:4]] = years.get(b['timeBucket'][:4], 0) + b['count']
        for y in sorted(years, reverse=True):
            folder(y, 'timeline', kodi.ICON, label2=kodi.L(30010, years[y]), year=y)
    else:
        xbmcplugin.setPluginCategory(HANDLE, year)
        for b in buckets:
            if b['timeBucket'].startswith(year):
                name = '{} {}'.format(month_name(int(b['timeBucket'][5:7])), year)
                folder(name, 'bucket', kodi.ICON, label2=kodi.L(30010, b['count']), bucket=b['timeBucket'], name=name)
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
        folder(name, 'album', kodi.ICON, art=art, label2=kodi.L(30010, a['assetCount']),
               album_id=a['id'], name=name, order=a.get('order'))
    xbmcplugin.addSortMethod(HANDLE, xbmcplugin.SORT_METHOD_NONE)
    xbmcplugin.addSortMethod(HANDLE, xbmcplugin.SORT_METHOD_LABEL)
    end()


def list_assets(client, assets, category=None):
    xbmcplugin.setContent(HANDLE, 'images')
    if category:
        xbmcplugin.setPluginCategory(HANDLE, category)
    for asset in assets:
        li, url = items.asset_item(client, asset)
        xbmcplugin.addDirectoryItem(HANDLE, url, li, isFolder=False)
    xbmcplugin.addSortMethod(HANDLE, xbmcplugin.SORT_METHOD_NONE)
    xbmcplugin.addSortMethod(HANDLE, xbmcplugin.SORT_METHOD_DATE)
    end()


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
        if HANDLE >= 0:
            end(False)
    except ApiError as e:
        kodi.log('request failed: {}'.format(e), xbmc.LOGERROR)
        kodi.error(str(e))
        if HANDLE >= 0:
            end(False)
    except Exception as e:  # keep Kodi from waiting on a listing that never ends
        kodi.log(traceback.format_exc(), xbmc.LOGERROR)
        kodi.error(str(e))
        if HANDLE >= 0:
            end(False)


def dispatch(action, params):
    if action == 'root':
        root()
    elif action == 'settings':
        kodi.ADDON.openSettings()
    elif action == 'signin':
        if signin.sign_in():
            xbmc.executebuiltin('Container.Refresh')
    elif action == 'enter_key':
        if signin.enter_key():
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
        list_assets(ImmichClient(), ImmichClient().timeline_bucket(params['bucket']), params.get('name'))
    elif action == 'albums':
        albums(ImmichClient())
    elif action == 'album':
        client = ImmichClient()
        list_assets(client, client.search(order=params.get('order') or 'desc', albumIds=[params['album_id']]),
                    params.get('name'))
    else:
        kodi.log('unknown action {}'.format(action), xbmc.LOGWARNING)
        end(False)

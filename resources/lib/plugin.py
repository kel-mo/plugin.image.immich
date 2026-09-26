# -*- coding: utf-8 -*-
"""plugin:// router and directory listings."""
import json
import traceback
from urllib.parse import parse_qsl, urlencode

import xbmc
import xbmcgui
import xbmcplugin

from . import kodi, signin

BASE = 'plugin://{}/'.format(kodi.ADDON_ID)
HANDLE = -1


def url_for(action, **params):
    params['action'] = action
    return BASE + '?' + urlencode({k: v for k, v in params.items() if v is not None and v != ''})


def run_plugin(action, **params):
    return 'RunPlugin({})'.format(url_for(action, **params))


def end(succeeded=True, cache_to_disc=False):
    xbmcplugin.endOfDirectory(HANDLE, succeeded, cacheToDisc=cache_to_disc)


def folder(label, action, icon=None, art=None, context=None, **params):
    li = xbmcgui.ListItem(label, offscreen=True)
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
    action_item(kodi.L(30003), 'settings')
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
    else:
        kodi.log('unknown action {}'.format(action), xbmc.LOGWARNING)
        end(False)

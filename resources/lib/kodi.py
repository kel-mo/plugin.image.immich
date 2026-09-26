# -*- coding: utf-8 -*-
"""Thin helpers around the Kodi Python API."""
import json
import os

import xbmc
import xbmcaddon
import xbmcgui
import xbmcvfs

ADDON = xbmcaddon.Addon()
ADDON_ID = ADDON.getAddonInfo('id')
ADDON_NAME = ADDON.getAddonInfo('name')
ADDON_VERSION = ADDON.getAddonInfo('version')
ADDON_PATH = xbmcvfs.translatePath(ADDON.getAddonInfo('path'))
PROFILE = xbmcvfs.translatePath(ADDON.getAddonInfo('profile'))
ICON = os.path.join(ADDON_PATH, 'resources', 'icon.png')


def L(string_id, *args):
    text = ADDON.getLocalizedString(string_id)
    return text.format(*args) if args else text


def setting(key):
    return ADDON.getSetting(key)


def setting_bool(key):
    return ADDON.getSettingBool(key)


def setting_int(key):
    return ADDON.getSettingInt(key)


def set_setting(key, value):
    ADDON.setSetting(key, str(value) if value is not None else '')


def log(msg, level=xbmc.LOGINFO):
    xbmc.log('[{}] {}'.format(ADDON_ID, msg), level)


def debug(msg):
    if setting_bool('debug'):
        xbmc.log('[{}] {}'.format(ADDON_ID, msg), xbmc.LOGINFO)
    else:
        xbmc.log('[{}] {}'.format(ADDON_ID, msg), xbmc.LOGDEBUG)


def notify(message, heading=None, icon=None, time=4000):
    xbmcgui.Dialog().notification(heading or ADDON_NAME, message, icon or ICON, time)


def error(message, heading=None):
    xbmcgui.Dialog().notification(heading or ADDON_NAME, message, xbmcgui.NOTIFICATION_ERROR, 6000)


def jsonrpc(method, **params):
    payload = {'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params}
    result = json.loads(xbmc.executeJSONRPC(json.dumps(payload)))
    if 'error' in result:
        log('JSON-RPC {} failed: {}'.format(method, result['error']), xbmc.LOGWARNING)
        return None
    return result.get('result')


def ensure_dir(path):
    if not xbmcvfs.exists(path.rstrip('/') + '/'):
        xbmcvfs.mkdirs(path)
    return path


def read_json(path, default=None):
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def write_json(path, data):
    ensure_dir(os.path.dirname(path))
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, sort_keys=True)
    os.replace(tmp, path)


def data_file(name):
    return os.path.join(ADDON_PATH, 'resources', 'data', name)


def profile_file(name):
    return os.path.join(PROFILE, name)

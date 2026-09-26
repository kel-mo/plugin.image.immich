# -*- coding: utf-8 -*-
"""Account: the Immich server address and an API key made in Immich."""
import hashlib

import xbmcgui

from . import kodi
from .api import MIN_SERVER, ApiError, AuthError, ImmichClient, clean_url

REQUIRED = {'asset.read', 'asset.view', 'album.read', 'user.read'}


def is_signed_in():
    return bool(kodi.setting('server_url') and kodi.setting('api_key'))


def fingerprint(server, api_key):
    return hashlib.sha256('{}|{}'.format(server, api_key).encode('utf-8')).hexdigest()[:16]


def verify(server, api_key):
    """Returns (server_url, api_key, user name) or raises ApiError with a message for the user."""
    server = clean_url(server)
    if not server:
        raise ApiError(kodi.L(30601))
    client = ImmichClient(server, api_key.strip())
    version = client.server_version()
    if version < MIN_SERVER:
        raise ApiError(kodi.L(30621, '.'.join(map(str, version))))
    try:
        perms = set((client.key_info() or {}).get('permissions') or [])  # needs no scope
    except AuthError:
        raise ApiError(kodi.L(30624))
    missing = REQUIRED - perms if 'all' not in perms else set()
    if missing:
        raise ApiError(kodi.L(30625, ', '.join(sorted(missing))))
    me = client.me()
    return server, client.api_key, me.get('name') or me.get('email') or ''


def store(server, api_key, name):
    kodi.set_setting('server_url', server)
    kodi.set_setting('api_key', api_key)
    kodi.set_setting('username', name)
    kodi.set_setting('key_check', fingerprint(server, api_key))


def check():
    """Verify the settings once after they change; True when they work."""
    server, api_key = kodi.setting('server_url'), kodi.setting('api_key')
    if kodi.setting('key_check') == fingerprint(server, api_key):
        return True
    try:
        result = verify(server, api_key)
    except ApiError as e:
        kodi.error(str(e))
        return False
    store(*result)
    kodi.notify(kodi.L(30604, result[2]))
    return True


def sign_in():
    """Type or paste the server address and an API key."""
    dialog = xbmcgui.Dialog()
    server = dialog.input(kodi.L(30101), kodi.fresh_setting('server_url') or 'https://',
                          type=xbmcgui.INPUT_ALPHANUM)
    if not server or server.strip() == 'https://':
        return False
    api_key = dialog.input(kodi.L(30105), type=xbmcgui.INPUT_ALPHANUM)
    if not api_key:
        return False
    try:
        result = verify(server, api_key)
    except ApiError as e:
        kodi.error(str(e))
        return False
    store(*result)
    kodi.notify(kodi.L(30604, result[2]))
    return True


def sign_out():
    for key in ('api_key', 'username', 'key_check'):
        kodi.set_setting(key, '')
    kodi.notify(kodi.L(30609))

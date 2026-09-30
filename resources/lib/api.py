# -*- coding: utf-8 -*-
"""Minimal Immich REST client built on urllib (no external dependencies)."""
import json
import socket
import ssl
from datetime import datetime, timedelta
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

import xbmc

from . import kodi, proxy

MIN_SERVER = (2, 0, 0)
TIMEOUT = 30
PAGE = 1000

class ApiError(Exception):
    def __init__(self, message, status=None, detail=None):
        super().__init__(message)
        self.status = status
        self.detail = detail


class AuthError(ApiError):
    pass


def parse_version(data):
    return tuple(int(data.get(k) or 0) for k in ('major', 'minor', 'patch'))


def clean_url(url):
    url = (url or '').strip().rstrip('/')
    if url.endswith('/api'):
        url = url[:-4]
    if url and not url.startswith(('http://', 'https://')):
        netloc = url.split('/')[0]
        host = netloc.rsplit(':', 1)[0].strip('[]').lower()
        # IPs, single-label names, LAN domains and Immich's own port are usually plain http
        plain = ('.' not in host or host.replace('.', '').isdigit() or ':' in host or netloc.endswith(':2283')
                 or host.endswith(('.local', '.lan', '.home.arpa', '.internal')))
        url = ('http://' if plain else 'https://') + url
    return url


def proxy_address():
    """Waits a moment for the service when Kodi has just started."""
    monitor = xbmc.Monitor()
    for _ in range(30):
        base = proxy.address()
        if base or monitor.waitForAbort(0.1):
            break
    if not base:
        raise ApiError(kodi.L(30626))
    return base


_monitor = None


def aborting():
    """True once Kodi is shutting down; every request checks it so no call outlives teardown."""
    global _monitor
    if _monitor is None:
        _monitor = xbmc.Monitor()
    return _monitor.abortRequested()


class ImmichClient:
    def __init__(self, base_url=None, api_key=None, timeout=TIMEOUT):
        self.base_url = clean_url(base_url if base_url is not None else kodi.setting('server_url'))
        self.api_key = api_key if api_key is not None else kodi.setting('api_key')
        self.timeout = timeout
        self.ssl_ctx = ssl.create_default_context()

    # ------------------------------------------------------------------ core
    def url(self, path, **params):
        clean = {k: v for k, v in params.items() if v is not None and v != ''}
        query = urlencode(clean, doseq=True)
        return '{}/api{}{}'.format(self.base_url, path, '?' + query if query else '')

    def headers(self, extra=None):
        h = {'Accept': 'application/json',
             'User-Agent': '{}/{}'.format(kodi.ADDON_ID, kodi.ADDON_VERSION)}
        if self.api_key:
            h['x-api-key'] = self.api_key
        if extra:
            h.update(extra)
        return h

    def request(self, method, path, params=None, body=None, timeout=None):
        if not self.base_url:
            raise ApiError(kodi.L(30601))
        if aborting():
            raise ApiError('cancelled')
        url = self.url(path, **(params or {}))
        data = None
        extra = {}
        if body is not None:
            data = json.dumps(body).encode('utf-8')
            extra['Content-Type'] = 'application/json'
        req = Request(url, data=data, headers=self.headers(extra), method=method)
        kodi.debug('{} {}'.format(method, url))
        try:
            with urlopen(req, timeout=timeout or self.timeout, context=self.ssl_ctx) as resp:
                payload = resp.read()
        except HTTPError as e:
            detail = None
            try:
                detail = json.loads(e.read().decode('utf-8', 'replace')).get('message')
            except (ValueError, AttributeError):
                pass
            if isinstance(detail, list):
                detail = '; '.join(map(str, detail))
            if e.code in (401, 403):
                raise AuthError(kodi.L(30615 if e.code == 401 else 30627), e.code, detail)
            raise ApiError('HTTP {} for {}: {}'.format(e.code, path, detail or e.reason), e.code, detail)
        except (URLError, socket.timeout, OSError, HTTPException) as e:
            raise ApiError('{}: {}'.format(kodi.L(30614), e))
        if not payload:
            return None
        try:
            return json.loads(payload.decode('utf-8'))
        except ValueError:
            raise ApiError('{}: non-JSON response for {}'.format(kodi.L(30614), path))

    def get(self, path, **params):
        return self.request('GET', path, params=params)

    def post(self, path, body=None, **params):
        return self.request('POST', path, params=params, body=body if body is not None else {})

    # ---------------------------------------------------------------- server
    def server_version(self):
        return parse_version(self.get('/server/version') or {})

    def me(self):
        return self.get('/users/me')

    def key_info(self):
        return self.get('/api-keys/me')

    # ---------------------------------------------------------------- library
    def timeline_buckets(self, **filters):
        return self.get('/timeline/buckets', **_filters(filters))

    def timeline_bucket(self, time_bucket, **filters):
        cols = self.get('/timeline/bucket', timeBucket=time_bucket, **_filters(filters)) or {}
        return [from_bucket(row) for row in _rows(cols)]

    def albums(self):
        return self.get('/albums') or []

    def has_people(self):
        res = self.get('/people', page=1, size=1, withHidden='false') or {}
        return bool(res.get('people') or res.get('total'))

    def people(self):
        """Visible people and pets, all pages."""
        out, page = [], 1
        while True:
            res = self.get('/people', page=page, size=500, withHidden='false') or {}
            out += res.get('people') or []
            if not res.get('hasNextPage'):
                return out
            page += 1

    def asset_raw(self, asset_id):
        return self.get('/assets/{}'.format(quote(asset_id))) or {}

    def cities(self):
        """One asset per city, carrying the city, state and country."""
        return self.get('/search/cities') or []

    def memories(self, day):
        """Immich's "on this day" memories for a date: [(year, assets)], newest first."""
        found = self.get('/memories', **{'for': day.isoformat(), 'type': 'on_this_day'}) or []
        out = [((m.get('data') or {}).get('year'), [from_asset(a) for a in m.get('assets') or []]) for m in found]
        return sorted((m for m in out if m[0] and m[1]), key=lambda m: -m[0])

    def search_page(self, page=1, size=PAGE, order='desc', **filters):
        """One page of /search/metadata results: (assets, more pages follow)."""
        body = dict(filters, page=page, size=size, order=order, withExif=True)
        res = (self.post('/search/metadata', body) or {}).get('assets') or {}
        return [from_asset(a) for a in res.get('items') or []], bool(res.get('nextPage'))

    def smart_page(self, query, page=1, size=100):
        """Immich's AI search by description, best matches first: (assets, more pages follow)."""
        body = {'query': query, 'page': page, 'size': size, 'withExif': True, 'visibility': 'timeline'}
        res = (self.post('/search/smart', body) or {}).get('assets') or {}
        return [from_asset(a) for a in res.get('items') or []], bool(res.get('nextPage'))

    def search(self, order='desc', **filters):
        """All assets matching /search/metadata filters."""
        out, page, more = [], 1, True
        while more:
            found, more = self.search_page(page, order=order, **filters)
            out += found
            page += 1
        return out

    def random(self, size=250):
        body = {'size': size, 'withExif': True, 'visibility': 'timeline'}   # skips live-photo clips, archive
        return [from_asset(a) for a in self.post('/search/random', body) or []]

    # ------------------------------------------------------------------ media
    def media_url(self, path, **params):
        """Kodi fetches these through the local proxy, which adds the API key."""
        return proxy_address() + self.url(path, **params)[len(self.base_url):]

    def thumb_url(self, asset_id, size='thumbnail'):
        # fullsize only falls back to the original when edits are not requested
        return self.media_url('/assets/{}/thumbnail'.format(asset_id), size=size,
                              edited=None if size == 'fullsize' else 'true')

    def person_thumb_url(self, person_id):
        return self.media_url('/people/{}/thumbnail'.format(person_id))

    def video_url(self, asset_id):
        return self.media_url('/assets/{}/video/playback'.format(asset_id))

    def original_url(self, asset_id):
        return self.media_url('/assets/{}/original'.format(asset_id))


def _filters(filters):
    out = {'visibility': 'timeline', 'withStacked': 'true'}
    out.update({k: ('true' if v is True else 'false' if v is False else v) for k, v in filters.items()})
    return out


def _rows(cols):
    """Timeline buckets come back column-wise; turn them into one dict per asset."""
    ids = cols.get('id') or []
    keys = [k for k, v in cols.items() if isinstance(v, list) and len(v) == len(ids)]
    return [{k: cols[k][i] for k in keys} for i in range(len(ids))]


def _iso(text):
    try:
        return datetime.strptime(text[:19], '%Y-%m-%dT%H:%M:%S')
    except (TypeError, ValueError):
        return None


def _seconds(value):
    """Immich 3 sends milliseconds; 2.x sent '0:01:02.345'."""
    if isinstance(value, (int, float)):
        return int(value // 1000)
    try:
        h, m, s = (value or '').split(':')
        return int(int(h) * 3600 + int(m) * 60 + float(s))
    except ValueError:
        return 0


def from_bucket(row):
    taken = _iso(row.get('fileCreatedAt'))
    if taken is not None:
        taken += timedelta(hours=row.get('localOffsetHours') or 0)
    return {'id': row['id'], 'image': bool(row.get('isImage')), 'taken': taken,
            'ratio': row.get('ratio') or 1.0, 'duration': _seconds(row.get('duration')),
            'city': row.get('city'), 'country': row.get('country'),
            'live': row.get('livePhotoVideoId'), 'favorite': bool(row.get('isFavorite')),
            'thumbhash': row.get('thumbhash')}


def from_asset(a):
    exif = a.get('exifInfo') or {}
    width, height = exif.get('exifImageWidth') or a.get('width'), exif.get('exifImageHeight') or a.get('height')
    if exif.get('orientation') in ('5', '6', '7', '8', 5, 6, 7, 8):
        width, height = height, width
    return {'id': a['id'], 'image': a.get('type') != 'VIDEO', 'taken': _iso(a.get('localDateTime')),
            'ratio': (width / height) if width and height else 1.0,
            'duration': _seconds(a.get('duration')), 'city': exif.get('city'),
            'country': exif.get('country'), 'live': a.get('livePhotoVideoId'),
            'favorite': bool(a.get('isFavorite')), 'thumbhash': a.get('thumbhash'),
            'mime': a.get('originalMimeType'),
            'orientation': str(exif.get('orientation') or 1) if 'exifInfo' in a else None}

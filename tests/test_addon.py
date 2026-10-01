"""Offline regression tests against the fake server; never the real one or the real Kodi settings."""
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import unittest
from http.client import IncompleteRead
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock
from urllib.request import Request, urlopen

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRATCH = tempfile.mkdtemp(prefix='immich-tests-')
tempfile.tempdir = SCRATCH                      # the stub profile goes in here
sys.dont_write_bytecode = True
sys.path[:0] = [os.path.join(ROOT, 'tests', 'stubs'), os.path.join(ROOT, 'tests'), ROOT]

import mock_immich


def serve(handler):
    server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f'http://127.0.0.1:{server.server_address[1]}'


class Quiet(mock_immich.Handler):
    def log_message(self, fmt, *args):
        pass

    def do_HEAD(self):
        """Headers only, as Immich answers the HEAD Kodi sends before an image."""
        self.send_response(200)
        self.send_header('Content-Type', 'image/png')
        self.send_header('Content-Length', '0')
        self.end_headers()


class Broken(BaseHTTPRequestHandler):
    """/api/401 and /api/403 refuse, /api/stall stops mid-body, anything else is cut off mid-chunk."""
    protocol_version = 'HTTP/1.1'

    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        path = self.path.split('?')[0]
        if path in ('/api/401', '/api/403'):
            body = b'{"message": "Missing required permission: album.read"}'
            self.send_response(int(path[-3:]))
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(200)
        if path == '/api/stall':
            self.send_header('Content-Length', '100')
            self.end_headers()
            self.wfile.write(b'{"a"')
            self.wfile.flush()
            time.sleep(1.5)
            return
        self.send_header('Content-Type', 'image/png')
        self.send_header('Transfer-Encoding', 'chunked')
        self.end_headers()
        self.wfile.write(b'10000\r\n' + b'x' * 5000)
        self.wfile.flush()
        self.connection.shutdown(socket.SHUT_RDWR)


MOCK_SERVER, MOCK = serve(Quiet)
BROKEN_SERVER, BROKEN = serve(Broken)
HOME = os.environ.get('HOME')
os.environ['HOME'] = SCRATCH                    # hides the real Kodi profile from the stub
os.environ['IMMICH_URL'] = MOCK
os.environ['IMMICH_KEY'] = mock_immich.KEY

import xbmc
import xbmcaddon
import xbmcgui

from resources.lib import api, kodi, plugin, proxy, service, signin, viewer

THUMBHASH = '1QcSHQRnh493V4dIh4eXh1h4kJUI'
SCHEMES = {'immich': 'http://immich', 'immich:2283': 'http://immich:2283', 'photos.local': 'http://photos.local',
           'nas.lan:2283': 'http://nas.lan:2283', '192.168.1.5:2283': 'http://192.168.1.5:2283',
           '127.0.0.1': 'http://127.0.0.1', '169.254.1.1': 'http://169.254.1.1',
           '[fe80::1]:2283': 'http://[fe80::1]:2283', 'fe80::1': 'http://fe80::1',
           '8.8.8.8': 'https://8.8.8.8', '8.8.8.8:2283': 'https://8.8.8.8:2283',
           '[2606:4700::1111]:2283': 'https://[2606:4700::1111]:2283',
           'photos.example.com': 'https://photos.example.com',
           'photos.example.com:2283': 'https://photos.example.com:2283',
           'http://8.8.8.8:2283/': 'http://8.8.8.8:2283', 'https://nas.lan/api': 'https://nas.lan'}


def tearDownModule():
    for server in (MOCK_SERVER, BROKEN_SERVER):
        server.shutdown()
        server.server_close()
    if HOME is not None:
        os.environ['HOME'] = HOME
    shutil.rmtree(SCRATCH, ignore_errors=True)


def png_alpha(path):
    """Rows of alpha values from an 8-bit RGBA PNG, without PIL."""
    import struct
    import zlib
    data = open(path, 'rb').read()
    pos, chunks, w = 8, b'', 0
    while pos < len(data):
        size, kind = struct.unpack('>I4s', data[pos:pos + 8])
        body = data[pos + 8:pos + 8 + size]
        if kind == b'IHDR':
            w, _, depth, colour = struct.unpack('>IIBB', body[:10])
            assert (depth, colour) == (8, 6), 'expects 8-bit RGBA'
        elif kind == b'IDAT':
            chunks += body
        pos += 12 + size
    raw, stride, rows, prev = zlib.decompress(chunks), w * 4, [], bytearray(w * 4)
    for i in range(0, len(raw), stride + 1):
        kind, line = raw[i], bytearray(raw[i + 1:i + 1 + stride])
        for x in range(stride):
            a = line[x - 4] if x >= 4 else 0
            b, c = prev[x], prev[x - 4] if x >= 4 else 0
            if kind == 1:
                line[x] = (line[x] + a) & 255
            elif kind == 2:
                line[x] = (line[x] + b) & 255
            elif kind == 3:
                line[x] = (line[x] + (a + b) // 2) & 255
            elif kind == 4:
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                line[x] = (line[x] + (a if pa <= pb and pa <= pc else b if pb <= pc else c)) & 255
        rows.append(line[3::4])
        prev = line
    return rows


def make_viewer(assets):
    return viewer.Viewer('script-immich-viewer.xml', ROOT, 'default', '1080i',
                         client=api.ImmichClient(), assets=assets, start=0, autoplay=True)


class Music(xbmc.Player):
    def isPlaying(self):
        return True


class Video(Music):
    def isPlayingVideo(self):
        return True


class CutOff:
    closed = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def read(self):
        raise IncompleteRead(b'')

    def close(self):
        self.closed = True


class NoProxy(unittest.TestCase):
    def setUp(self):
        patch = mock.patch.object(api, 'proxy_address', return_value='http://127.0.0.1:1')
        patch.start()
        self.addCleanup(patch.stop)


class Harness(unittest.TestCase):
    def test_fake_server_only(self):
        self.assertTrue(xbmcaddon._S.startswith(SCRATCH))
        self.assertEqual((kodi.setting('server_url'), kodi.setting('api_key')), (MOCK, mock_immich.KEY))
        self.assertEqual(api.ImmichClient().server_version(), (3, 2, 2))


class ReadErrors(unittest.TestCase):
    def test_truncated_body(self):
        with self.assertRaises(api.ApiError):
            api.ImmichClient(BROKEN, 'k').get('/assets')

    def test_stalled_body(self):
        with self.assertRaises(api.ApiError):
            api.ImmichClient(BROKEN, 'k', timeout=0.5).get('/stall')

    def test_response_closed(self):
        resp = CutOff()
        with mock.patch.object(api, 'urlopen', return_value=resp), self.assertRaises(api.ApiError):
            api.ImmichClient(BROKEN, 'k').get('/assets')
        self.assertTrue(resp.closed)

    def test_callers_degrade(self):
        probe = plugin.Probe()
        self.assertIs(probe(api.ImmichClient(BROKEN, 'k').albums, True, False), False)
        self.assertTrue(probe.offline)
        window = make_viewer([])
        window.raws, window.quick = {}, api.ImmichClient(BROKEN, 'k')
        self.assertIsNone(window.raw({'id': 'c4ca4238-a0b9-2382-0dcc-509a6f75849b'}))


class Refusals(unittest.TestCase):
    def test_403_is_a_permission(self):
        for path, text in (('/401', kodi.L(30615)), ('/403', kodi.L(30627))):
            with self.subTest(path=path):
                with self.assertRaises(api.AuthError) as e:
                    api.ImmichClient(BROKEN, 'k').get(path)
                self.assertEqual(str(e.exception), text)


class CleanUrl(unittest.TestCase):
    def test_default_scheme(self):
        for typed, want in SCHEMES.items():
            with self.subTest(typed=typed):
                self.assertEqual(api.clean_url(typed), want)


class Proxy(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = proxy.start()
        cls.monitor = service.Monitor(cls.server)
        cls.base = proxy.address()
        cls.asset = api.ImmichClient().search_page(size=1)[0][0]['id']

    @classmethod
    def tearDownClass(cls):
        proxy.stop(cls.server)

    def test_settings_published_together(self):
        self.assertEqual(self.server.upstream, (MOCK, mock_immich.KEY))
        with urlopen(f'{self.base}/api/assets/{self.asset}/thumbnail', timeout=10) as resp:
            self.assertEqual((resp.status, resp.read(8)), (200, b'\x89PNG\r\n\x1a\n'))

    def done(self, since):
        entries = [e.rpartition('@') for e in xbmcgui.Window(10000).getProperty(proxy.DONE).split()]
        return [name for name, _, at in entries if float(at) >= since]

    def test_finished_photo_announced(self):
        since = time.time()
        url = f'{self.base}/api/assets/{self.asset}/thumbnail?size=fullsize'
        urlopen(Request(url, method='HEAD'), timeout=10).close()
        time.sleep(0.2)
        self.assertEqual(self.done(since), [])
        with urlopen(url, timeout=10) as resp:
            resp.read()
        time.sleep(0.2)
        self.assertEqual(self.done(since), [f'{self.asset}:fullsize'])

    def test_cut_off_photo_not_announced(self):
        settings = {'server_url': BROKEN, 'api_key': 'k'}
        with mock.patch.object(kodi, 'fresh_setting', side_effect=settings.get):
            self.monitor.onSettingsChanged()
        self.addCleanup(self.monitor.onSettingsChanged)
        since = time.time()
        with urlopen(f'{self.base}/api/assets/{self.asset}/thumbnail?size=fullsize', timeout=10) as resp:
            try:
                resp.read()
            except IncompleteRead:
                pass
        time.sleep(0.2)
        self.assertEqual(self.done(since), [])

    def test_upstream_cut_off(self):
        errors = []
        settings = {'server_url': BROKEN, 'api_key': 'k'}
        with mock.patch.object(kodi, 'fresh_setting', side_effect=settings.get):
            self.monitor.onSettingsChanged()
        self.addCleanup(self.monitor.onSettingsChanged)
        with mock.patch.object(self.server, 'handle_error', side_effect=lambda *a: errors.append(sys.exc_info()[1])):
            with urlopen(f'{self.base}/api/assets/{self.asset}/original', timeout=10) as resp:
                resp.read()
            time.sleep(0.2)
        self.assertEqual(errors, [])


class Slideshow(NoProxy):
    def run_viewer(self, player):
        assets = api.ImmichClient().search_page(size=2)[0]
        window = make_viewer(assets)
        window.actions = [10]
        with mock.patch.object(xbmc, 'Player', player):
            window.run()
        return window

    def test_music_keeps_playing(self):
        xbmc.BUILTINS.clear()
        self.run_viewer(Music)
        self.assertNotIn(viewer.STOP, xbmc.BUILTINS)
        self.run_viewer(Video)
        self.assertIn(viewer.STOP, xbmc.BUILTINS)

    def test_one_monitor(self):
        api.aborting()
        made = []

        class Counted(xbmc.Monitor):
            def __init__(self):
                super().__init__()
                made.append(self)

        with mock.patch.object(xbmc, 'Monitor', Counted):
            self.run_viewer(xbmc.Player)
        self.assertEqual(len(made), 1)

    def test_backdrop_ignores_unsafe_id(self):
        window = make_viewer([])
        found = window.backdrop({'id': '../escaped', 'thumbhash': THUMBHASH})
        self.assertFalse(os.path.exists(os.path.join(kodi.PROFILE, 'escaped.png')))
        self.assertFalse(found.endswith('.png'))
        good = window.backdrop({'id': 'c4ca4238-a0b9-2382-0dcc-509a6f75849b', 'thumbhash': THUMBHASH})
        self.assertTrue(os.path.exists(good))


class Recorder:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        return lambda *a, **k: self.calls.append((name,) + a)

    def zooms(self):
        """(start, end, center) of each zoom animation set, as the control sees them."""
        found = []
        for call in self.calls:
            if call[0] == 'setAnimations':
                for _, spec in call[1]:
                    if 'effect=zoom' in spec:
                        f = dict(p.split('=', 1) for p in spec.split() if '=' in p)
                        found.append((float(f['start']), float(f['end']), f['center']))
        return found

    def last(self):
        """The animations of the last setAnimations call, parsed."""
        call = [c for c in self.calls if c[0] == 'setAnimations'][-1]
        return [dict(p.split('=', 1) for p in spec.split() if '=' in p) for _, spec in call[1]]

    def net_zoom(self):
        """(start, end) of the last zooms set, chained legs combined, to 2 places."""
        legs = [f for f in self.last() if f['effect'] == 'zoom']
        start, end = float(legs[0]['start']), float(legs[0]['end'])
        for f in legs[1:]:
            start, end = start * float(f['start']) / 100, end * float(f['end']) / 100
        return round(start, 2), round(end, 2)

    def net_slide(self):
        """((x, y) start, (x, y) end) of the last slides set, chained legs added, to 2 places."""
        legs = [[tuple(float(n) for n in f[k].split(',')) for k in ('start', 'end')]
                for f in self.last() if f['effect'] == 'slide']
        if not legs:
            return None
        (sx, sy), (ex, ey) = legs[0]
        for (ax, ay), (bx, by) in legs[1:]:
            sx, sy, ex, ey = sx + ax, sy + ay, ex + bx, ey + by
        return (round(sx, 2), round(sy, 2)), (round(ex, 2), round(ey, 2))

    def slides(self):
        """(start, end, tween, easing, time) of each slide animation set."""
        found = []
        for call in self.calls:
            if call[0] == 'setAnimations':
                for _, spec in call[1]:
                    if 'effect=slide' in spec:
                        f = dict(p.split('=', 1) for p in spec.split() if '=' in p)
                        pair = lambda v: tuple(float(n) for n in v.split(','))
                        found.append((pair(f['start']), pair(f['end']), f.get('tween'), f.get('easing'), f['time']))
        return found


class KenBurns(NoProxy):
    def viewer(self, kenburns=True, playing=True, height='1080', ratio=16 / 9, zoom=110, size=None, hires=False):
        assets = api.ImmichClient().search_page(size=2)[0]
        assets[0]['ratio'] = ratio
        assets[0]['width'], assets[0]['height'] = size or (None, None)
        window = make_viewer(assets)
        self.window = window
        window.playing = playing
        controls = {}
        with mock.patch.object(window, 'getControl', lambda i: controls.setdefault(i, Recorder())), \
                mock.patch.object(xbmc, 'getInfoLabel', lambda s: height if s == 'System.ScreenHeight' else ''), \
                mock.patch.object(kodi, 'setting_bool', lambda k: {'kenburns': kenburns, 'hires': hires}.get(k, False)), \
                mock.patch.object(window.client, 'key_info', lambda: {'permissions': ['all']}), \
                mock.patch.object(kodi, 'setting_int', lambda k: {'slide_time': 5, 'zoom': zoom}.get(k, 0)):
            window.setup()
            window.display(0)
        main = window.layers[0][2] if window.prepared.get(0, (0, True))[1] else window.layers[0][3]
        return window.main or main

    def test_photo_decoded_at_largest_frame(self):
        main = self.viewer()
        names = [c[0] for c in main.calls]
        self.assertIn(('setWidth', 2112), main.calls)
        self.assertIn(('setHeight', 1188), main.calls)
        loaded = [i for i, c in enumerate(main.calls) if c[0] == 'setImage' and c[1]]
        self.assertTrue(loaded and names.index('setWidth') < loaded[0])

    def test_zoom_only_shrinks(self):
        main = self.viewer()
        self.assertEqual(sorted(main.net_zoom()), [round(100 / 1.1, 2), 100.0])
        cx, cy = (int(v) for v in main.last()[0]['center'].split(','))
        self.assertIn(('setPosition', round(cx * (1 - 1.1)), round(cy * (1 - 1.1))), main.calls)

    def test_still_photo_fills_screen(self):
        main = self.viewer(playing=False)
        self.assertEqual(main.zooms()[-1:], [(round(100 / 1.1, 3), round(100 / 1.1, 3), '960,540')])
        self.assertIn(('setPosition', -96, -54), main.calls)

    def test_4k_interface_outsizes_too(self):
        main = self.viewer(height='2160')
        self.assertIn(('setWidth', 2112), main.calls)
        self.assertEqual(sorted(main.net_zoom()), [round(100 / 1.1, 2), 100.0])

    def test_off_leaves_controls_alone(self):
        main = self.viewer(kenburns=False)
        self.assertFalse([c for c in main.calls if c[0] in ('setWidth', 'setHeight', 'setPosition')])
        self.assertEqual(main.zooms(), [])

    def test_portrait_fits_over_backdrop(self):
        main = self.viewer(ratio=0.75)
        back = self.window.layers[0][1]
        self.assertIs(main, self.window.layers[0][3])
        self.assertTrue([c for c in back.calls if c[0] == 'setImage' and c[1]])
        self.assertIsNone(main.net_slide())
        self.assertEqual(sorted(main.net_zoom()), [round(100 / 1.1, 2), 100.0])

    def test_three_two_fits_over_backdrop(self):
        main = self.viewer(ratio=1.5)
        self.assertIs(main, self.window.layers[0][3])

    def test_short_panorama_fits(self):
        main = self.viewer(height='2160', ratio=3.92, size=(4000, 1020), hires=True)
        self.assertIs(main, self.window.layers[0][3])
        self.assertIsNone(main.net_slide())

    def test_panorama_pans_sideways(self):
        main = self.viewer(ratio=3.92)
        self.assertIn(('setWidth', 4657), main.calls)
        (x0, y0), (_, y1) = main.net_slide()
        self.assertEqual((abs(x0), y0, y1), (round(0.015 * 1920 * 9.6 / 2, 2), 0.0, 0.0))

    def test_four_three_keeps_zooming(self):
        main = self.viewer(ratio=4 / 3)
        self.assertEqual(main.slides(), [])
        self.assertEqual(sorted(main.net_zoom()), [round(100 / 1.1, 2), 100.0])

    def test_paused_pan_photo_shows_centre(self):
        main = self.viewer(ratio=3.92, playing=False)
        self.assertEqual(main.slides(), [])
        self.assertEqual(main.zooms()[-1:], [(round(100 / 1.1, 3), round(100 / 1.1, 3), '960,540')])

    def test_pan_holds_where_it_got_to(self):
        main = self.viewer(ratio=3.92)
        kb = self.window.kb
        kb['t0'] -= kb['time'] / 2000.0                     # half way through
        self.window.freeze()
        self.assertTrue(main.net_slide(), 'no pan held')
        (x0, _), (x1, _) = main.net_slide()
        done = viewer.ease(0.5, 'skew')                     # share of the way at half the time
        (d0, _), (d1, _) = self.window.kb['pan']
        self.assertEqual((round(x0, 1), round(x1, 1)), (round(d0 + (d1 - d0) * done, 1),) * 2)
        zoom = main.net_zoom()
        self.assertAlmostEqual(zoom[0], 100 * (1 + 0.1 * done) / 1.1, delta=0.05)
        self.assertEqual(zoom[0], zoom[1])

    def test_motion_rises_fast_and_settles_slowly(self):
        for ratio in (16 / 9, 3.92):                        # zoom, then pan
            main = self.viewer(ratio=ratio)
            legs = [f for f in main.last() if f['effect'] == 'zoom']
            ms = self.window.kb['time']
            self.assertEqual([(f.get('tween'), f.get('easing')) for f in legs], [('sine', 'in'), ('sine', 'out')])
            self.assertEqual((int(legs[0]['time']), int(legs[1]['delay']), int(legs[1]['time'])),
                             (round(ms * 0.3), round(ms * 0.3), ms - round(ms * 0.3)))
            z0, z1 = float(legs[0]['start']), float(legs[0]['end']) * float(legs[1]['end']) / 100
            self.assertAlmostEqual((float(legs[0]['end']) - z0) / (z1 - z0), 0.3, delta=0.01)

    def test_zoom_setting_capped(self):
        self.viewer(zoom=125)
        self.assertEqual(self.window.zoom, 110)

    def test_zoom_stops_at_original_pixels(self):
        main = self.viewer(height='2160', size=(4000, 2250), hires=True)
        top = 2250 / 2160                                   # one original pixel per screen pixel
        self.assertEqual(sorted(main.net_zoom()), [round(100 / 1.1, 2), round(100 * top / 1.1, 2)])

    def test_panorama_stops_at_original_pixels(self):
        main = self.viewer(height='2160', ratio=3.92, size=(12672, 2200), hires=True)
        self.assertEqual(sorted(main.net_zoom()), [round(100 / 1.1, 2), round(100 * (2200 / 2160) / 1.1, 2)])
        self.assertTrue(main.net_slide())

    def test_preview_limits_zoom(self):
        main = self.viewer(height='2160', ratio=4 / 3, size=(4080, 3060))
        start, end = main.net_zoom()                       # a 1440 preview is already past one to one
        self.assertEqual((start, end), (round(100 / 1.1, 2),) * 2)

    def test_unknown_size_uses_setting(self):
        main = self.viewer(height='2160')
        self.assertEqual(sorted(main.net_zoom()), [round(100 / 1.1, 2), 100.0])

    def test_delay_follows_skin_speed(self):
        with mock.patch.object(viewer, 'skin_slowdown', lambda: 0.5):
            spec = viewer.anim('zoom', start=100, end=110, time=1000, delay=300)[1]
        f = dict(p.split('=', 1) for p in spec.split() if '=' in p)
        self.assertEqual((f['time'], f['delay']), ('2000', '600'))

    def test_resume_settles(self):
        main = self.viewer(ratio=3.92)
        kb = self.window.kb
        kb['t0'] -= kb['time'] / 4000.0                     # a quarter through
        self.window.freeze()
        left = kb['time']
        self.window.thaw()
        legs = [f for f in main.last() if f['effect'] in ('zoom', 'slide')]
        self.assertEqual({(f['tween'], f['easing'], int(f['time'])) for f in legs}, {('sine', 'out', int(left))})
        self.assertFalse([f for f in legs if 'delay' in f])


class Loading(NoProxy):
    def viewer(self):
        window = make_viewer(api.ImmichClient().search_page(size=5)[0])
        window.getControl = lambda i: Recorder()
        with mock.patch.object(kodi, 'setting_bool', lambda k: k == 'hires'), \
                mock.patch.object(window.client, 'key_info', lambda: {'permissions': ['all']}):
            window.setup()
        return window

    def announce(self, token, at):
        xbmcgui.Window(10000).setProperty(proxy.DONE, f'{token}@{at}')

    def test_next_photo_waited_for(self):
        window = self.viewer()
        self.announce('', 0)
        window.prepare(1, 1)
        token = f"{window.assets[1]['id']}:fullsize"
        now = time.time()
        self.assertFalse(window.ready(1, now))
        self.announce(token, now)
        self.assertTrue(window.ready(1, now))

    def test_old_announcement_ignored(self):
        window = self.viewer()
        token = f"{window.assets[1]['id']}:fullsize"
        self.announce(token, time.time() - 60)
        window.prepare(1, 1)
        self.assertFalse(window.ready(1, time.time()))

    def test_gives_up_waiting(self):
        window = self.viewer()
        self.announce('', 0)
        window.prepare(1, 1)
        self.assertTrue(window.ready(1, time.time() + viewer.LOAD_WAIT + 1))

    def test_slideshow_holds_until_loaded(self):
        window = self.viewer()
        self.announce('', 0)
        shown = []
        window.display = lambda index, fade=None: (shown.append(index), setattr(window, 'shown', 0),
                                                   setattr(window, 'next_at', 1), setattr(window, 'preload_at', None))
        ticks = []

        class Ticking(xbmc.Monitor):
            def waitForAbort(self, t=0):
                ticks.append(t)
                if len(ticks) == 5:
                    window.actions.append(10)       # close
                return False

        window.monitor = Ticking()
        window.prepared[1] = (1, True)
        window.waiting[1] = (f"{window.assets[1]['id']}:fullsize", time.time())
        with mock.patch.object(window, 'setup', lambda: None), mock.patch.object(window, 'prepare', lambda *a: None):
            window.run()
        self.assertEqual(shown, [0])                # the first photo only: the next never arrived

    def test_details_fetched_ahead(self):
        window = self.viewer()
        for a in window.assets:
            a['mime'] = a['orientation'] = None     # as memories and timeline months come
        window.display(0)
        if window.ahead:
            window.ahead.join(10)
        self.assertEqual({a['id'] for a in window.assets[1:4]} - set(window.raws), set())


class TextureCache(NoProxy):
    """Kodi 22 caches photos at 720p even with setImage(url, False); the viewer drops those copies first."""
    def fake(self, textures):
        calls = []

        def jsonrpc(method, **params):
            calls.append((method, params))
            if method == 'Textures.GetTextures':
                want = params['filter']
                return {'textures': [t for t in textures if want['operator'] == 'is' and t['url'] == want['value']
                                     or want['operator'] == 'contains' and want['value'] in t['url']]}
            return 'OK'
        return calls, jsonrpc

    def test_cached_copy_dropped_before_showing(self):
        window = make_viewer(api.ImmichClient().search_page(size=2)[0])
        log = []

        class Logged(Recorder):
            def __getattr__(self, name):
                return lambda *a, **k: log.append((name,) + a)

        window.getControl = lambda i: Logged()
        with mock.patch.object(kodi, 'setting_bool', lambda k: False):
            window.setup()
        url = window.client.thumb_url(window.assets[0]['id'], 'preview')
        calls, jsonrpc = self.fake([{'textureid': 7, 'url': url}, {'textureid': 8, 'url': url + 'x'}])
        with mock.patch.object(kodi, 'jsonrpc', side_effect=lambda m, **p: (log.append(m), jsonrpc(m, **p))[1]):
            window.prepare(0, 0)
        self.assertIn(('Textures.RemoveTexture', {'textureid': 7}), calls)
        self.assertNotIn(('Textures.RemoveTexture', {'textureid': 8}), calls)
        shown = [i for i, e in enumerate(log) if e == ('setImage', url, False)]
        self.assertTrue(shown and log.index('Textures.RemoveTexture') < shown[0])

    def test_service_start_drops_cached_originals(self):
        base = 'http://127.0.0.1:52283/api/assets/{}/thumbnail?size={}'
        textures = [{'textureid': 1, 'url': base.format('a', 'fullsize')},
                    {'textureid': 2, 'url': base.format('b', 'preview')},
                    {'textureid': 3, 'url': base.format('c', 'thumbnail')},
                    {'textureid': 4, 'url': 'http://elsewhere/api/assets/d/thumbnail?size=fullsize'}]
        calls, jsonrpc = self.fake(textures)
        with mock.patch.object(kodi, 'jsonrpc', side_effect=jsonrpc), \
                mock.patch.object(proxy, 'address', lambda: 'http://127.0.0.1:52283'):
            proxy.forget_cached_photos()
        self.assertEqual([p['textureid'] for m, p in calls if m == 'Textures.RemoveTexture'], [1])


class Skin(unittest.TestCase):
    def test_photos_keep_subpixel_positions(self):
        import xml.etree.ElementTree as ET
        root = ET.parse(os.path.join(ROOT, 'resources', 'skins', 'default', '1080i', 'script-immich-viewer.xml'))
        photos = {c.get('id'): c.find('texture') for c in root.iter('control') if c.get('id') in ('102', '103', '202', '203')}
        self.assertEqual({i: t is not None and t.get('subpixel') for i, t in photos.items()},
                         {'102': 'true', '103': 'true', '202': 'true', '203': 'true'})

    def test_photo_edges_feathered(self):
        import xml.etree.ElementTree as ET
        skin = os.path.join(ROOT, 'resources', 'skins', 'default')
        root = ET.parse(os.path.join(skin, '1080i', 'script-immich-viewer.xml'))
        masks = {c.get('id'): c.find('texture').get('diffuse') for c in root.iter('control')
                 if c.get('id') in ('102', '103', '202', '203') and c.find('texture') is not None}
        self.assertEqual(masks, dict.fromkeys(('102', '103', '202', '203'), 'edge.png'))
        alpha = png_alpha(os.path.join(skin, 'media', 'edge.png'))
        n = len(alpha)
        self.assertEqual([alpha[y][x] for x, y in ((0, 0), (n // 2, 0), (n - 1, n // 2), (1, 1), (n // 2, n // 2))],
                         [0, 0, 0, 255, 255])


class SignIn(unittest.TestCase):
    def test_api_key_hidden(self):
        typed, answers = [], [MOCK, mock_immich.KEY]

        def answer(dialog, heading, default='', **kwargs):
            typed.append(kwargs)
            return answers.pop(0)

        with mock.patch.object(xbmcgui.Dialog, 'input', answer), \
                mock.patch.object(kodi, 'fresh_setting', return_value=''):
            self.assertTrue(signin.sign_in())
        self.assertEqual(typed[1].get('option'), xbmcgui.ALPHANUM_HIDE_INPUT)


if __name__ == '__main__':
    unittest.main()

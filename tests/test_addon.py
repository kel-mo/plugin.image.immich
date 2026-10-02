"""Offline regression tests against the fake server; never the real one or the real Kodi settings."""
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import unittest
from datetime import date
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
import xbmcplugin

from resources.lib import api, kodi, plugin, proxy, service, signin, tiles, viewer

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
        """(start, end) of the last zooms set, chained legs combined, to 3 places then 2."""
        legs = [f for f in self.last() if f['effect'] == 'zoom']
        start, end = float(legs[0]['start']), float(legs[0]['end'])
        for f in legs[1:]:
            start, end = start * float(f['start']) / 100, end * float(f['end']) / 100
        return round(round(start, 3), 2), round(round(end, 3), 2)

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


OUT = 1.1 * viewer.SUPERSAMPLE                        # photo controls over the screen, at 110%


def net(percent):
    """An on-screen zoom as net_zoom() sees it: shrunk to 3 places, then 2."""
    return round(round(percent / OUT, 3), 2)


class KenBurns(NoProxy):
    def viewer(self, kenburns=True, playing=True, height='1080', ratio=16 / 9, zoom=110, size=None, hires=False,
               smooth=True):
        assets = api.ImmichClient().search_page(size=2)[0]
        assets[0]['ratio'] = ratio
        assets[0]['width'], assets[0]['height'] = size or (None, None)
        window = make_viewer(assets)
        self.window = window
        window.playing = playing
        controls = {}
        with mock.patch.object(window, 'getControl', lambda i: controls.setdefault(i, Recorder())), \
                mock.patch.object(xbmc, 'getInfoLabel', lambda s: height if s == 'System.ScreenHeight' else ''), \
                mock.patch.object(kodi, 'setting_bool', lambda k: {'kenburns': kenburns, 'hires': hires, 'smooth_zoom': smooth}.get(k, False)), \
                mock.patch.object(window.client, 'key_info', lambda: {'permissions': ['all']}), \
                mock.patch.object(kodi, 'setting_int', lambda k: {'slide_time': 5, 'zoom': zoom}.get(k, 0)):
            window.setup()
            window.display(0)
        main = window.layers[0][2] if window.prepared.get(0, (0, True))[1] else window.layers[0][3]
        return window.main or main

    def test_photo_decoded_at_largest_frame(self):
        main = self.viewer()
        names = [c[0] for c in main.calls]
        self.assertIn(('setWidth', round(1920 * OUT)), main.calls)
        self.assertIn(('setHeight', round(1080 * OUT)), main.calls)
        loaded = [i for i, c in enumerate(main.calls) if c[0] == 'setImage' and c[1]]
        self.assertTrue(loaded and names.index('setWidth') < loaded[0])

    def test_zoom_only_shrinks(self):
        main = self.viewer()
        self.assertEqual(sorted(main.net_zoom()), [net(100), net(110)])
        cx, cy = (int(v) for v in main.last()[0]['center'].split(','))
        self.assertIn(('setPosition', round(cx * (1 - OUT)), round(cy * (1 - OUT))), main.calls)

    def test_still_photo_fills_screen(self):
        main = self.viewer(playing=False)
        self.assertEqual(main.zooms()[-1:], [(round(100 / OUT, 3), round(100 / OUT, 3), '960,540')])
        self.assertIn(('setPosition', round(960 * (1 - OUT)), round(540 * (1 - OUT))), main.calls)

    def test_4k_interface_outsizes_too(self):
        main = self.viewer(height='2160')
        self.assertIn(('setWidth', round(1920 * OUT)), main.calls)
        self.assertEqual(sorted(main.net_zoom()), [net(100), net(110)])

    def test_smooth_zoom_off_decodes_at_zoom_size(self):
        main = self.viewer(smooth=False)
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
        self.assertEqual(sorted(main.net_zoom()), [net(100), net(110)])

    def test_three_two_fits_over_backdrop(self):
        main = self.viewer(ratio=1.5)
        self.assertIs(main, self.window.layers[0][3])

    def test_short_panorama_fits(self):
        main = self.viewer(height='2160', ratio=3.92, size=(4000, 1020), hires=True)
        self.assertIs(main, self.window.layers[0][3])
        self.assertIsNone(main.net_slide())

    def test_panorama_pans_sideways(self):
        main = self.viewer(ratio=3.92)
        self.assertIn(('setWidth', round(1080 * 3.92 * OUT)), main.calls)
        (x0, y0), (_, y1) = main.net_slide()
        self.assertEqual((abs(x0), y0, y1), (round(0.015 * 1920 * 9.6 / 2, 2), 0.0, 0.0))

    def test_four_three_keeps_zooming(self):
        main = self.viewer(ratio=4 / 3)
        self.assertEqual(main.slides(), [])
        self.assertEqual(sorted(main.net_zoom()), [net(100), net(110)])

    def test_paused_pan_photo_shows_centre(self):
        main = self.viewer(ratio=3.92, playing=False)
        self.assertEqual(main.slides(), [])
        self.assertEqual(main.zooms()[-1:], [(round(100 / OUT, 3), round(100 / OUT, 3), '960,540')])

    def test_pause_eases_back_to_rest(self):
        for ratio in (16 / 9, 3.92):                        # zoom, then pan
            main = self.viewer(ratio=ratio)
            kb = self.window.kb
            kb['t0'] -= kb['time'] / 2000.0                 # half way through
            mid = self.window.state(viewer.ease(0.5, 'skew'))
            self.window.settle()
            legs = main.last()
            self.assertEqual({(f['tween'], f['easing'], f['time']) for f in legs}, {('sine', 'out', '1500')})
            zoom = next(f for f in legs if f['effect'] == 'zoom')
            self.assertEqual((float(zoom['start']), float(zoom['end'])), (mid[0], round(100 / OUT, 3)))
            slide = [f['end'] for f in legs if f['effect'] == 'slide']
            self.assertEqual(slide, ['0,0'] if ratio > 3 else [])
            self.assertIsNone(self.window.kb)

    def test_resume_starts_gently_from_rest(self):
        for ratio in (16 / 9, 3.92):
            main = self.viewer(ratio=ratio)
            self.window.settle()
            self.window.stay = 3
            self.window.thaw()
            legs = [f for f in main.last() if f['effect'] in ('zoom', 'slide')]
            self.assertEqual({(f['tween'], f['easing']) for f in legs}, {('sine', 'inout')})
            self.assertFalse([f for f in legs if 'delay' in f])
            self.assertEqual(main.net_zoom()[0], net(100))
            if ratio > 3:
                self.assertEqual(main.net_slide()[0], (0.0, 0.0))
            else:
                self.assertEqual(int(legs[0]['time']), int((3000 + self.window.fade) * 1.1))

    def still(self, **kw):
        self.viewer(playing=False, **kw)
        window = self.window
        with mock.patch.object(window.monitor, 'waitForAbort', lambda s: False):
            window.sharpen()
        return window

    def test_paused_photo_gets_a_sharp_still(self):
        window = self.still()
        cover = window.stills[1]
        self.assertIs(window.still['ctrl'], cover)
        self.assertEqual([c for c in cover.calls if c[0] in ('setPosition', 'setWidth', 'setHeight')],
                         [('setPosition', 0, 0), ('setWidth', 1920), ('setHeight', 1080)])
        self.assertTrue([c for c in cover.calls if c[0] == 'setImage' and c[1]])
        self.assertIsNotNone(window.preload_at)             # the next photo still loads behind

    def test_paused_panorama_still_spans(self):
        window = self.still(ratio=3.92)
        fit = window.stills[2]
        self.assertIs(window.still['ctrl'], fit)
        self.assertIn(('setWidth', round(1080 * 3.92)), fit.calls)

    def test_resume_gives_a_whole_slide(self):
        self.viewer()
        window = self.window
        window.next_at = time.time() + 0.5                  # nearly over when paused
        window.handle(12)
        window.handle(12)
        self.assertAlmostEqual(window.next_at - time.time(), window.stay, delta=0.1)
        window.handle(12)
        window.still = {'ctrl': window.stills[1], 'name': '', 'since': 0, 'arrived': None, 'shown': True}
        window.handle(12)                                   # waits for the still to fade first
        self.assertAlmostEqual(window.next_at - time.time(), window.stay + viewer.SWAP, delta=0.1)

    def test_paused_slide_schedules_the_still(self):
        self.viewer(playing=False)
        self.assertEqual(self.window.still_at, self.window.preload_at)
        self.viewer(playing=False, smooth=False)
        self.assertIsNotNone(self.window.still_at)          # still outsized by the zoom
        self.viewer(playing=False, kenburns=False)
        self.assertIsNone(self.window.still_at)

    def test_still_fades_over_the_moving_photo(self):
        window = self.still()
        window.show_still()
        (fade,) = window.stills[0].last()
        self.assertEqual((fade['start'], fade['end'], fade['time']), ('0', '100', '600'))
        layers = [len(layer[0].calls) for layer in window.layers]
        moves = len(window.main.calls)
        window.stay = 3
        window.thaw()
        legs = [f for f in window.main.last() if f['effect'] == 'zoom']
        self.assertEqual({(f['delay'], f['center']) for f in legs}, {('600', '960,540')})   # after the still, in place
        self.assertFalse([c for c in window.main.calls[moves:] if c[0] == 'setPosition'])
        (fade,) = window.stills[0].last()
        self.assertEqual((fade['start'], fade['end'], fade['time']), ('100', '0', '600'))
        self.assertEqual([len(layer[0].calls) for layer in window.layers], layers)   # photo layers untouched
        self.assertIsNone(window.still)
        self.assertIs(window.unstill[0], window.stills[1])  # texture let go once faded
        self.assertEqual(window.main.net_zoom()[0], net(100))

    def test_next_photo_fades_the_still_with_it(self):
        window = self.still()
        window.show_still()
        with mock.patch.object(window.monitor, 'waitForAbort', lambda s: False):
            window.display(1, 500)
        (fade,) = window.stills[0].last()
        self.assertEqual((fade['start'], fade['end'], fade['time']), ('100', '0', '500'))

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
        self.assertEqual(sorted(main.net_zoom()), [net(100), net(100 * top)])

    def test_panorama_stops_at_original_pixels(self):
        main = self.viewer(height='2160', ratio=3.92, size=(12672, 2200), hires=True)
        self.assertEqual(sorted(main.net_zoom()), [net(100), net(100 * (2200 / 2160))])
        self.assertTrue(main.net_slide())

    def test_enlarged_preview_still_zooms(self):
        main = self.viewer(height='2160', ratio=4 / 3, size=(4080, 3060))
        self.assertEqual(sorted(main.net_zoom()), [net(100), net(110)])   # a 1440 preview is already past one to one
        main = self.viewer(height='2160', ratio=0.75, size=(3060, 4080))    # a rotated phone portrait
        self.assertEqual(sorted(main.net_zoom()), [net(100), net(110)])

    def test_unknown_size_uses_setting(self):
        main = self.viewer(height='2160')
        self.assertEqual(sorted(main.net_zoom()), [net(100), net(110)])

    def test_delay_follows_skin_speed(self):
        with mock.patch.object(viewer, 'skin_slowdown', lambda: 0.5):
            spec = viewer.anim('zoom', start=100, end=110, time=1000, delay=300)[1]
        f = dict(p.split('=', 1) for p in spec.split() if '=' in p)
        self.assertEqual((f['time'], f['delay']), ('2000', '600'))


try:
    import PIL                                          # in Kodi's flatpak Python, not always on the host
except ImportError:
    PIL = None


class Tiles(NoProxy):
    def setUp(self):
        super().setUp()
        shutil.rmtree(tiles.tile_dir(), ignore_errors=True)
        self.addCleanup(shutil.rmtree, tiles.tile_dir(), True)
        self.client = api.ImmichClient()

    def ids(self, assets):
        return {a['id'] for a in assets}

    def test_on_this_day_prefers_a_memory(self):
        memories = self.ids(a for _, assets in self.client.memories(date.today()) for a in assets)
        self.assertIn(tiles.pick(self.client, 'memories', '2026-10-02')['id'], memories)

    def test_on_this_day_falls_back_to_any_photo(self):
        with mock.patch.object(self.client, 'memories', lambda day: []):
            picked = tiles.pick(self.client, 'memories', '2026-10-02')
        self.assertIn(picked['id'], self.ids(mock_immich.ASSETS))

    def test_albums_use_the_newest_cover(self):
        self.assertEqual(tiles.pick(self.client, 'albums', '2026-10-02')['id'], mock_immich.ASSETS[0]['id'])

    def test_tiles_show_different_photos(self):
        memories = [a['id'] for _, assets in self.client.memories(date.today()) for a in assets]
        days = tiles.pick(self.client, 'memories', '2026-10-02')['id']
        other = tiles.pick(self.client, 'memories', '2026-10-02', {days})['id']
        self.assertIn(other, memories)
        self.assertNotEqual(other, days)
        only = {a['id'] for a in mock_immich.ASSETS}           # nothing new left: share rather than show nothing
        self.assertIn(tiles.pick(self.client, 'memories', '2026-10-02', only)['id'], memories)

    def test_timeline_shows_the_newest_day(self):
        self.assertEqual(tiles.pick(self.client, 'timeline', '2026-10-02')['id'], mock_immich.ASSETS[0]['id'])

    def test_same_photo_all_day(self):
        days = [tiles.pick(self.client, 'memories', day)['id'] for day in ('2026-10-02', '2026-10-02')]
        self.assertEqual(days[0], days[1])

    def test_offline_gives_no_photo(self):
        self.assertIsNone(tiles.pick(api.ImmichClient(BROKEN, 'k', timeout=0.5), 'memories', '2026-10-02'))

    def test_without_pil_the_photo_is_used(self):
        dest = os.path.join(kodi.ensure_dir(tiles.tile_dir()), 'x.jpg')
        with mock.patch.dict(sys.modules, {'PIL': None}):
            tiles.render(b'photo', 'Immich', dest)
        with open(dest, 'rb') as f:
            self.assertEqual(f.read(), b'photo')
        self.assertFalse(os.path.exists(dest + '.tmp'))

    @unittest.skipUnless(PIL, 'needs PIL')
    def test_tile_is_square_dark_and_labelled(self):
        from PIL import Image
        dest = os.path.join(kodi.ensure_dir(tiles.tile_dir()), 'x.jpg')
        photo = self.client.image(mock_immich.ASSETS[0]['id'])
        with mock.patch.object(tiles, 'font_file', lambda: (None, False)):
            tiles.render(photo, 'Immich', dest)
        im = Image.open(dest).convert('L')
        self.assertEqual(im.size, (tiles.SIZE, tiles.SIZE))
        centre = im.crop((128, 216, 384, 296)).getextrema()
        self.assertEqual(centre[1], 255)                    # white text in the middle
        self.assertLess(im.crop((0, 0, 64, 64)).getextrema()[1], 160)   # darkened photo at the corner
        row = [im.getpixel((x, tiles.SIZE // 2)) for x in range(tiles.SIZE)]
        first = next(x for x, v in enumerate(row) if v > 240)
        with mock.patch.object(tiles, 'font_file', lambda: (None, False)), mock.patch.object(tiles, 'SHADOW', 0):
            tiles.render(photo, 'Immich', dest)
        plain = Image.open(dest).convert('L')
        darkest = lambda i: min(i.getpixel((x, tiles.SIZE // 2)) for x in range(first - 20, first))
        self.assertLess(darkest(im), darkest(plain) * 0.6)                                   # the halo beside the text

    @unittest.skipUnless(PIL, 'needs PIL')
    def test_one_text_size_fits_every_name(self):
        from PIL import Image, ImageDraw
        names = ['Places', 'On this day and more']
        with mock.patch.object(tiles, 'font_file', lambda: (None, False)):
            size = tiles.text_size(names)
            face, stroke = tiles.font(size)
        draw = ImageDraw.Draw(Image.new('L', (1, 1)))
        for name in names:
            for line in tiles.lines(draw, name, face, stroke, tiles.fit(size)).split('\n'):
                self.assertLessEqual(draw.textbbox((0, 0), line, font=face, stroke_width=stroke)[2], tiles.fit(size))

    @unittest.skipUnless(PIL, 'needs PIL')
    def test_long_names_wrap_rather_than_shrink(self):
        from PIL import Image, ImageDraw
        with mock.patch.object(tiles, 'font_file', lambda: (None, False)):
            self.assertEqual(tiles.text_size(['Search', 'Smart collections']), tiles.text_size(['collections']))
            self.assertLess(tiles.text_size(['Search', 'Supercalifragilistic']), tiles.text_size(['Search']))
            size = tiles.text_size(['Search', 'Smart collections'])
            face, stroke = tiles.font(size)
            draw = ImageDraw.Draw(Image.new('L', (1, 1)))
            self.assertEqual(tiles.lines(draw, 'Smart collections', face, stroke, tiles.fit(size)), 'Smart\ncollections')
            self.assertEqual(tiles.lines(draw, 'Search', face, stroke, tiles.fit(size)), 'Search')

    @unittest.skipUnless(PIL, 'needs PIL')
    def test_halo_sits_on_wrapped_lines(self):
        from PIL import Image, ImageDraw
        with mock.patch.object(tiles, 'font_file', lambda: (None, False)):
            size = tiles.text_size(['Favourites', 'On this day and more'])
            face, stroke = tiles.font(size)
        draw = ImageDraw.Draw(Image.new('L', (1, 1)))
        text = tiles.lines(draw, 'On this day and more', face, stroke, tiles.fit(size))
        self.assertIn('\n', text)
        mask = tiles.text_mask(text, face, stroke, size)
        glyphs = mask.getbbox()
        halo = tiles.halo(mask, size).point(lambda v: 255 if v > 40 else 0).getbbox()
        top, bottom = glyphs[1] - halo[1], halo[3] - glyphs[3]
        self.assertLessEqual(abs(top - bottom), 3)           # as far past the last line as the first

    @unittest.skipUnless(PIL, 'needs PIL')
    def test_names_drawn_past_the_fonts_hinting(self):
        with mock.patch.object(tiles, 'font_file', lambda: (None, False)):
            for size in (40, 72, 81, 102):
                face, stroke = tiles.font(size)
                self.assertGreater(face.size, 200, size)          # Estuary's Noto Sans is misdrawn up to 200 px
                self.assertEqual(face.size, size * tiles.scale(size))

    def test_a_refresh_past_midnight_keeps_its_day(self):
        with mock.patch.object(tiles, 'render', lambda data, label, dest, size: open(dest, 'wb').close()), \
                mock.patch.object(kodi, 'jsonrpc', lambda *a, **k: {}), \
                mock.patch.object(tiles, 'drawing', lambda day=None: (day or 'tomorrow') + ' v'):
            tiles.refresh(self.client, xbmc.Monitor())
        with open(tiles.stamp()) as f:
            self.assertEqual(f.read(), date.today().isoformat() + ' v')

    def test_skin_bold_font_first(self):
        skin = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, skin)
        os.makedirs(os.path.join(skin, 'xml'))
        os.makedirs(os.path.join(skin, 'fonts'))
        with open(os.path.join(skin, 'xml', 'Font.xml'), 'w') as f:
            f.write('<fonts><fontset id="Default"><font><name>font13</name><filename>Sans-Regular.ttf</filename></font>'
                    '<font><name>font_bold</name><filename>Sans-Bold.ttf</filename></font></fontset></fonts>')
        for name in ('Sans-Regular.ttf', 'Sans-Bold.ttf'):
            open(os.path.join(skin, 'fonts', name), 'w').close()
        where = lambda p: p.replace('special://skin', skin)
        with mock.patch.object(tiles.xbmcvfs, 'translatePath', where):
            self.assertEqual(tiles.font_file(), (os.path.join(skin, 'fonts', 'Sans-Bold.ttf'), True))
            os.remove(os.path.join(skin, 'fonts', 'Sans-Bold.ttf'))
            self.assertEqual(tiles.font_file(), (os.path.join(skin, 'fonts', 'Sans-Regular.ttf'), False))

    def test_refresh_draws_each_tile_once_a_day(self):
        calls = []
        def rpc(method, **params):
            calls.append(method)
            return {'textures': [{'textureid': 7}]} if method == 'Textures.GetTextures' else {}
        with mock.patch.object(tiles, 'render', lambda data, label, dest, size: open(dest, 'wb').close()), \
                mock.patch.object(kodi, 'jsonrpc', rpc):
            self.assertTrue(tiles.due())
            self.assertEqual(tiles.refresh(self.client, xbmc.Monitor()), len(tiles.FOLDERS))
            self.assertFalse(tiles.due())
        for key in tiles.FOLDERS:
            self.assertEqual(len(tiles.drawn(key)), 1, key)
        self.assertEqual(calls, ['Textures.GetTextures', 'Textures.RemoveTexture'])

    def test_missing_names_wait_for_a_restart(self):
        names = lambda i, *a: '' if i == 30026 else kodi.ADDON.getLocalizedString(i)
        with mock.patch.object(kodi, 'L', names), mock.patch.object(self.client, 'image', lambda *a: self.fail('fetched')):
            self.assertEqual(tiles.refresh(self.client, xbmc.Monitor()), 0)
        self.assertTrue(tiles.due())                         # tried again later
        self.assertEqual(tiles.drawn('shuffle'), [])

    def test_an_update_redraws_the_same_day(self):
        kodi.ensure_dir(tiles.tile_dir())
        with open(tiles.stamp(), 'w') as f:
            f.write(tiles.drawing())
        self.assertFalse(tiles.due())
        with mock.patch.object(kodi, 'ADDON_VERSION', '9.9.9'):
            self.assertTrue(tiles.due())
        with open(tiles.stamp(), 'w') as f:
            f.write(date.today().isoformat())                # as 0.3.1 and 0.3.2 wrote it
        self.assertTrue(tiles.due())

    def test_service_draws_with_a_short_timeout_and_stops_the_proxy_on_exit(self):
        seen, order = [], []
        class Monitor:                                      # one check, then Kodi exits
            waits = iter([False, True])
            def __init__(self, server): pass
            def waitForAbort(self, t): return next(self.waits)
            def abortRequested(self): return False
        with mock.patch.object(proxy, 'start', lambda: object()), mock.patch.object(proxy, 'stop', lambda s: order.append('stop')), \
                mock.patch.object(proxy, 'forget_keyed_textures', lambda: None), \
                mock.patch.object(proxy, 'forget_cached_photos', lambda: None), \
                mock.patch.object(service, 'Monitor', Monitor), mock.patch.object(tiles, 'due', lambda: True), \
                mock.patch.object(tiles, 'refresh', lambda client, monitor: seen.append(client.timeout) or 1):
            service.run()
        self.assertEqual(seen, [service.TILE_TIMEOUT])
        self.assertEqual(order, ['stop'])
        self.assertLessEqual(service.TILE_TIMEOUT, 4)                 # Kodi kills a service still running after five

    def test_off_draws_nothing(self):
        with mock.patch.dict(xbmcaddon.SETTINGS, {'tiles': 'false'}):
            self.assertFalse(tiles.due())

    def draw(self, *names):
        kodi.ensure_dir(tiles.tile_dir())
        for name in names:
            open(os.path.join(tiles.tile_dir(), name), 'w').close()
        return [os.path.join(tiles.tile_dir(), n) for n in names]

    def test_newest_drawing_wins(self):
        old, new = self.draw('timeline-100.jpg', 'timeline-200.jpg')
        self.assertEqual(tiles.current('timeline'), new)
        self.assertIsNone(tiles.current('places'))

    def test_old_drawings_go(self):
        gone, newest, other, stray = self.draw('memories-100.jpg', 'memories-200.jpg', 'timeline-100.jpg', 'immich-100.jpg')
        with mock.patch.object(kodi, 'jsonrpc', lambda *a, **k: {}):
            tiles.tidy()
        self.assertEqual([os.path.exists(p) for p in (gone, newest, other, stray)], [False, True, True, False])

    def test_menu_shows_tiles(self):
        tile, shuffle = self.draw('timeline-100.jpg', 'shuffle-100.jpg')
        del xbmcplugin.ITEMS[:]
        with mock.patch.object(signin, 'is_signed_in', lambda: True), mock.patch.object(signin, 'check', lambda: True):
            plugin.root()
        art = {url.split('action=')[-1]: li.art.get('thumb') for url, li, _ in xbmcplugin.ITEMS}
        self.assertEqual(art['timeline'], tile)
        self.assertEqual(art['play'], shuffle)              # Shuffle everything
        self.assertEqual(art['places'], kodi.ICON)          # no tile drawn yet
        with mock.patch.dict(xbmcaddon.SETTINGS, {'tiles': 'false'}):
            self.assertEqual(tiles.art('timeline'), kodi.ICON)


class Covers(NoProxy):
    """Years, months and countries show a photo, as cities do."""

    def setUp(self):
        super().setUp()
        self.client = api.ImmichClient()
        self.path = os.path.join(kodi.PROFILE, 'covers.json')
        if os.path.exists(self.path):
            os.remove(self.path)

    def listing(self, call):
        del xbmcplugin.ITEMS[:]
        call()
        return {li.label: li.art for _, li, _ in xbmcplugin.ITEMS}

    def test_years_and_months_show_their_newest_photo(self):
        buckets = self.client.timeline_buckets()
        years = self.listing(lambda: plugin.timeline(self.client))
        newest = {b['timeBucket'][:4]: b['timeBucket'] for b in sorted(buckets, key=lambda b: b['timeBucket'])}
        for y, art in years.items():
            first = next(a['id'] for a in self.client.timeline_bucket(newest[y]) if a['image'])
            self.assertIn('/assets/{}/thumbnail'.format(first), art['thumb'], y)
            self.assertIn('size=preview', art['fanart'])
        year = max(years)
        months = self.listing(lambda: plugin.timeline(self.client, year))
        self.assertTrue(months)
        for name, art in months.items():
            self.assertIn('/assets/', art['thumb'], name)

    def test_covers_are_kept_until_a_bucket_changes(self):
        calls = []
        real = self.client.timeline_bucket
        with mock.patch.object(self.client, 'timeline_bucket', lambda key: calls.append(key) or real(key)):
            buckets = self.client.timeline_buckets()
            plugin.timeline(self.client)
            fetched = len(calls)
            plugin.timeline(self.client)                                     # from the cache
            self.assertEqual(len(calls), fetched)
            kept = kodi.read_json(self.path)
            self.assertEqual(sorted(kept), sorted(set(b['timeBucket'] for b in buckets) & set(kept)))
            kept[calls[0]][0] += 1                                           # a photo arrived
            kodi.write_json(self.path, kept)
            plugin.timeline(self.client)
            self.assertEqual(calls[fetched:], [calls[0]])
        gone = dict(kept, **{'1999-01-01T00:00:00.000Z': [1, 'x']})          # a bucket no longer in the timeline
        kodi.write_json(self.path, gone)
        with mock.patch.object(self.client, 'timeline_buckets', lambda: buckets[:1]):
            plugin.timeline(self.client)
        self.assertNotIn('1999-01-01T00:00:00.000Z', kodi.read_json(self.path))

    def test_countries_show_a_city_photo(self):
        countries = self.listing(lambda: plugin.places(self.client))
        self.assertGreater(len(countries), 1)
        for name, art in countries.items():
            self.assertIn('/assets/', art['thumb'], name)
        cities = self.listing(lambda: plugin.places(self.client, 'Australia'))
        self.assertIn(countries['Australia']['thumb'], [a['thumb'] for a in cities.values()])


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


class Screensaver(NoProxy):
    def test_addon_declares_screensaver(self):
        import xml.etree.ElementTree as ET
        points = {e.get('point'): e.get('library') for e in ET.parse(os.path.join(ROOT, 'addon.xml')).iter('extension')}
        self.assertEqual(points.get('xbmc.ui.screensaver'), 'screensaver.py')
        self.assertTrue(os.path.exists(os.path.join(ROOT, 'screensaver.py')))

    def run_screensaver(self, source, found, query=''):
        from resources.lib import screensaver
        shown, self.asked = [], []

        def source_assets(client, params):
            self.asked.append(params)
            return found.get(params['source'], [])
        with mock.patch.object(kodi, 'setting_int', lambda k: source if k == 'screensaver_source' else 0), \
                mock.patch.object(kodi, 'setting', lambda k: query if k == 'screensaver_query' else ''), \
                mock.patch.object(plugin, 'source_assets', source_assets), \
                mock.patch.object(viewer, 'play', lambda client, assets, **kw: shown.append((assets, kw))):
            screensaver.run()
        return shown

    def test_shows_photos_only_as_a_screensaver(self):
        found = {'memories': [{'id': 'p', 'image': True}, {'id': 'v', 'image': False}]}
        shown = self.run_screensaver(0, found)
        (assets, kw), = shown
        self.assertEqual(([a['id'] for a in assets], kw.get('screensaver')), (['p'], True))

    def test_falls_back_to_random(self):
        shown = self.run_screensaver(0, {'random': [{'id': 'r', 'image': True}]})
        (assets, kw), = shown
        self.assertEqual([a['id'] for a in assets], ['r'])
        self.assertIsNotNone(kw.get('more'))

    def test_smart_search(self):
        shown = self.run_screensaver(3, {'search': [{'id': 's', 'image': True}]}, query='beach at sunset')
        (assets, kw), = shown
        self.assertEqual(([a['id'] for a in assets], self.asked[0].get('query')), (['s'], 'beach at sunset'))
        self.assertIsNone(kw.get('more'))

    def test_blank_search_falls_back_to_random(self):
        shown = self.run_screensaver(3, {'search': [{'id': 's', 'image': True}], 'random': [{'id': 'r', 'image': True}]})
        (assets, _), = shown
        self.assertEqual([a['id'] for a in assets], ['r'])

    def test_viewer_as_screensaver(self):
        window = make_viewer([])
        window.screensaver = True
        window.getControl = lambda i: Recorder()
        with mock.patch.object(kodi, 'setting_bool', lambda k: False):
            window.setup()
        self.assertTrue(window.kenburns)                    # always on in the screensaver
        window.handle(2)                                    # any key wakes it
        self.assertTrue(window.closed)
        window.closed = False
        window.monitor.onScreensaverDeactivated()
        self.assertTrue(window.closed)

    def test_screensaver_text_wanders(self):
        assets = api.ImmichClient().search_page(size=3)[0]
        for screensaver in (False, True):
            window = make_viewer(assets)
            window.screensaver = screensaver
            controls = {}
            window.getControl = lambda i: controls.setdefault(i, Recorder())
            window.info, window.more = 1, None
            counters = []
            for i in range(len(assets) * 4):
                window.index = i % len(assets)
                window.caption(assets[window.index])
                counters.append(window.getProperty('immich.position'))
            spots = {g: [c[1:] for c in controls.get(g, Recorder()).calls if c[0] == 'setPosition'] for g in (310, 320)}
            if not screensaver:
                self.assertEqual((spots, counters[0]), ({310: [], 320: []}, '1 / 3'))
                continue
            self.assertEqual(set(counters), {''})                   # no counter
            for found in spots.values():
                self.assertEqual(len(found), len(counters))
                self.assertGreater(len(set(found)), 1)
                self.assertTrue(all(abs(x) <= viewer.DRIFT[0] and abs(y) <= viewer.DRIFT[1] for x, y in found))

    def test_screensaver_text_changes_corners(self):
        assets = api.ImmichClient().search_page(size=3)[0]
        window = make_viewer(assets)
        window.screensaver, window.info, window.more = True, 1, None
        window.getControl = lambda i: Recorder()
        now, corners = [viewer.CORNER_TIME * 1000 + 1], []
        with mock.patch.object(viewer.time, 'time', lambda: now[0]):
            for n in range(24):
                window.caption(assets[n % len(assets)])
                corners.append((window.getProperty('immich.edge'), window.getProperty('immich.side')))
                now[0] += viewer.CORNER_TIME / 6
        self.assertEqual(corners, [viewer.CORNERS[n // 6 % 4] for n in range(24)])   # 6 photos to a corner, in turn

    def test_skin_lets_the_text_wander(self):
        import xml.etree.ElementTree as ET
        root = ET.parse(os.path.join(ROOT, 'resources', 'skins', 'default', '1080i', 'script-immich-viewer.xml'))
        groups = {c.get('id'): ' '.join(l.text or '' for l in c.iter('label')) for c in root.iter('control') if c.get('id') in ('310', '320')}
        self.assertIn('immich.date', groups.get('310', ''))
        self.assertIn('System.Time', groups.get('320', ''))
        top, right = 'Window.Property(immich.edge),top', 'Window.Property(immich.side),right'
        for gid in ('310', '320'):                          # moves to the other edge
            group = next(c for c in root.iter('control') if c.get('id') == gid)
            self.assertTrue(any(top in (a.get('condition') or '') for a in group.findall('animation')))
        for label in ('immich.date', 'System.Time'):        # one copy for each side
            sides = [c.findtext('visible') for c in root.iter('control')
                     if label in (c.findtext('label') or '') and c.findtext('textcolor') == 'DDFFFFFF']
            self.assertTrue(all(right in v for v in sides))
            self.assertEqual(sorted('!String.IsEqual({})'.format(right) in v for v in sides), [False, True])

    def test_skin_outlines_the_text(self):
        import xml.etree.ElementTree as ET
        root = ET.parse(os.path.join(ROOT, 'resources', 'skins', 'default', '1080i', 'script-immich-viewer.xml'))
        groups = [root.find('controls')] + [c for c in root.iter('control') if c.get('type') == 'group']
        outlined = []
        for group in groups:
            labels = [c for c in group if c.get('type') == 'label']
            texts = [c for c in labels if c.findtext('textcolor') == 'DDFFFFFF']
            outlined += [c.findtext('label') for c in texts]
            for text in texts:
                copies = labels[labels.index(text) - 28:labels.index(text)]    # halo and dark ring, drawn first
                self.assertEqual({c.findtext('textcolor') for c in copies}, {'40000000', 'DD000000'})
                for c in copies:
                    for tag in ('label', 'visible', 'font', 'align', 'width', 'height'):
                        self.assertEqual(c.findtext(tag), text.findtext(tag))
                    self.assertEqual([a.attrib for a in c.findall('animation')], [a.attrib for a in text.findall('animation')])
                    for tag in ('left', 'right', 'top'):
                        if text.find(tag) is not None:
                            self.assertLessEqual(abs(int(c.findtext(tag)) - int(text.findtext(tag))), 4)
                self.assertIsNone(text.find('shadowcolor'))
        for prop in ('immich.date', 'immich.place', 'immich.position', 'System.Time', 'immich.status'):
            self.assertTrue(any(prop in label for label in outlined), prop)

    def test_viewer_window_is_freed(self):
        import gc
        import weakref
        refs = []

        def run(window):
            window.getControl = lambda i: Recorder()
            with mock.patch.object(kodi, 'setting_bool', lambda k: False):
                window.setup()
            refs.append(weakref.ref(window))
        gc.disable()                                        # Kodi checks for leftovers before any collection
        try:
            with mock.patch.object(viewer.Viewer, 'run', run):
                for screensaver in (False, True):
                    viewer.play(api.ImmichClient(), [{'id': 'p', 'image': True}], screensaver=screensaver)
        finally:
            gc.enable()
        self.assertEqual([r() for r in refs], [None, None])

    def test_screensaver_leaves_the_player_alone(self):
        xbmc.BUILTINS.clear()
        assets = api.ImmichClient().search_page(size=2)[0]
        window = make_viewer(assets)
        window.screensaver = True
        window.actions = [10]
        with mock.patch.object(xbmc, 'Player', Video):
            window.run()
        self.assertNotIn(viewer.STOP, xbmc.BUILTINS)

    def test_settings_offer_the_sources(self):
        import xml.etree.ElementTree as ET
        setting = next(s for s in ET.parse(os.path.join(ROOT, 'resources', 'settings.xml')).iter('setting')
                       if s.get('id') == 'screensaver_source')
        labels = [o.get('label') for o in setting.iter('option')]
        with open(os.path.join(ROOT, 'resources', 'language', 'resource.language.en_gb', 'strings.po')) as f:
            po = f.read()
        query = next(s for s in ET.parse(os.path.join(ROOT, 'resources', 'settings.xml')).iter('setting')
                     if s.get('id') == 'screensaver_query')
        self.assertEqual(len(labels), 4)
        self.assertTrue(all(f'msgctxt "#{n}"' in po for n in labels + [setting.get('label'), query.get('label')]))


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

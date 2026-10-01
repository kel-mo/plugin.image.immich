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



class KenBurns(NoProxy):
    def viewer(self, kenburns=True, playing=True, height='1080', zoom=110):
        assets = api.ImmichClient().search_page(size=2)[0]
        assets[0]['ratio'] = 16 / 9
        window = make_viewer(assets)
        self.window = window
        window.playing = playing
        controls = {}
        with mock.patch.object(window, 'getControl', lambda i: controls.setdefault(i, Recorder())), \
                mock.patch.object(xbmc, 'getInfoLabel', lambda s: height if s == 'System.ScreenHeight' else ''), \
                mock.patch.object(kodi, 'setting_bool', lambda k: kenburns if k == 'kenburns' else False), \
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

    def test_motion_rises_fast_and_settles_slowly(self):
        main = self.viewer()
        legs = [f for f in main.last() if f['effect'] == 'zoom']
        ms = self.window.kb['time']
        self.assertEqual([(f.get('tween'), f.get('easing')) for f in legs], [('sine', 'in'), ('sine', 'out')])
        self.assertEqual((int(legs[0]['time']), int(legs[1]['delay']), int(legs[1]['time'])),
                         (round(ms * 0.3), round(ms * 0.3), ms - round(ms * 0.3)))
        z0, z1 = float(legs[0]['start']), float(legs[0]['end']) * float(legs[1]['end']) / 100
        self.assertAlmostEqual((float(legs[0]['end']) - z0) / (z1 - z0), 0.3, delta=0.01)

    def test_delay_follows_skin_speed(self):
        with mock.patch.object(viewer, 'skin_slowdown', lambda: 0.5):
            spec = viewer.anim('zoom', start=100, end=110, time=1000, delay=300)[1]
        f = dict(p.split('=', 1) for p in spec.split() if '=' in p)
        self.assertEqual((f['time'], f['delay']), ('2000', '600'))

    def test_resume_settles(self):
        main = self.viewer()
        kb = self.window.kb
        kb['t0'] -= kb['time'] / 4000.0                     # a quarter through
        self.window.freeze()
        left = kb['time']
        self.window.thaw()
        legs = [f for f in main.last() if f['effect'] == 'zoom']
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

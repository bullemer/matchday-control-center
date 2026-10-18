"""Render Score67's existing OBS page to transparent frames for the output encoder."""
import os
import re
import struct
import tempfile
import threading
import time
import zlib
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

def _default_frame_path():
    shm = Path("/dev/shm")
    if shm.is_dir() and os.access(shm, os.W_OK):
        return shm / "mcc-score67-overlay.png"
    return Path(__file__).resolve().parent / "data" / "score67-overlay.png"


FRAME_PATH = _default_frame_path()
WIDTH, HEIGHT = 1920, 1080


def normalize_url(value):
    if not isinstance(value, str) or len(value) > 2048:
        raise ValueError('Paste a Score67 OBS overlay link (up to 2048 characters)')
    value = value.strip()
    if not value:
        return ''
    url = urlsplit(value)
    if (url.scheme != 'https' or url.hostname not in ('score67.de', 'www.score67.de')
            or url.username or url.password or url.port not in (None, 443)
            or url.fragment or any(ord(c) < 32 for c in value)):
        raise ValueError('Use an HTTPS OBS overlay link from score67.de')
    match = re.fullmatch(r'/s/([A-Za-z0-9_-]+)(?:/overlay)?/?', url.path)
    if not match:
        raise ValueError('Use the Score67 “Copy OBS overlay link” link: /s/your-board/overlay')
    query = urlencode([(k, v) for k, v in parse_qsl(url.query, keep_blank_values=True) if k != 'preview'])
    return urlunsplit(('https', url.netloc, f'/s/{match[1]}/overlay', query, ''))


def transparent_png():
    def chunk(kind, data):
        return struct.pack('!I', len(data)) + kind + data + struct.pack('!I', zlib.crc32(kind + data))
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('!2I5B', WIDTH, HEIGHT, 8, 6, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(bytes((WIDTH * 4 + 1) * HEIGHT))) + chunk(b'IEND', b''))


BLANK = transparent_png()


def write_frame(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=path.parent, suffix='.png')
    try:
        with os.fdopen(fd, 'wb') as output:
            output.write(data)
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def ensure_frame(path=FRAME_PATH):
    if not Path(path).exists():
        write_frame(path, BLANK)


class BrowserOverlay:
    def __init__(self, path=FRAME_PATH):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._url = ''
        self._generation = 0
        self._state = {'state': 'off', 'message': '', 'updated_at': 0.0}
        self._thread = None

    def configure(self, source, visible, url):
        desired = url if source == 'score67' and visible else ''
        with self._lock:
            if desired == self._url:
                return
            self._url = desired
            self._generation += 1
            # Clear immediately; an old in-flight screenshot may never restore this frame.
            write_frame(self.path, BLANK)
            self._state = {'state': 'loading' if desired else 'off', 'message': '', 'updated_at': time.time()}
            self._wake.set()

    def status(self):
        with self._lock:
            return dict(self._state)

    def start(self):
        write_frame(self.path, BLANK)
        self._thread = threading.Thread(target=self._run, daemon=True, name='score67-renderer')
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=12)
        write_frame(self.path, BLANK)

    def _publish(self, generation, frame, state, message=''):
        with self._lock:
            if generation != self._generation or self._stop.is_set():
                return False
            write_frame(self.path, frame)
            self._state = {'state': state, 'message': message, 'updated_at': time.time()}
            return True

    def _run(self):
        runtime = browser = page = None
        current = -1
        while not self._stop.is_set():
            self._wake.clear()
            with self._lock:
                url, generation = self._url, self._generation
            try:
                if generation != current:
                    if browser:
                        browser.close()
                    browser = page = None
                    current = generation
                if not url:
                    self._wake.wait(2)
                    continue
                if not page:
                    from playwright.sync_api import sync_playwright
                    if runtime is None:
                        runtime = sync_playwright().start()
                    browser = runtime.chromium.launch(headless=True, timeout=10000)
                    page = browser.new_page(viewport={'width': WIDTH, 'height': HEIGHT}, device_scale_factor=1)
                    page.set_default_timeout(3000)
                    health = {'received': 0, 'failed': False, 'script_error': False}
                    def response_received(response, health=health):
                        if re.fullmatch(r'/api/s/[^/]+/state', urlsplit(response.url).path):
                            health['failed'] = response.status != 200
                            if not health['failed']:
                                health['received'] = time.monotonic()
                    page.on('response', response_received)
                    page.on('pageerror', lambda error, health=health: health.update(script_error=True))
                    loaded_at = time.monotonic()
                    response = page.goto(url, wait_until='domcontentloaded', timeout=10000)
                    if not response or response.status != 200 or not page.locator('body.overlay #scoreboard').count():
                        raise ValueError('Overlay unavailable')
                frame = page.screenshot(omit_background=True, timeout=3000)
                if health['failed'] or health['script_error']:
                    raise ValueError('Score67 state unavailable')
                age = time.monotonic() - (health['received'] or loaded_at)
                if not health['received']:
                    if age > 10:
                        raise ValueError('Score67 state unavailable')
                    self._publish(generation, BLANK, 'loading')
                elif age > 45:
                    self._publish(generation, frame, 'stale', 'Score67 connection interrupted — showing last received scores. Reconnecting automatically.')
                else:
                    self._publish(generation, frame, 'ready')
                self._wake.wait(.5)
            except Exception:
                # Browser errors can contain the private access key: never expose their text.
                self._publish(generation, BLANK, 'error',
                              'Score67 overlay unavailable. Check the OBS link, internet connection and Chromium installation. Retrying automatically.')
                if browser:
                    try:
                        browser.close()
                    except Exception:
                        pass
                browser = page = None
                if runtime:
                    try:
                        runtime.stop()
                    except Exception:
                        pass
                    runtime = None
                self._wake.wait(5)
        if runtime:
            try:
                runtime.stop()
            except Exception:
                pass

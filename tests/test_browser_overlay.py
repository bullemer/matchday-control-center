import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from browser_overlay import BrowserOverlay, BLANK, normalize_url


class OverlayTests(unittest.TestCase):
    def test_private_key_and_options_preserved_preview_removed(self):
        self.assertEqual(normalize_url(' https://score67.de/s/game?k=a%2Bb&pos=tr&view=small&delay=500&preview=1 '),
                         'https://score67.de/s/game/overlay?k=a%2Bb&pos=tr&view=small&delay=500')

    def test_invalid_urls(self):
        for url in ['http://score67.de/s/game/overlay', 'https://evil.test/s/game/overlay',
                    'https://score67.de.evil.test/s/game/overlay', 'https://user:pass@score67.de/s/a',
                    'https://score67.de:5000/s/a', 'https://score67.de/control', 'https://score67.de/s/a#key']:
            with self.subTest(url=url), self.assertRaises(ValueError):
                normalize_url(url)

    def test_hiding_rejects_in_flight_capture(self):
        with tempfile.TemporaryDirectory() as tmp:
            overlay = BrowserOverlay(Path(tmp)/'frame.png')
            overlay.configure('score67', True, 'https://score67.de/s/a/overlay')
            generation = overlay._generation
            self.assertTrue(overlay._publish(generation, b'old frame', 'ready'))
            overlay.configure('score67', False, 'https://score67.de/s/a/overlay')
            self.assertFalse(overlay._publish(generation, b'late frame', 'ready'))
            self.assertEqual(overlay.path.read_bytes(), BLANK)
            self.assertEqual(overlay.status()['state'], 'off')

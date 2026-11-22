import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import media_engine as m


class ProgramRestoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'program.json'
        for name, value in (('PROGRAM_SETTING', None), ('_saved_program', None), ('_active_camera', None), ('_last_camera', None)):
            p = patch.object(m, name, value); p.start(); self.addCleanup(p.stop)

    def test_off_until_the_app_switches_it_on(self):
        m._save_program('cam2')                       # tests and smoke runs: nothing written
        self.assertFalse(self.path.exists())

    def test_saved_camera_is_selected_again_after_a_restart(self):
        self.assertIsNone(m.load_program_setting(self.path))   # first start: nothing saved
        m._save_program('cam3')
        self.assertEqual(json.loads(self.path.read_text())['camera'], 'cam3')
        m._active_camera, m._saved_program = None, None        # "restart"
        self.assertEqual(m.load_program_setting(self.path), 'cam3')
        self.assertEqual((m._active_camera, m._last_camera), ('cam3', 'cam3'))
        # The supervisor then builds the program as soon as cam3 publishes.
        with patch.object(m, '_relay_proc', None), patch.object(m, '_replay', None), \
                patch.object(m, '_camera_status', {'cam3': True}), patch.object(m, '_relay_retry_at', 0.0), \
                patch.object(m, 'switch_camera', return_value=(True, 'ok')) as cut:
            m._supervise_relay(100.0)
        cut.assert_called_once_with('cam3', automatic=True)

    def test_broken_or_unknown_saved_camera_is_ignored(self):
        for text in ('not json', '{"camera": "cam9"}', '[]'):
            self.path.write_text(text)
            m._active_camera = None
            self.assertIsNone(m.load_program_setting(self.path))
            self.assertIsNone(m._active_camera)

    def test_a_camera_already_chosen_wins(self):
        self.path.write_text('{"camera": "cam2"}')
        m._active_camera = 'cam1'   # e.g. the live-broadcast resume picked one
        m.load_program_setting(self.path)
        self.assertEqual(m._active_camera, 'cam1')


if __name__ == '__main__':
    unittest.main()

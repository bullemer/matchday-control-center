"""Regression checks for STOP races, replay handovers, fallback audio and clips."""
import shutil
import subprocess
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from test_control import EngineStateCase, m


class HandoverRegressions(EngineStateCase):
    def setUp(self):
        super().setUp()
        for name, value in dict(_replay=None, _relay_slot=0, _relay_zmq=None,
                                _muted=False, _follow=False, _relay_sources={},
                                _mix={c: {'on': c == 'cam1', 'gain': 0} for c in m.CAMERA_IDS}).items():
            self.stack.enter_context(patch.object(m, name, value))
        self.stack.enter_context(patch.object(m, 'get_mediamtx_running', return_value=True))
        self.stack.enter_context(patch.object(m, '_notify_replay'))
        self.stack.enter_context(patch.object(m, '_relay_command', return_value=([], {}, set(), '', None, '')))
        self.running(record=False)
        m._active_camera = 'cam1'

    def test_stop_between_readiness_and_install_discards_camera_and_replay(self):
        for replay in (False, True):
            with self.subTest(replay=replay):
                candidate = MagicMock()
                generation = m._generation
                def stop_after_ready(*args):
                    m._stop_all()
                    return candidate, None
                with patch.object(m, '_take_over', side_effect=stop_after_ready), patch.object(m.threading, 'Thread') as watcher:
                    result = (m._install_replay(generation, 'cam1', {'length': 10, 'speed': 1, 'hold': True})
                              if replay else m._replace_relay('cam2', generation))
                self.assertEqual(result, (False, m.SUPERSEDED))
                self.assertIsNone(m._relay_proc)
                self.assertIsNone(m._active_camera)
                self.assertIsNone(m._replay)
                self.kill.assert_any_call(candidate)
                watcher.assert_not_called()

    def test_unmute_restores_missing_commentary_fallback_but_all_off_stays_silent(self):
        m._active_camera = 'cam2'
        m._relay_proc.poll.return_value = None
        m._relay_channels = {'cam2': 'm0'}
        m._relay_missing = {'cam1'}
        with patch.object(m, '_send_volume', return_value=True) as volume:
            self.assertTrue(m.set_mute(True)[0])
            self.assertEqual(volume.call_args.args, ('m0', 0.0))
            self.assertTrue(m.set_mute(False)[0])
            self.assertEqual(volume.call_args.args, ('m0', 1.0))
            self.assertEqual(m._relay_muted, set())
            self.assertTrue(m.set_mix('cam1', on=False)[0])
            self.assertEqual(volume.call_args.args, ('m0', 0.0))

    def test_failed_cut_keeps_held_replay_until_stop(self):
        proc = m._relay_proc
        proc.poll.return_value = None
        m._replay = {'generation': 0, 'camera': 'cam1', 'hold': True}
        observed = threading.Event()
        release = threading.Event()
        def pause(_):
            observed.set()
            release.wait(2)
        with patch.object(m, '_relay_command', side_effect=ValueError('No video')):
            self.assertFalse(m.switch_camera('cam3')[0])
        with patch.object(m.time, 'sleep', side_effect=pause):
            watcher = threading.Thread(target=m._watch_replay, args=(proc, 0, 'cam1', 10, True))
            watcher.start()
            try:
                self.assertTrue(observed.wait(1), 'Replay watcher exited after failed cut')
                self.assertIs(m._relay_proc, proc)
                self.assertIsNotNone(m._replay)
                self.kill.assert_not_called()
                m._stop_all()
            finally:
                release.set()
                watcher.join(3)
            self.assertFalse(watcher.is_alive())

    def test_expired_replay_returns_after_failed_request(self):
        proc = m._relay_proc
        proc.poll.return_value = None
        m._replay = {'generation': 0, 'camera': 'cam1'}
        m._generation = 1  # a newer request failed; replay still owns the picture
        with patch.object(m, 'switch_camera', side_effect=[(False, m.SUPERSEDED), (True, 'ok')]) as cut:
            m._watch_replay(proc, 0, 'cam1', 0)
        self.assertEqual(cut.call_count, 2)
        cut.assert_called_with('cam1', automatic=True, expected_stop=0)


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg required')
class ClipBoundaryRegressions(EngineStateCase):
    def test_clip_contains_footage_on_both_sides_of_restart(self):
        r = m.recordings
        sid = r.create_session('480p', 'Boundary')
        for color in ('red', 'blue'):
            path = r.new_part(sid)
            subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
                            f'color={color}:s=160x90:r=10', '-f', 'lavfi', '-i', 'sine=sample_rate=48000',
                            '-t', '10', '-c:a', 'aac', '-c:v', 'libx264',
                            '-preset', 'ultrafast', '-g', '10', '-f', 'mpegts', path], check=True, timeout=15)
            r.close_part(sid)
        session = r._load(sid)
        session['parts'][-1]['duration'] = None  # the newest part is still recording
        r._save(session)
        for offset in (8, 12.5):
            with self.subTest(marker=offset):
                session = r._load(sid)
                session['markers'] = [{'id': 'm1', 'offset': offset, 'label': 'Goal', 'clip': None}]
                r._save(session)
                name = r.export_clip(sid, 'm1', pre=8, post=5)
                path = r._dir(sid) / name
                self.assertAlmostEqual(r._probe_duration(path), 13, delta=1)
                pixels = subprocess.run(['ffmpeg', '-v', 'error', '-i', str(path), '-vf',
                                         'scale=1:1', '-pix_fmt', 'rgb24', '-f', 'rawvideo', '-'],
                                        capture_output=True, check=True, timeout=15).stdout
                self.assertGreater(pixels[0], 150)  # red footage before the restart
                self.assertGreater(pixels[-1], 150)  # blue footage after the restart
                self.assertEqual(list(r._dir(sid).glob('*.concat')), [])
                self.assertEqual(list(path.parent.glob('*.part.mp4')), [])

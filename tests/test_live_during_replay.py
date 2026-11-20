"""While a replay is on air the recording holds the replay: the live camera is recorded
next to it, so the live game is not lost (20 Nov 2026: four replays cost 96 s of play)."""
from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch

from test_control import EngineStateCase, m

r = m.recordings


class LiveCaptureEngineTests(EngineStateCase):
    def setUp(self):
        super().setUp()
        for key, value in dict(_replay=None, _live_capture=None, _relay_zmq=None, _muted=False,
                               _camera_status={'cam1': True, 'cam2': True, 'cam3': False}).items():
            self.stack.enter_context(patch.object(m, key, value))
        self.stack.enter_context(patch.object(m, 'get_mediamtx_running', return_value=True))
        self.running()
        m._active_camera = 'cam2'
        r.new_part(m._recording); r.part_started(m._recording, 1000.0)
        Path(self.tmp, m._recording, 'part-001.ts').write_bytes(b'x')
        self.install = self.stack.enter_context(patch.object(m, '_install_replay', return_value=(True, 'Replay on air')))
        self.stack.enter_context(patch.object(m.time, 'time', return_value=1060.0))
        self.proc = MagicMock(name='live'); self.proc.poll.return_value = None
        self.process = self.stack.enter_context(patch.object(m, 'Process', return_value=self.proc))
        self.close = self.stack.enter_context(patch.object(r, 'close_live_window'))

    def test_replay_records_the_live_camera_it_covers(self):
        self.assertTrue(m.play_replay(10)[0])
        cmd = self.process.call_args.args[0]
        self.assertIn(m.RTSP_BASE + '/cam2', cmd)
        self.assertEqual(cmd[cmd.index('-c') + 1], 'copy')  # no encoding: no CPU on match day
        self.assertTrue(cmd[-1].endswith('/live/live-01-cam2.ts'))
        self.assertEqual(m._live_capture['camera'], 'cam2')
        # A second replay right after keeps the same live recording running.
        m._replay = {'camera': 'cam2', 'generation': 1}
        self.assertTrue(m.play_replay(10)[0])
        self.assertEqual(self.process.call_count, 1)

    def test_live_recording_ends_a_little_after_the_replay(self):
        m.play_replay(10)
        m._replay = {'camera': 'cam2'}
        m._supervise_live_capture(100.0)
        self.assertTrue(m._live_capture['aired'])
        m._replay = None  # back to live
        m._supervise_live_capture(101.0)
        m._supervise_live_capture(101.0 + m.LIVE_TAIL - 0.5)
        self.assertIsNotNone(m._live_capture)  # overlaps the return to live
        with patch.object(m.threading, 'Thread') as thread:
            m._supervise_live_capture(101.0 + m.LIVE_TAIL)
            thread.call_args.kwargs['target']()
        self.assertIsNone(m._live_capture)
        self.kill.assert_any_call(self.proc)
        session, path, aired, _ = self.close.call_args.args
        self.assertEqual((session, aired), (m._recording, True))

    def test_offline_camera_or_no_camera_records_nothing(self):
        m._active_camera = 'cam3'
        m.play_replay(10)
        self.process.assert_not_called()
        self.assertIsNone(m._live_capture)

    def test_stop_recording_closes_the_live_recording_before_joining(self):
        m.play_replay(10)
        self.running(key='key', record=False)  # keep streaming, stop only the recording
        m._want_record, m._recorder_proc = True, MagicMock()
        with patch.object(r, 'close_part'):
            m.stop_recording()
        self.close.assert_called_once()
        self.assertIsNone(m._live_capture)

    def test_a_crashed_live_recording_restarts_while_the_replay_runs(self):
        m.play_replay(10)
        m._replay = {'camera': 'cam2'}
        m._supervise_live_capture(100.0)
        self.proc.poll.return_value = 1
        fresh = MagicMock(); fresh.poll.return_value = None
        self.process.return_value = fresh
        with patch.object(m.threading, 'Thread'):
            m._supervise_live_capture(100.5)
        self.assertIs(m._live_capture['proc'], fresh)
        self.assertTrue(m._live_capture['aired'])


class LiveWindowStoreTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(patch.object(r, 'RECORDINGS_DIR', Path(self.tmp)))
        self.sid = r.create_session('720p', 'A vs B')
        r.new_part(self.sid); r.part_started(self.sid, 1000.0)
        Path(self.tmp, self.sid, 'part-001.ts').write_bytes(b'x')
        self.path = r.new_live_window(self.sid, 'cam2')
        Path(self.path).write_bytes(b'video')

    def test_window_is_placed_on_the_recording_timeline(self):
        with patch.object(r, '_probe_duration', return_value=20.0), patch.object(r, '_remux_live', return_value='live/live-01-cam2.mp4'):
            Path(self.tmp, self.sid, 'live/live-01-cam2.mp4').write_bytes(b'mp4')
            window = r.close_live_window(self.sid, self.path, aired=True, wall_end=1100.0)
        self.assertEqual((window['offset'], window['duration']), (80.0, 20.0))
        listed = r.get(self.sid)['live']
        self.assertEqual([(w['camera'], w['time'], w['file']) for w in listed], [('cam2', '1:20', 'live/live-01-cam2.mp4')])

    def test_window_of_a_replay_that_never_aired_is_dropped(self):
        r.close_live_window(self.sid, self.path, aired=False, wall_end=1100.0)
        self.assertFalse(Path(self.path).exists())
        self.assertEqual(r.get(self.sid)['live'], [])

    def test_highlight_inside_a_replay_is_cut_from_the_live_camera(self):
        with patch.object(r, '_probe_duration', return_value=20.0), patch.object(r, '_remux_live', return_value=None):
            r.close_live_window(self.sid, self.path, aired=True, wall_end=1100.0)
        goal = r.add_marker(self.sid, 'Goal', wall=1090.0)      # offset 90: inside 80..100
        early = r.add_marker(self.sid, 'Chance', wall=1030.0)   # offset 30: live on the program
        runs = []
        def run(cmd, timeout=120):
            runs.append(cmd)
            Path(cmd[-1]).write_bytes(b'clip')
        with patch.object(r, '_run', side_effect=run), patch.object(r, '_has_audio', return_value=True):
            r.export_clip(self.sid, goal['id'])
            r.export_clip(self.sid, early['id'])
        live, program = runs
        self.assertEqual(live[live.index('-i') + 1], self.path)
        self.assertEqual(live[live.index('-ss') + 1], '0.00')   # 10 s in, 20 s before: from the start
        self.assertEqual(live[live.index('-t') + 1], '20.00')   # until the end of the window
        self.assertIn('libx264', live)                           # the recording's size for the reel
        self.assertNotIn(self.path, program)
        markers = {mk['id']: mk for mk in r._load(self.sid)['markers']}
        self.assertEqual((markers[goal['id']]['clip_live'], markers[early['id']]['clip_live']), (True, False))

    def test_finalize_finishes_windows_left_by_a_crash(self):
        with patch.object(r, '_probe_duration', return_value=15.0), \
                patch.object(r, '_remux_live', return_value='live/live-01-cam2.mp4'):
            r._finish_live_windows(self.sid)
        window = r._load(self.sid)['live'][0]
        self.assertEqual((window['duration'], window['mp4']), (15.0, 'live/live-01-cam2.mp4'))
        self.assertIsNotNone(window['offset'])


if __name__ == '__main__':
    unittest.main()

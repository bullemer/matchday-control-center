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


class StopRemembersCameraTests(EngineStateCase):
    def test_go_live_after_stop_starts_on_the_camera_that_was_on_air(self):
        self.stack.enter_context(patch.object(m, '_last_camera', None))
        self.stack.enter_context(patch.object(m, '_replay', None))
        self.stack.enter_context(patch.object(m, '_layout', {'mode': 'single', 'main': 'cam1', 'secondary': 'cam2', 'position': 'top-right'}))
        self.running()
        m._active_camera = 'cam3'
        m._stop_all()
        self.assertIsNone(m._active_camera)
        board = Path(self.tmp, 'scoreboard.txt'); board.write_text('x')
        with patch.object(m, 'get_mediamtx_running', return_value=True), patch.object(m, 'SCOREBOARD_PATH', str(board)), \
                patch.object(m, '_paths', return_value={'cam1': True, 'cam2': True, 'cam3': True}), \
                patch.object(m, 'switch_camera', return_value=(True, 'ok')) as cut, patch.object(m, '_launch_encoder'):
            self.assertTrue(m.start_recording()[0])
        cut.assert_called_once_with('cam3', expected_stop=m._stop_generation)


class ResumeTests(EngineStateCase):
    """The app went down while live: the next start continues the broadcast."""
    def setUp(self):
        super().setUp()
        for key, value in dict(_replay=None, _last_camera=None,
                               _layout={'mode': 'single', 'main': 'cam1', 'secondary': 'cam2', 'position': 'top-right'}).items():
            self.stack.enter_context(patch.object(m, key, value))

    def test_snapshot_and_resume_continue_the_same_session(self):
        self.running(key='yt-key')
        m._active_camera, m._layout = 'cam2', {'mode': 'pip', 'main': 'cam2', 'secondary': 'cam3', 'position': 'top-left'}
        r.new_part(m._recording); r.part_started(m._recording, 1000.0)
        Path(self.tmp, m._recording, 'part-001.ts').write_bytes(b'x')
        state = m.resume_state()
        self.assertEqual(state['targets'], {'youtube': {'key': 'yt-key', 'server': m.YOUTUBE_RTMP_BASE}})
        self.assertEqual((state['camera'], state['record'], state['layout']['mode']), ('cam2', True, 'pip'))
        session = state['session']
        m._stop_all(finalize=False)  # the crash
        m._layout = {'mode': 'single', 'main': 'cam1', 'secondary': 'cam2', 'position': 'top-right'}
        self.assertIsNone(m.resume_state())
        r.recover()
        self.assertEqual(r._load(session)['state'], 'unfinished')
        with patch.object(m, '_start_output', return_value=(True, 'Going live')) as start, \
                patch.object(r, '_probe_duration', return_value=50.0):
            self.assertTrue(m.resume(state, 'A vs B')[0])
            targets, profile, token, record, title, again = start.call_args.args
            self.assertEqual((list(targets), record, again), (['youtube'], True, session))
            self.assertEqual((m._last_camera, m._layout['mode'], m._layout['secondary']), ('cam2', 'pip', 'cam3'))
            # The recorder continues the same session with a new part.
            with m._lock:
                m._begin_recording('A vs B', session)
            self.assertEqual(m._recording, session)
            self.assertEqual(r._load(session)['state'], 'recording')
            self.assertEqual(r.new_part(session).rsplit('/', 1)[1], 'part-002.ts')

    def test_saved_session_cannot_be_reopened(self):
        sid = r.create_session('720p', 'A vs B')
        s = r._load(sid); s.update(state='done', file='x.mp4'); r._save(s)
        self.assertFalse(r.reopen(sid))
        self.assertFalse(r.reopen('20990101-000000'))
        with m._lock:
            m._profile = '720p'
            m._begin_recording('A vs B', sid)  # a fresh session instead
        self.assertNotEqual(m._recording, sid)

    def test_shutdown_saves_the_recording_within_a_time_limit(self):
        self.running()
        session = m._recording
        with patch.object(r, 'finalize') as finalize, patch.object(m, '_thread', None), \
                patch.object(m, '_encoder_thread', None), patch.object(m, '_mediamtx_proc', None), \
                patch.object(m, '_meters', {}), patch.object(m._stop, 'set'):
            m.shutdown_all()
        finalize.assert_called_once_with(session, timeout=m.SHUTDOWN_SAVE_SECONDS)


class AppResumeTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        import app as a
        self.a = a
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.object(a, 'RUNTIME_DIR', self.tmp))
        self.enterContext(patch.object(a, '_resume_pending', None))

    def test_live_state_is_kept_while_live_and_removed_after_stop(self):
        a, path = self.a, self.tmp / 'live-state.json'
        with patch.object(a, 'resume_state', return_value={'targets': {'youtube': {'key': 'k', 'server': 's'}}, 'record': True, 'session': 'x'}):
            a._save_live_state()
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)  # holds stream keys
        self.assertEqual(a._load_resume()['session'], 'x')
        with patch.object(a, 'resume_state', return_value=None):
            a._save_live_state()  # STOP: nothing wanted
        self.assertFalse(path.exists())

    def test_old_live_state_is_not_resumed(self):
        a, path = self.a, self.tmp / 'live-state.json'
        path.write_text('{"saved_at": %f, "record": true}' % (a.time.time() - a.RESUME_MAX - 5))
        self.assertIsNone(a._load_resume())
        self.assertFalse(path.exists())

    def test_recovery_leaves_the_resumed_session_alone(self):
        a = self.a
        a._resume_pending = {'record': True, 'session': 'keep'}
        sessions = [{'id': 'keep', 'state': 'unfinished'}, {'id': 'old', 'state': 'unfinished'}]
        with patch.object(a.recordings, 'recover'), patch.object(a.recordings, 'list_sessions', return_value=sessions), \
                patch.object(a.recordings, 'finalize') as finalize, patch.object(a, '_resume_broadcast') as resume:
            a._recover_recordings()
        finalize.assert_called_once_with('old')
        resume.assert_called_once()

    def test_resume_waits_for_a_camera_then_goes_live(self):
        a = self.a
        status = iter([{'cam1': False}, {'cam1': True}])
        with patch.object(a, 'get_mediamtx_running', return_value=True), \
                patch.object(a, 'get_camera_status', side_effect=lambda: next(status)), \
                patch.object(a._workers_stop, 'wait'), patch.object(a, 'resume', return_value=(True, 'Going live')) as go:
            a._resume_pending = {'record': True, 'session': 's'}
            a._resume_broadcast(a._resume_pending)
        go.assert_called_once()
        self.assertIsNone(a._resume_pending)


class UploadHealthTests(unittest.TestCase):
    def proc(self, samples):
        p = m.Process.__new__(m.Process)
        p._samples = __import__('collections').deque(samples, maxlen=60)
        return p

    def test_rate_and_speed_over_the_last_seconds(self):
        now = m.time.monotonic()
        # 5 s: 4.8 s of media and 2.5 MB out = 4000 kbit/s at 0.96x
        p = self.proc([(now - 20, 0, 0), (now - 5, 10_000_000, 1_000_000), (now, 14_800_000, 3_500_000)])
        self.assertEqual(p.rates(), (4000.0, 0.96))
        self.assertEqual(self.proc([(now, 1, 1)]).rates(), (None, None))
        self.assertEqual(self.proc([(now - 9, 0, 0), (now - 8, 1_000_000, 1)]).rates(), (0.0, 0.0))  # stalled

    def test_health_levels(self):
        for speed, health in ((1.0, 'good'), (0.95, 'slow'), (0.5, 'failing'), (None, None)):
            proc = MagicMock(); proc.rates.return_value = (4000.0, speed)
            self.assertEqual(m._upload_health({'proc': proc, 'state': 'live'})['health'], health)
        self.assertIsNone(m._upload_health({'proc': proc, 'state': 'starting'})['health'])


class MicDelayTests(EngineStateCase):
    def setUp(self):
        super().setUp()
        for key, value in dict(_mic_delay_ms=0, _replay=None, _muted=False, _relay_zmq=None,
                               AUDIO_SETTING=Path(self.tmp, 'audio.json'),
                               _layout={'mode': 'single', 'main': 'cam1', 'secondary': 'cam2', 'position': 'top-right'}).items():
            self.stack.enter_context(patch.object(m, key, value))

    def test_usb_microphone_is_delayed_to_match_the_picture(self):
        m._mic_delay_ms = 1500
        with patch.object(m, '_probe', return_value={'video', 'audio'}), patch.object(m, '_usb_device_available', return_value=True):
            cmd, channels, *_ = m._relay_command('cam1', {'usb:mic': 0.0, 'cam1': 0.0})
        graph = cmd[cmd.index('-filter_complex') + 1]
        usb, cam = channels['usb:mic'], channels['cam1']
        self.assertRegex(graph, r'\[1:a:0\]adelay=delays=1500:all=1,aresample')
        self.assertNotRegex(graph, r'\[0:a:0\]adelay')  # camera sound is already in step

    def test_setting_persists_and_rebuilds_a_relay_with_a_microphone(self):
        m._active_camera, m._relay_channels = 'cam1', {'usb:mic': 'm0'}
        with patch.object(m, 'get_active_camera', return_value='cam1'), \
                patch.object(m, 'switch_camera', return_value=(True, 'ok')) as cut:
            self.assertTrue(m.set_mic_delay(1200)[0])
            cut.assert_called_once_with('cam1', force=True)
            m._relay_channels = {'cam1': 'm0'}
            self.assertTrue(m.set_mic_delay(800)[0])
            self.assertEqual(cut.call_count, 1)  # no microphone on air: nothing to rebuild
        self.assertFalse(m.set_mic_delay(5000)[0])
        m._mic_delay_ms = 0
        m.load_audio_setting()
        self.assertEqual(m._mic_delay_ms, 800)

"""Four match cameras and two reporting cameras (interviews, surroundings).

Reporting cameras are heard while they are in the picture, may share the picture with a
match camera (two match cameras never), and are recorded on their own for the session."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from test_control import ApiTests, EngineStateCase, m

r = m.recordings


class CameraListTests(unittest.TestCase):
    def test_four_match_and_two_reporting_cameras_with_keys(self):
        cams = m.get_cameras()
        self.assertEqual([c['id'] for c in cams], ['cam1', 'cam2', 'cam3', 'cam4', 'cam5', 'cam6'])
        self.assertEqual([c['role'] for c in cams], ['match'] * 4 + ['report'] * 2)
        self.assertEqual([c['key'] for c in cams], ['1', '2', '3', '4', '5', '6'])
        self.assertEqual([c['short'] for c in cams][3:], ['CAM 4', 'REP 1', 'REP 2'])

    def test_names_and_positions_are_saved(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(m, 'CAMERA_SETTING', Path(tmp) / 'cameras.json'), \
                patch.object(m, '_camera_info', {}):
            self.assertTrue(m.set_camera_info('cam2', label='Behind goal', position='North end')[0])
            m._camera_info.clear()
            m.load_camera_setting()
            cam2 = next(c for c in m.get_cameras() if c['id'] == 'cam2')
            self.assertEqual((cam2['label'], cam2['position']), ('Behind goal', 'North end'))
            m.set_camera_info('cam2', label='')  # empty name: back to the default
            self.assertEqual(m._camera_info['cam2']['label'], 'Cam 2')
            self.assertFalse(m.set_camera_info('cam9', label='x')[0])

    def test_every_camera_may_publish(self):
        config = json.loads(Path(m.generate_mediamtx_config()).read_text())
        self.assertEqual(config['authInternalUsers'][0]['permissions'][0]['path'], '~^(cam1|cam2|cam3|cam4|cam5|cam6)$')
        self.assertTrue(all(config['paths'][c]['overridePublisher'] for c in m.CAMERA_IDS))


class LayoutRuleTests(unittest.TestCase):
    def layout(self, **kw):
        return {'mode': 'pip', 'main': 'cam1', 'secondary': 'cam5', 'position': 'top-right', **kw}

    def test_match_big_interview_small(self):
        self.assertEqual(m._effective_layout('cam2', self.layout())['mode'], 'pip')
        self.assertEqual(m._effective_layout('cam6', self.layout(secondary='cam1'))['mode'], 'pip')

    def test_two_match_cameras_are_never_combined(self):
        self.assertEqual(m._effective_layout('cam2', self.layout(secondary='cam1'))['mode'], 'single')
        with patch.object(m, '_replace_relay') as replace:
            ok, message = m.set_layout('pip', 'cam1', 'cam2')
        self.assertFalse(ok)
        self.assertIn('reporting camera', message)
        replace.assert_not_called()

    def test_cut_to_the_inset_camera_shows_it_full_screen(self):
        self.assertEqual(m._effective_layout('cam5', self.layout())['mode'], 'single')
        with patch.object(m, '_layout', self.layout()), patch.object(m, '_probe', return_value={'video', 'audio'}):
            cmd, *_rest, secondary, _ = m._relay_command('cam5', {'cam5': 0.0})
        self.assertIsNone(secondary)
        self.assertNotIn('overlay=', ' '.join(cmd))


class ReportingSoundTests(unittest.TestCase):
    def mix(self, **on):
        mix = {c: {'on': c in m.REPORT_CAMERAS, 'gain': 0.0} for c in m.CAMERA_IDS}
        for c, value in on.items():
            mix[c]['on'] = value
        return patch.object(m, '_mix', mix)

    def test_interview_heard_only_while_in_the_picture(self):
        single = {'mode': 'single', 'main': 'cam1', 'secondary': 'cam5', 'position': 'top-right'}
        pip = dict(single, mode='pip')
        with self.mix(), patch.object(m, '_follow', False):
            with patch.object(m, '_layout', single):
                self.assertEqual(m._effective_mix('cam1'), {})          # game on air: silent
                self.assertEqual(m._effective_mix('cam5'), {'cam5': 0.0})  # interview full screen
            with patch.object(m, '_layout', pip):
                self.assertEqual(m._effective_mix('cam1'), {'cam5': 0.0})  # interview inset

    def test_reporting_camera_can_be_kept_silent(self):
        with self.mix(cam5=False), patch.object(m, '_follow', False), \
                patch.object(m, '_layout', {'mode': 'single', 'main': 'cam5', 'secondary': 'cam5', 'position': 'top-right'}):
            self.assertEqual(m._effective_mix('cam5'), {})

    def test_presets_leave_reporting_cameras_alone(self):
        with self.mix(), patch.object(m, '_follow', False), patch.object(m, '_apply_mix', return_value=(True, 'Mixer updated')):
            self.assertTrue(m.set_audio_source('off')[0])
            self.assertTrue(m._mix['cam5']['on'] and m._mix['cam6']['on'])
            self.assertEqual(m.get_audio_source(), 'off')
            m.set_audio_source('cam1')
            self.assertEqual(m.get_audio_source(), 'cam1')

    def test_follow_never_borrows_interview_sound_for_the_game(self):
        # The Pi (cam2) has no sound: follow keeps a match camera's, never the reporter's.
        audio = {'cam1': True, 'cam2': False, 'cam5': True}
        with self.mix(), patch.object(m, '_follow', True), patch.object(m, '_follow_last', 'cam5'), \
                patch.object(m, '_camera_audio', audio), \
                patch.object(m, '_layout', {'mode': 'single', 'main': 'cam2', 'secondary': 'cam5', 'position': 'top-right'}):
            self.assertEqual(m._effective_mix('cam2'), {'cam1': 0.0})


class CutSourceTests(EngineStateCase):
    def setUp(self):
        super().setUp()
        self.stack.enter_context(patch.object(m, 'get_mediamtx_running', return_value=True))
        self.stack.enter_context(patch.object(m, '_manual_cut_at', -1e9))
        self.stack.enter_context(patch.object(m, '_cut_source', 'manual'))
        self.replace = self.stack.enter_context(patch.object(m, '_replace_relay', return_value=(True, 'ok')))

    def test_auto_director_only_picks_match_cameras(self):
        ok, message = m.switch_camera('cam5', source='auto')
        self.assertFalse(ok)
        self.assertIn('match camera', message)
        self.assertTrue(m.switch_camera('cam3', source='auto')[0])
        self.assertEqual(m.get_stream_status()['cut_source'], 'auto')

    def test_manual_cut_holds_off_the_auto_director(self):
        self.assertTrue(m.switch_camera('cam2')[0])
        self.assertFalse(m.switch_camera('cam3', source='auto')[0])
        self.assertEqual(self.replace.call_count, 1)
        self.assertFalse(m.switch_camera('cam3', source='robot')[0])


class IsoRecordingTests(EngineStateCase):
    def setUp(self):
        super().setUp()
        self.stack.enter_context(patch.object(m, '_iso', {}))
        self.stack.enter_context(patch.object(m, '_iso_retry', {}))
        self.stack.enter_context(patch.object(m, '_camera_status', {'cam1': True, 'cam5': True, 'cam6': False}))
        self.procs = []

        def process(cmd, copy=False):
            proc = MagicMock(name='iso'); proc.poll.return_value = None; proc.first_progress = None
            proc.cmd = cmd
            self.procs.append(proc)
            return proc
        self.process = self.stack.enter_context(patch.object(m, 'Process', side_effect=process))

    def test_publishing_reporting_cameras_are_recorded_while_recording(self):
        m._supervise_iso(0.0)
        self.assertEqual(self.process.call_count, 0)  # nothing without a recording
        self.running()
        m._supervise_iso(1.0)
        self.assertEqual(list(m._iso), ['cam5'])  # match cameras and offline cam6: no
        cmd = self.procs[0].cmd
        self.assertIn(m.RTSP_BASE + '/cam5', cmd)
        self.assertEqual(cmd[cmd.index('-c') + 1], 'copy')
        self.assertTrue(cmd[-1].endswith('/iso/cam5-01.ts'))
        m._supervise_iso(2.0)
        self.assertEqual(self.process.call_count, 1)
        session = r._load(m._recording)
        self.assertEqual([p['camera'] for p in session['iso']], ['cam5'])

    def test_camera_drop_ends_the_part_and_a_new_part_follows(self):
        self.running()
        m._supervise_iso(1.0)
        m._camera_status['cam5'] = False
        m._supervise_iso(2.0)
        self.assertEqual(m._iso, {})
        self.kill.assert_any_call(self.procs[0])
        m._camera_status['cam5'] = True
        m._supervise_iso(3.0)
        self.assertTrue(self.procs[1].cmd[-1].endswith('/iso/cam5-02.ts'))

    def test_dead_recorder_restarts_after_a_pause(self):
        self.running()
        m._supervise_iso(1.0)
        self.procs[0].poll.return_value = 1
        m._supervise_iso(2.0)
        self.assertEqual(self.process.call_count, 1)
        m._supervise_iso(2.0 + m.ISO_RETRY)
        self.assertEqual(self.process.call_count, 2)

    def test_first_frame_sets_the_part_start(self):
        self.running()
        m._supervise_iso(1.0)
        self.procs[0].first_progress = (1500.0, 2.0)
        m._supervise_iso(2.0)
        self.assertEqual(r._load(m._recording)['iso'][0]['start'], 1498.0)

    def test_stop_recording_ends_them_before_saving(self):
        self.running()
        m._supervise_iso(1.0)
        with patch.object(r, 'close_part'):
            m.stop_recording()
        self.assertEqual(m._iso, {})
        self.kill.assert_any_call(self.procs[0])
        m._supervise_iso(5.0)
        self.assertEqual(self.process.call_count, 1)


class IsoStoreTests(EngineStateCase):
    def test_finalize_saves_reporting_camera_parts_on_the_timeline(self):
        sid = r.create_session('720p', 'A vs B')
        r.new_part(sid); r.part_started(sid, 1000.0)
        Path(self.tmp, sid, 'part-001.ts').write_bytes(b'x')
        keep = r.new_iso_part(sid, 'cam5'); r.iso_started(sid, keep, 1030.0)
        Path(keep).write_bytes(b'interview')
        empty = r.new_iso_part(sid, 'cam5'); Path(empty).write_bytes(b'')
        info = r.get(sid)
        self.assertEqual([p['recording'] for p in info['iso']], [True, True])

        def remux(sid_, part):
            Path(self.tmp, sid_, part['file'][:-3] + '.mp4').write_bytes(b'mp4')
            Path(self.tmp, sid_, part['file']).unlink()
            return part['file'][:-3] + '.mp4'
        durations = {'part-001.ts': 120.0, 'cam5-01.ts': 45.0, 'cam5-02.ts': 0.0}
        with patch.object(r, '_run'), patch.object(r, '_remux_live', side_effect=remux), \
                patch.object(r, '_probe_duration', side_effect=lambda path: durations.get(Path(path).name, 120.0)):
            Path(self.tmp, sid, 'joined.part.mp4').write_bytes(b'joined')
            ok, _ = r.finalize(sid)
        self.assertTrue(ok)
        iso = r.get(sid)['iso']
        self.assertEqual(len(iso), 1)  # the empty part is dropped
        self.assertEqual((iso[0]['file'], iso[0]['offset'], iso[0]['duration'], iso[0]['recording']),
                         ('iso/cam5-01.mp4', 30.0, 45.0, False))
        self.assertTrue(r.file_path(sid, iso[0]['file']).is_file())

    def test_reporting_parts_count_towards_recording_room(self):
        sid = r.create_session('720p', 'A vs B')
        Path(r.new_iso_part(sid, 'cam6')).write_bytes(b'x' * 1000)
        self.assertEqual(r.parts_bytes(sid), 1000)


class CameraApiTests(unittest.TestCase):
    setUp, post = ApiTests.setUp, ApiTests.post  # its login and client, not its tests

    def test_camera_names_api(self):
        import app as a
        with patch.object(a, 'set_camera_info', return_value=(True, 'Camera saved')) as save:
            response = self.post('/api/cameras/cam5', {'label': 'Interview', 'position': 'Clubhouse'})
            self.assertEqual(response.status_code, 200)
            save.assert_called_once()
        self.assertEqual(self.post('/api/cameras/cam9', {'label': 'x'}).status_code, 404)

    def test_switch_accepts_new_cameras_and_cut_source(self):
        import app as a
        with patch.object(a, 'switch_camera', return_value=(True, 'ok')) as cut:
            self.assertEqual(self.post('/api/switch/cam6', {}).status_code, 200)
            cut.assert_called_with('cam6', source='manual')
            self.post('/api/switch/cam4', {'source': 'auto'})
            cut.assert_called_with('cam4', source='auto')
        self.assertEqual(self.post('/api/switch/cam7', {}).status_code, 400)


del ApiTests, EngineStateCase  # run the imported base tests once, in their own module

if __name__ == '__main__':
    unittest.main()

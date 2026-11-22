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
REAL_GENERATE = m.generate_mediamtx_config


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
        with tempfile.TemporaryDirectory() as tmp, patch.object(m, 'MEDIAMTX_CONFIG', tmp + '/mtx.yml'):
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


class CameraInputSettingTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / 'cameras.json'
        self.enterContext(patch.object(m, 'CAMERA_SETTING', self.path))
        self.enterContext(patch.object(m, '_camera_info', {}))
        self.running = self.enterContext(patch.object(m, 'get_mediamtx_running', return_value=False))
        self.config = self.enterContext(patch.object(m, 'generate_mediamtx_config'))

    def cam(self, cam):
        return next(c for c in m.get_cameras() if c['id'] == cam)

    def test_defaults_rtmp_and_quality_by_role(self):
        self.assertEqual((self.cam('cam1')['protocol'], self.cam('cam1')['quality']), ('rtmp', '1080p'))
        self.assertEqual(self.cam('cam5')['quality'], '720p')
        self.assertEqual(self.cam('cam5')['recommended'], {'size': '1280x720', 'fps': 30, 'kbps': 3000,
                                                          'keyframe_s': 1, 'label': '720p · 3 Mbit/s'})

    def test_srt_settings_are_validated_and_saved_privately(self):
        self.assertTrue(m.set_camera_info('cam5', protocol='srt', latency_ms=500, quality='720p-low')[0])
        self.assertFalse(m.set_camera_info('cam5', protocol='udp')[0])
        self.assertFalse(m.set_camera_info('cam5', latency_ms=20)[0])
        self.assertFalse(m.set_camera_info('cam5', latency_ms=500.5)[0])
        self.assertFalse(m.set_camera_info('cam5', quality='4k')[0])
        self.assertTrue(m.set_camera_info('cam5', encrypt=True)[0])
        cam = self.cam('cam5')
        self.assertEqual((cam['protocol'], cam['latency_ms'], cam['quality'], cam['encrypt']), ('srt', 500, '720p-low', True))
        self.assertRegex(cam['passphrase'], r'^[A-Za-z0-9_-]{16}$')
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)  # holds the passphrase
        m._camera_info.clear(); m.load_camera_setting()
        self.assertEqual(self.cam('cam5')['passphrase'], cam['passphrase'])
        # Switching encryption off keeps the passphrase for later; a new one on request.
        m.set_camera_info('cam5', encrypt=False)
        self.assertEqual(self.cam('cam5')['passphrase'], cam['passphrase'])
        m.set_camera_info('cam5', new_passphrase=True)
        self.assertNotEqual(self.cam('cam5')['passphrase'], cam['passphrase'])

    def test_broken_saved_settings_fall_back_to_defaults(self):
        self.path.write_text(json.dumps({'cam2': {'protocol': 'udp', 'latency_ms': 'x', 'encrypt': True,
                                                  'passphrase': 'short', 'quality': '8k'}}))
        m.load_camera_setting()
        cam = self.cam('cam2')
        self.assertEqual((cam['protocol'], cam['latency_ms'], cam['encrypt'], cam['quality']), ('rtmp', 300, False, '1080p'))

    def test_encryption_change_reloads_the_running_server(self):
        self.running.return_value = True
        m.set_camera_info('cam5', label='Interview')
        self.config.assert_not_called()  # a name change never touches MediaMTX
        ok, message = m.set_camera_info('cam5', encrypt=True)
        self.assertIn('reconnects', message)
        self.config.assert_called_once()
        m.set_camera_info('cam5', latency_ms=400)
        self.assertEqual(self.config.call_count, 1)

    def test_mediamtx_accepts_srt_and_checks_passphrases(self):
        m.set_camera_info('cam6', encrypt=True)
        with tempfile.TemporaryDirectory() as tmp, patch.object(m, 'MEDIAMTX_CONFIG', tmp + '/mtx.yml'), \
                patch.object(m, '_ensure_offline_card', return_value=False):
            config = json.loads(Path(REAL_GENERATE()).read_text())
        self.assertTrue(config['srt'])
        self.assertEqual(config['srtAddress'], ':8890')
        self.assertEqual(config['paths']['cam6']['srtPublishPassphrase'], self.cam('cam6')['passphrase'])
        self.assertNotIn('srtPublishPassphrase', config['paths']['cam5'])

    def test_stream_id_carries_the_camera_password(self):
        with patch.object(m, 'camera_password', return_value=''):
            self.assertEqual(self.cam('cam3')['srt_stream_id'], 'publish:cam3')
        with patch.object(m, 'camera_password', return_value='pw'):
            self.assertEqual(self.cam('cam3')['srt_stream_id'], 'publish:cam3:camera:pw')


class CameraInputStatsTests(unittest.TestCase):
    def setUp(self):
        for name in ('_inputs', '_input_bytes', '_srt_stats', '_camera_audio', '_camera_source'):
            self.enterContext(patch.object(m, name, {}))
        self.enterContext(patch.object(m, '_camera_status', {'cam5': True}))
        self.enterContext(patch.object(m, '_camera_info', {}))
        self.enterContext(patch.object(m, 'CAMERA_SETTING', Path('/nonexistent/cameras.json')))

    def path(self, received, source='s1'):
        return {'name': 'cam5', 'ready': True, 'source': {'type': 'srtConn', 'id': source},
                'tracks': ['H265', 'MPEG-4 Audio'], 'inboundBytes': received,
                'tracks2': [{'codec': 'H265', 'codecProps': {'width': 1280, 'height': 720}},
                            {'codec': 'MPEG-4 Audio', 'codecProps': {}}]}

    def paths(self, *items):
        response = MagicMock(); response.json.return_value = {'items': list(items)}
        return patch.object(m.requests, 'get', return_value=response)

    def srt(self, received, lost, rtt=12.4):
        response = MagicMock()
        response.json.return_value = {'items': [{'path': 'cam5', 'state': 'publish', 'packetsReceived': received,
                                                 'packetsReceivedLoss': lost, 'msRTT': rtt, 'packetsReceivedDrop': 0}]}
        return patch.object(m.requests, 'get', return_value=response)

    def test_protocol_codec_size_bitrate_and_srt_loss(self):
        with patch.object(m.time, 'monotonic', return_value=100.0), self.paths(self.path(0)):
            m._paths()
        with patch.object(m.time, 'monotonic', return_value=102.0), self.paths(self.path(750_000)):
            m._paths()  # 750 kB in 2 s = 3000 kbit/s
        with self.srt(1000, 0):
            m._update_srt_stats()
        with self.srt(1900, 100):
            m._update_srt_stats()  # 100 lost of 1000 sent since the last look
        cam = m.get_camera_inputs()['cam5']
        self.assertEqual((cam['protocol'], cam['codec'], cam['width'], cam['height']), ('srt', 'H265', 1280, 720))
        self.assertEqual((cam['kbps'], cam['loss_pct'], cam['rtt_ms'], cam['audio']), (3000, 10.0, 12, True))
        self.assertIn('packets lost', cam['warning'])

    def test_low_bitrate_and_small_picture_are_flagged(self):
        m.load_camera_setting()
        m._inputs['cam5'] = {'protocol': 'rtmp', 'codec': 'H264', 'width': 640, 'height': 360, 'kbps': 900, 'audio': True}
        warning = m.get_camera_inputs()['cam5']['warning']
        self.assertIn('only 0.9 of 3 Mbit/s', warning)
        self.assertIn('set 1280x720 in the app', warning)

    def test_reconnect_starts_fresh_counters(self):
        with patch.object(m.time, 'monotonic', return_value=100.0), self.paths(self.path(5_000_000)):
            m._paths()
        with patch.object(m.time, 'monotonic', return_value=102.0), self.paths(self.path(1000, source='s2')):
            m._paths()
        self.assertIsNone(m._inputs['cam5'].get('kbps'))  # no negative or huge jump

    def test_offline_camera_has_no_input(self):
        m._inputs['cam5'] = {'protocol': 'srt'}
        m._camera_status['cam5'] = False
        self.assertEqual(m.get_camera_inputs(), {})


class CameraLinkApiTests(unittest.TestCase):
    setUp, post = ApiTests.setUp, ApiTests.post

    def cams(self, **cam5):
        cams = [dict(c) for c in m.get_cameras()]
        cams[4].update(cam5)
        import app as a
        return patch.object(a, 'get_cameras', return_value=cams)

    def test_tally_shows_the_srt_link_only_without_secrets(self):
        import app as a
        with self.cams(protocol='srt', latency_ms=400), patch.object(a, 'camera_password', return_value=''):
            page = self.client.get('/tally/cam5').data.decode()
        self.assertIn('srt://localhost:8890?streamid=publish%3Acam5&amp;latency=400000', page)
        self.assertIn('REP 1', page)
        self.assertIn('1280x720 · 30 fps · 3 Mbit/s', page)
        with self.cams(protocol='srt', encrypt=True, passphrase='secretpassphrase'), patch.object(a, 'camera_password', return_value=''):
            page = self.client.get('/tally/cam5').data.decode()
        self.assertNotIn('secretpassphrase', page)
        self.assertNotIn('srt://', page)

    def test_settings_api_validates_types(self):
        import app as a
        with patch.object(a, 'set_camera_info', return_value=(True, 'Camera saved')) as save:
            self.assertEqual(self.post('/api/cameras/cam5', {'protocol': 'srt', 'latency_ms': 400, 'encrypt': True}).status_code, 200)
            save.assert_called_with('cam5', protocol='srt', latency_ms=400, encrypt=True)
            self.assertEqual(self.post('/api/cameras/cam5', {'latency_ms': '400'}).status_code, 400)
            self.assertEqual(self.post('/api/cameras/cam5', {'encrypt': 'yes'}).status_code, 400)
        self.assertEqual(self.client.post('/api/cameras/cam5', json={'protocol': 'srt'}).status_code, 401)


class NvidiaEncoderTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        for name, value in dict(ENCODER_SETTING=Path(tmp.name) / 'encoder.json', _hw_wanted='software', _hw_device=None,
                                _hw_warning='', _relay_hw_failed=False, _nvenc_ok=None, _encoder_proc=None,
                                _encoder_hw=False, _want_stream=False).items():
            self.enterContext(patch.object(m, name, value))

    def test_nvidia_is_refused_without_a_working_gpu(self):
        with patch.object(m, '_test_nvenc', return_value=False):
            ok, message = m.set_hardware_encoding('nvidia')
        self.assertFalse(ok)
        self.assertIn('NVIDIA', message)
        self.assertEqual(m._hw_wanted, 'software')
        self.assertFalse(m.set_hardware_encoding('quantum')[0])

    def test_nvidia_encodes_final_picture_and_relays(self):
        with patch.object(m, '_test_nvenc', return_value=True):
            self.assertTrue(m.set_hardware_encoding('nvidia')[0])
        self.assertEqual(json.loads(m.ENCODER_SETTING.read_text()), {'encoder': 'nvidia'})
        with patch.object(m, 'Process') as proc, patch.object(m, 'ensure_frame'):
            m._launch_encoder()
        cmd = proc.call_args.args[0]
        self.assertEqual(cmd[cmd.index('-c:v') + 1], 'h264_nvenc')
        self.assertNotIn('-vaapi_device', cmd)
        self.assertEqual(m._relay_video_codec()[:2], ['-c:v', 'h264_nvenc'])
        with patch.object(m, '_probe', return_value={'video', 'audio'}), \
                patch.object(m, '_layout', {'mode': 'single', 'main': 'cam1', 'secondary': 'cam5', 'position': 'top-right'}):
            relay = m._relay_command('cam1', {})[0]
        self.assertEqual(relay[relay.index('-c:v') + 1], 'h264_nvenc')
        self.assertEqual(m.get_encoder_status()['relay'], 'nvidia')

    def test_failed_nvidia_relay_retries_in_software(self):
        with patch.object(m, '_test_nvenc', return_value=True):
            m.set_hardware_encoding('nvidia')
        self.assertTrue(m._relay_nvenc_failed('Relay did not produce frames'))
        self.assertEqual(m._relay_video_codec()[1], 'libx264')
        self.assertIn('relays use the software encoder', m.get_encoder_status()['warning'])
        self.assertFalse(m._relay_nvenc_failed('Relay did not produce frames'))  # only once

    def test_relay_failure_falls_back_within_the_same_cut(self):
        with patch.object(m, '_test_nvenc', return_value=True):
            m.set_hardware_encoding('nvidia')
        codecs = []

        def take_over(cmd, current):
            codecs.append(cmd[cmd.index('-c:v') + 1])
            return (None, 'nvenc: no capable devices') if len(codecs) == 1 else (MagicMock(), None)
        with patch.object(m, '_probe', return_value={'video', 'audio'}), patch.object(m, '_take_over', side_effect=take_over), \
                patch.object(m, '_await_retired'), patch.object(m, '_retire'), patch.object(m, '_log_cut'), \
                patch.object(m, '_generation', 5), patch.object(m, '_relay_proc', None), patch.object(m, '_active_camera', None), \
                patch.object(m, '_layout', {'mode': 'single', 'main': 'cam1', 'secondary': 'cam5', 'position': 'top-right'}):
            ok, _ = m._replace_relay('cam1', 5)
        self.assertTrue(ok)
        self.assertEqual(codecs, ['h264_nvenc', 'libx264'])


class MatchCameraIsoAndCutLogTests(EngineStateCase):
    def setUp(self):
        super().setUp()
        for name, value in dict(_iso={}, _iso_retry={}, _iso_match=False, _cut_logged=None, _replay=None,
                                _relay_secondary=None, _cut_source='manual', _layout=dict(m._layout),
                                _camera_status={'cam1': True, 'cam2': True, 'cam5': True},
                                RECORDING_SETTING=Path(self.tmp) / 'recording.json').items():
            self.stack.enter_context(patch.object(m, name, value))
        self.stack.enter_context(patch.object(m, 'Process', side_effect=lambda cmd, copy=False: MagicMock(
            poll=MagicMock(return_value=None), first_progress=None, cmd=cmd)))
        self.active = self.stack.enter_context(patch.object(m, 'get_active_camera', return_value='cam1'))

    def test_match_cameras_recorded_separately_when_chosen(self):
        self.running()
        m._supervise_iso(1.0)
        self.assertEqual(sorted(m._iso), ['cam5'])
        self.assertTrue(m.set_iso_match(True)[0])
        self.assertEqual(json.loads(m.RECORDING_SETTING.read_text()), {'match_cameras': True})
        m._supervise_iso(2.0)
        self.assertEqual(sorted(m._iso), ['cam1', 'cam2', 'cam5'])
        self.assertTrue(m.get_recording_status()['iso_match'])
        m.set_iso_match(False)
        m._supervise_iso(3.0)
        self.assertEqual(sorted(m._iso), ['cam5'])  # stopped at once
        m._iso_match = False
        m.load_recording_setting()
        self.assertFalse(m._iso_match)

    def test_separate_cameras_shorten_the_estimated_recording_time(self):
        room = 10 * m.recordings.GB
        self.assertLess(m.recordings.minutes_left(room, '720p', 4 * 6000 * 1000 / 8 * 3600),
                        m.recordings.minutes_left(room, '720p'))

    def test_cut_log_holds_start_cuts_layouts_and_replays(self):
        self.running()
        m._log_cut()
        m._log_cut()  # nothing changed: nothing logged
        self.active.return_value = 'cam2'
        m._log_cut()
        m._relay_secondary, m._layout = 'cam5', {**m._layout, 'mode': 'pip'}
        m._log_cut()
        m._replay = {'camera': 'cam2'}
        m._log_cut('replay')
        m._replay, m._relay_secondary, m._cut_source = None, None, 'auto'
        self.active.return_value = 'cam1'
        m._log_cut()
        cuts = m.recordings.cuts(m._recording)
        self.assertEqual([(c['camera'], c['source'], c['mode'], c['secondary']) for c in cuts],
                         [('cam1', 'start', 'single', None), ('cam2', 'manual', 'single', None),
                          ('cam2', 'manual', 'pip', 'cam5'), ('replay', 'replay', 'single', None),
                          ('cam1', 'auto', 'single', None)])
        csv = m.recordings.cuts_csv(m._recording).splitlines()
        self.assertEqual(csv[0], 'time,offset_s,wall_clock,camera,source,layout,second_camera')
        self.assertEqual(len(csv), 6)
        self.assertEqual(m.recordings.get(m._recording)['cuts'], 5)

    def test_no_cut_log_without_recording(self):
        m._log_cut()
        self.assertIsNone(m._cut_logged)


class MountSettingTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.enterContext(patch.object(m, 'CAMERA_SETTING', Path(tmp.name) / 'cameras.json'))
        self.enterContext(patch.object(m, '_camera_info', {}))
        self.enterContext(patch.object(m, 'get_mediamtx_running', return_value=False))

    def test_mount_and_one_wide_camera(self):
        cams = {c['id']: c for c in m.get_cameras()}
        self.assertEqual((cams['cam1']['mount'], cams['cam5']['mount'], cams['cam1']['wide']), ('fixed', 'handheld', False))
        self.assertTrue(m.set_camera_info('cam3', mount='tracking')[0])
        self.assertFalse(m.set_camera_info('cam3', mount='drone')[0])
        m.set_camera_info('cam1', wide=True)
        m.set_camera_info('cam2', wide=True)  # only one wide shot
        cams = {c['id']: c for c in m.get_cameras()}
        self.assertEqual([c for c in m.MATCH_CAMERAS if cams[c]['wide']], ['cam2'])
        self.assertFalse(m.set_camera_info('cam5', wide=True)[0])
        self.assertEqual(cams['cam3']['mount'], 'tracking')


del ApiTests, EngineStateCase  # run the imported base tests once, in their own module

if __name__ == '__main__':
    unittest.main()

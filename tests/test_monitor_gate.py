"""Headset monitor and microphone noise gate (21 Nov 2026: PA music and crowd noise picked
up by open microphones got the YouTube stream blocked)."""
import json
from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch

from test_control import EngineStateCase, m
import network_manager as nm


def graph_of(cmd):
    return cmd[cmd.index('-filter_complex') + 1]


class GateAndMonitorMixTests(EngineStateCase):
    def setUp(self):
        super().setUp()
        for key, value in dict(_mic_delay_ms=0, _replay=None, _muted=False, _relay_zmq=None,
                               _gate={'on': True, 'threshold_db': -40.0},
                               AUDIO_SETTING=Path(self.tmp, 'audio.json'),
                               _layout={'mode': 'single', 'main': 'cam1', 'secondary': 'cam2', 'position': 'top-right'}).items():
            self.stack.enter_context(patch.object(m, key, value))

    def command(self, wanted):
        with patch.object(m, '_probe', return_value={'video', 'audio'}), \
                patch.object(m, '_usb_device_available', return_value=True):
            return m._relay_command('cam1', wanted)

    def test_microphone_is_gated_before_its_fader_and_left_out_of_the_monitor(self):
        cmd, channels, *_ = self.command({'cam1': 0.0, 'usb:mic': -6.0})
        graph = graph_of(cmd)
        self.assertEqual(channels, {'cam1': 'm0', 'usb:mic': 'm1'})
        self.assertRegex(graph, r'agate@g1=threshold=0\.010000:range=0\.03:[^,;]*,volume@m1=')
        self.assertNotIn('agate@g0', graph)  # camera sound is not gated
        self.assertIn('[silencex][m0x]amix=inputs=2', graph)  # monitor: camera only
        self.assertNotIn('[m1x]', graph)
        self.assertIn('[silence][m0][m1]amix=inputs=3', graph)  # program: everything

    def test_monitor_is_a_second_output_that_cannot_break_the_program(self):
        cmd, *_ = self.command({'cam1': 0.0})
        program = cmd.index(f'{m.RTSP_BASE}/program')
        mon = cmd.index('[mon]')
        self.assertGreater(mon, program)
        self.assertEqual(cmd[-3:], ['-f', 'tee', f'[f=rtsp:rtsp_transport=tcp:onfail=ignore]{m.RTSP_BASE}/monitor'])

    def test_silent_mix_still_feeds_the_monitor(self):
        cmd, channels, *_ = self.command({})
        self.assertIn('[silencex]amix=inputs=1', graph_of(cmd))
        self.assertEqual(channels, {})

    def test_gate_off_opens_fully(self):
        m._gate = {'on': False, 'threshold_db': -40.0}
        cmd, *_ = self.command({'usb:mic': 0.0})
        self.assertIn('agate@g0=threshold=0.010000:range=1:', graph_of(cmd))

    def test_gate_switches_live_without_a_cut(self):
        m._active_camera, m._relay_channels = 'cam1', {'cam1': 'm0', 'usb:mic': 'm1'}
        with patch.object(m, '_send_command', return_value=True) as send, \
                patch.object(m, 'switch_camera') as cut:
            self.assertTrue(m.set_gate(on=False)[0])
            self.assertTrue(m.set_gate(on=True, threshold_db=-40)[0])  # same threshold: still live
        self.assertEqual([c.args[1] for c in send.call_args_list], ['agate@g1 range 1', 'agate@g1 range 0.03'])
        cut.assert_not_called()

    def test_new_threshold_rebuilds_a_relay_with_a_microphone(self):
        # ffmpeg 6.1's agate ignores a live threshold command.
        m._active_camera, m._relay_channels = 'cam1', {'cam1': 'm0', 'usb:mic': 'm1'}
        with patch.object(m, '_send_command', return_value=True) as send, \
                patch.object(m, 'get_active_camera', return_value='cam1'), \
                patch.object(m, 'switch_camera', return_value=(True, 'ok')) as cut:
            ok, message = m.set_gate(threshold_db=-30)
        self.assertTrue(ok, message)
        send.assert_not_called()
        cut.assert_called_once_with('cam1', force=True)
        self.assertEqual(json.loads(m.AUDIO_SETTING.read_text())['gate'], {'on': True, 'threshold_db': -30.0})

    def test_threshold_without_a_microphone_on_air_needs_no_cut(self):
        m._active_camera, m._relay_channels = 'cam1', {'cam1': 'm0'}
        with patch.object(m, 'switch_camera') as cut:
            self.assertTrue(m.set_gate(threshold_db=-30)[0])
        cut.assert_not_called()

    def test_gate_rebuilds_the_relay_when_it_does_not_answer(self):
        m._active_camera, m._relay_channels = 'cam1', {'usb:mic': 'm0'}
        with patch.object(m, '_send_command', return_value=False), \
                patch.object(m, 'get_active_camera', return_value='cam1'), \
                patch.object(m, 'switch_camera', return_value=(True, 'ok')) as cut:
            self.assertTrue(m.set_gate(on=False)[0])
        cut.assert_called_once_with('cam1', force=True)

    def test_gate_validation(self):
        for args in ({'threshold_db': -80}, {'threshold_db': 0}, {'threshold_db': '-40'}, {'on': 'yes'}):
            self.assertFalse(m.set_gate(**args)[0], args)
        self.assertEqual(m._gate, {'on': True, 'threshold_db': -40.0})


class AudioSettingTests(EngineStateCase):
    def setUp(self):
        super().setUp()
        for key, value in dict(_mic_delay_ms=0, _gate={'on': True, 'threshold_db': -45.0},
                               _monitor={'on': False, 'mode': 'nomic', 'output': ''},
                               AUDIO_SETTING=Path(self.tmp, 'audio.json')).items():
            self.stack.enter_context(patch.object(m, key, value))
        self.stack.enter_context(patch.object(m, 'start_monitor_worker'))

    def test_settings_survive_a_restart_together(self):
        m.set_mic_delay(700)
        m.set_gate(on=False, threshold_db=-50)
        m.set_monitor(on=True, mode='full', output='alsa_output.usb-headset')
        m._mic_delay_ms, m._gate, m._monitor = 0, {}, {}
        m.load_audio_setting()
        self.assertEqual(m._mic_delay_ms, 700)
        self.assertEqual(m._gate, {'on': False, 'threshold_db': -50.0})
        self.assertEqual(m._monitor, {'on': True, 'mode': 'full', 'output': 'alsa_output.usb-headset'})

    def test_old_and_broken_files_give_safe_defaults(self):
        m.AUDIO_SETTING.write_text(json.dumps({'mic_delay_ms': 500}))  # before gate/monitor existed
        m.load_audio_setting()
        self.assertEqual((m._mic_delay_ms, m._gate['on'], m._monitor['on']), (500, True, False))
        m.AUDIO_SETTING.write_text(json.dumps({'gate': {'threshold_db': 5}, 'monitor': {'on': True, 'output': ''}}))
        m.load_audio_setting()
        self.assertEqual((m._gate['threshold_db'], m._monitor['on']), (-45.0, False))
        m.AUDIO_SETTING.write_text('[1, 2]')
        m.load_audio_setting()
        self.assertEqual(m._mic_delay_ms, 0)

    def test_monitor_needs_a_chosen_output(self):
        self.assertFalse(m.set_monitor(on=True)[0])
        self.assertFalse(m.set_monitor(mode='loud')[0])
        self.assertFalse(m.set_monitor(output='bad\nname')[0])
        self.assertTrue(m.set_monitor(on=True, output='alsa_output.usb-headset')[0])
        self.assertTrue(m.get_mixer()['monitor']['on'])


class MonitorPlayerTests(unittest.TestCase):
    def setUp(self):
        from contextlib import ExitStack
        self.stack = self.enterContext(ExitStack())
        for key, value in dict(_monitor={'on': True, 'mode': 'nomic', 'output': 'usb-headset'},
                               _monitor_proc=None, _monitor_key=None, _monitor_retry_at=0.0,
                               _monitor_error='', _want_stream=False).items():
            self.stack.enter_context(patch.object(m, key, value))
        self.stack.enter_context(patch.object(m, 'get_mediamtx_running', return_value=True))
        self.kill = self.stack.enter_context(patch.object(m, '_kill_process'))
        self.Process = self.stack.enter_context(patch.object(m, 'Process'))

    def test_commands(self):
        cmd = m._monitor_command('nomic', 'usb-headset')
        self.assertIn(f'{m.RTSP_BASE}/monitor', cmd)
        self.assertEqual(cmd[cmd.index('-device') + 1], 'usb-headset')
        self.assertEqual(cmd[cmd.index('-f', cmd.index('-map')) + 1], 'pulse')
        self.assertIn(f'{m.RTSP_BASE}/final', m._monitor_command('full', 'usb-headset'))

    def test_starts_follows_changes_and_stops(self):
        m._monitor_tick()
        self.Process.assert_called_once()
        self.assertEqual(self.Process.call_args.kwargs, {'copy': True})
        first = m._monitor_proc
        first.poll.return_value = None
        m._monitor_tick()
        self.assertEqual(self.Process.call_count, 1)  # running: left alone
        m._monitor['mode'] = 'full'
        m._monitor_tick()  # changed: replaced at once
        self.kill.assert_called_with(first, 'monitor')
        self.assertEqual(self.Process.call_count, 2)
        self.assertIn(f'{m.RTSP_BASE}/final', self.Process.call_args.args[0])
        m._monitor_proc.poll.return_value = None
        m._monitor['on'] = False
        m._monitor_tick()
        self.assertIsNone(m._monitor_proc)
        self.assertEqual(m.get_monitor()['state'], 'off')

    def test_player_that_stops_is_restarted_after_a_pause(self):
        m._monitor_tick()
        m._monitor_proc.poll.return_value = 1
        m._monitor_proc.error = 'Connection refused'
        m._monitor_tick()
        self.assertIsNone(m._monitor_proc)
        self.assertEqual(m.get_monitor(), {'on': True, 'mode': 'nomic', 'output': 'usb-headset',
                                           'state': 'waiting', 'error': 'Connection refused'})
        m._monitor_tick()
        self.assertEqual(self.Process.call_count, 1)  # still waiting
        m._monitor_retry_at = 0
        m._monitor_tick()
        self.assertEqual(self.Process.call_count, 2)

    def test_full_mode_explains_waiting_off_air(self):
        m._monitor['mode'] = 'full'
        self.assertIn('Nothing is going out yet', m.get_monitor()['error'])

    def test_playing_state(self):
        m._monitor_tick()
        m._monitor_proc.healthy.return_value = True
        self.assertEqual(m.get_monitor()['state'], 'playing')
        m._monitor['mode'] = 'full'  # changed, the worker has not replaced the player yet
        self.assertEqual(m.get_monitor()['state'], 'starting')


class MonitorPathTests(unittest.TestCase):
    def test_monitor_path_is_always_readable_audio(self):
        with patch.object(m, '_ensure_offline_card', return_value=False), \
                patch.object(m, 'MEDIAMTX_CONFIG', str(Path(self.enterContext(__import__('tempfile').TemporaryDirectory()), 'x.yml'))):
            config = json.loads(Path(m.generate_mediamtx_config()).read_text())
        self.assertEqual(config['paths']['monitor'], {'alwaysAvailable': True, 'alwaysAvailableTracks': [
            {'codec': 'MPEG4Audio', 'sampleRate': 48000, 'channelCount': 2}]})


class AudioOutputsTests(unittest.TestCase):
    def test_usb_headsets_first_hdmi_last(self):
        sinks = [{'name': 'alsa_output.pci.hdmi', 'description': 'HDMI / DisplayPort 1 Output', 'properties': {'device.bus': 'pci'}},
                 {'name': 'alsa_output.pci.analog', 'description': 'Speaker + Headphones', 'properties': {'device.bus': 'pci'}},
                 {'name': 'bluez_output.AA_BB.1', 'description': 'BT headset', 'properties': {}},
                 {'name': 'alsa_output.usb-Jabra', 'description': 'Jabra Evolve2 40', 'properties': {'device.bus': 'usb'}},
                 {'description': 'no name'}]
        with patch.object(nm, 'run_cmd', return_value=(True, json.dumps(sinks))):
            outputs = nm.get_audio_outputs()
        self.assertEqual([o['id'] for o in outputs], ['alsa_output.usb-Jabra', 'bluez_output.AA_BB.1',
                                                      'alsa_output.pci.analog', 'alsa_output.pci.hdmi'])
        self.assertEqual(outputs[1]['bus'], 'bluetooth')
        with patch.object(nm, 'run_cmd', return_value=(False, '')):
            self.assertEqual(nm.get_audio_outputs(), [])


if __name__ == '__main__':
    unittest.main()

"""Opt-in real-media test of the headset monitor and the microphone noise gate (~1 min).
Uses real MediaMTX/ffmpeg and two temporary PipeWire/PulseAudio null sinks: one plays a
test tone as a fake microphone, the other stands in for the headset. Nothing is audible.
Run: venv/bin/python tests/monitor_smoke.py
"""
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import media_engine as m
from media_smoke import wait

MIC, HEADSET = 'mcc_smoke_mic', 'mcc_smoke_headset'
MIC_SOURCE = MIC + '.monitor'


def peak(args, seconds=2.5):
    """Highest overall peak (dBFS) ffmpeg measures on an audio input."""
    out = subprocess.run(['ffmpeg', '-hide_banner', '-nostdin', *args, '-t', str(seconds), '-map', '0:a:0',
                          '-af', 'astats=measure_perchannel=none:measure_overall=Peak_level', '-f', 'null', '-'],
                         capture_output=True, text=True, timeout=30).stderr
    values = re.findall(r'Peak level dB: (\S+)', out)
    assert values, out[-800:]
    return max(-120.0 if v in ('-inf', 'inf') else float(v) for v in values)


def path_peak(path):
    return peak(['-rtsp_transport', 'tcp', '-i', f'{m.RTSP_BASE}/{path}'])


def run():
    modules, procs = [], []
    original_config = m.generate_mediamtx_config
    with tempfile.TemporaryDirectory() as tmp:
        m.MEDIAMTX_CONFIG = tmp + '/mediamtx.json'
        m.FRAME_PATH = Path(tmp) / 'overlay.png'
        m.SCOREBOARD_PATH = tmp + '/scoreboard.txt'
        Path(m.SCOREBOARD_PATH).write_text('TEST 0 - 0 TEST | 00:00')
        m.AUDIO_SETTING = Path(tmp) / 'audio.json'
        m.RTMP_BASE = 'rtmp://127.0.0.1:21935'
        m.RTSP_BASE = 'rtsp://127.0.0.1:28554'
        m.MEDIAMTX_API = 'http://127.0.0.1:29997'
        m.ZMQ_ADDRESS = 'tcp://127.0.0.1:28799'
        m.recordings.RECORDINGS_DIR = Path(tmp) / 'recordings'

        def config():
            original_config()
            p = Path(m.MEDIAMTX_CONFIG)
            data = json.loads(p.read_text())
            data.update(apiAddress='127.0.0.1:29997', rtspAddress='127.0.0.1:28554',
                        rtmpAddress='127.0.0.1:21935', hls=False, webrtc=False)
            p.write_text(json.dumps(data))
            return str(p)
        m.generate_mediamtx_config = config

        def mic_tone(db):
            """The fake microphone: a 1 kHz tone at about db dBFS peak."""
            proc = subprocess.Popen(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-nostdin', '-re',
                                     '-f', 'lavfi', '-i', 'sine=frequency=1000:sample_rate=48000',
                                     '-af', f'volume={db + 18}dB', '-ac', '2', '-f', 'pulse', '-device', MIC, 'smoke mic'],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            procs.append(proc)
            return proc
        try:
            for sink in (MIC, HEADSET):
                modules.append(subprocess.run(['pactl', 'load-module', 'module-null-sink', f'sink_name={sink}'],
                                              capture_output=True, text=True, check=True).stdout.strip())
            m._usb_device_available = lambda device: device == MIC_SOURCE
            m.set_mic_devices([{'id': MIC_SOURCE, 'pulse_source': MIC_SOURCE}])
            assert m.start_mediamtx(), m.get_stream_status()
            procs.append(subprocess.Popen(
                ['ffmpeg', '-hide_banner', '-loglevel', 'error', '-nostdin', '-re',
                 '-f', 'lavfi', '-i', 'color=c=red:s=640x360:r=30', '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000',
                 '-c:v', 'libx264', '-preset', 'ultrafast', '-tune', 'zerolatency', '-pix_fmt', 'yuv420p', '-g', '30',
                 '-c:a', 'aac', '-ac', '2', '-f', 'flv', f'{m.RTMP_BASE}/cam1'],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
            wait(lambda: m.get_camera_status()['cam1'])
            mic = mic_tone(-24)  # speaking
            assert m.get_audio_source() == 'off'
            assert m.start_recording('480p', 'Monitor test')[0], m.get_stream_status()
            wait(lambda: m.get_recording_status()['writing'] and m.get_active_camera() == 'cam1', seconds=40)
            assert path_peak('monitor') < -60, 'monitor not silent with all audio off'
            print('PASS: the monitor path is readable and silent while all audio is off', flush=True)

            # Camera sound and the microphone on air: the monitor carries only the camera.
            assert m.set_audio_source('cam1')[0]
            assert m.set_mix('usb:' + MIC_SOURCE, on=True)[0]
            wait(lambda: sorted(m.get_stream_status()['audio_on_air']) == sorted(['cam1', 'usb:' + MIC_SOURCE]), seconds=40)
            assert m.set_audio_source('usb:' + MIC_SOURCE)[0]  # microphone only
            wait(lambda: m.get_stream_status()['audio_on_air'] == ['usb:' + MIC_SOURCE], seconds=40)
            time.sleep(2)
            program, monitor = path_peak('program'), path_peak('monitor')
            assert program > -30 and monitor < -60, (program, monitor)
            print(f'PASS: microphone on air ({program:.0f} dB) but not in the monitor ({monitor:.0f} dB)', flush=True)
            assert m.set_audio_source('cam1')[0]
            wait(lambda: m.get_stream_status()['audio_on_air'] == ['cam1'], seconds=40)
            time.sleep(2)
            monitor = path_peak('monitor')
            assert monitor > -30, monitor
            print(f'PASS: camera sound is in the monitor ({monitor:.0f} dB)', flush=True)

            # Noise gate on the microphone (default on at -45 dB).
            assert m.set_audio_source('usb:' + MIC_SOURCE)[0]
            wait(lambda: m.get_stream_status()['audio_on_air'] == ['usb:' + MIC_SOURCE], seconds=40)
            relay = m._relay_proc.proc.pid
            mic.terminate(); mic.wait()
            mic = mic_tone(-58)  # background noise only
            time.sleep(3)
            closed = path_peak('program')
            assert closed < -75, closed
            ok, message = m.set_gate(on=False)
            assert ok, message
            time.sleep(1.5)
            opened = path_peak('program')
            assert -65 < opened < -50, opened
            assert m._relay_proc.proc.pid == relay, 'switching the gate must not rebuild the relay'
            assert m.set_gate(on=True)[0]
            time.sleep(1.5)
            assert path_peak('program') < -75
            assert m._relay_proc.proc.pid == relay
            assert m.set_gate(threshold_db=-65)[0]  # threshold under the noise: open (seamless cut)
            time.sleep(3)
            low = path_peak('program')
            assert -65 < low < -50, low
            assert m.set_gate(threshold_db=-45)[0]
            time.sleep(3)
            assert path_peak('program') < -75
            print(f'PASS: noise gate holds -58 dB noise at {closed:.0f} dB, off passes it ({opened:.0f} dB), '
                  f'a threshold under the noise opens it ({low:.0f} dB)', flush=True)
            mic.terminate(); mic.wait()
            mic = mic_tone(-24)
            time.sleep(2)
            speech = path_peak('program')
            assert speech > -30, speech
            print(f'PASS: speech opens the gate ({speech:.0f} dB)', flush=True)

            # The headset player.
            assert m.set_audio_source('cam1')[0]
            assert m.set_monitor(on=True, mode='nomic', output=HEADSET)[0]
            wait(lambda: m.get_monitor()['state'] == 'playing', seconds=20)
            heard = peak(['-f', 'pulse', '-i', HEADSET + '.monitor'])
            assert heard > -30, heard
            print(f'PASS: the headset plays the monitor ({heard:.0f} dB)', flush=True)
            assert m.set_audio_source('usb:' + MIC_SOURCE)[0]
            time.sleep(3)
            heard = peak(['-f', 'pulse', '-i', HEADSET + '.monitor'])
            assert heard < -60, heard
            print('PASS: the operator does not hear their own microphone in "without my mic"', flush=True)
            assert m.set_monitor(mode='full')[0]
            wait(lambda: m.get_monitor()['state'] == 'playing', seconds=20)
            time.sleep(2)
            heard = peak(['-f', 'pulse', '-i', HEADSET + '.monitor'])
            assert heard > -30, heard
            print(f'PASS: "Exactly YouTube" plays the final output, microphone included ({heard:.0f} dB)', flush=True)
            assert m.set_monitor(mode='nomic')[0]
            wait(lambda: m.get_monitor()['state'] == 'playing', seconds=20)
            player = m._monitor_proc.proc.pid
            assert m.switch_camera('cam1', force=True)[0]
            time.sleep(3)
            assert m._monitor_proc is not None and m._monitor_proc.proc.pid == player, ('player restarted at a cut', m._monitor_error, m.get_monitor())
            print('PASS: the headset player stays connected through cuts', flush=True)
            m.stop_stream()
            time.sleep(3)
            assert m.get_monitor()['state'] == 'playing'
            print('PASS: the headset player keeps running (silence) after STOP', flush=True)
            assert m.set_monitor(on=False)[0]
            wait(lambda: m._monitor_proc is None, seconds=5)
            print('PASS: listening off stops the player', flush=True)
        finally:
            for proc in procs:
                if proc.poll() is None:
                    proc.terminate()
                    proc.wait(timeout=5)
            m.shutdown_all()
            for module in modules:
                subprocess.run(['pactl', 'unload-module', module], capture_output=True)


if __name__ == '__main__':
    run()

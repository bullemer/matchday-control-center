"""Opt-in real-media test, isolated ports and synthetic video; never contacts YouTube.
Run with ffmpeg/ffprobe on PATH: python tests/media_smoke.py
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import media_engine as m


def wait(check, seconds=25):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(.2)
    raise AssertionError('Timed out: ' + str(m.get_stream_status()))


def run():
    sources = []
    original_config = m.generate_mediamtx_config
    with tempfile.TemporaryDirectory() as tmp:
        m.MEDIAMTX_CONFIG = tmp + '/mediamtx.json'
        m.SCOREBOARD_PATH = tmp + '/scoreboard.txt'
        Path(m.SCOREBOARD_PATH).write_text('TEST 0 - 0 TEST | 00:00')
        m.RTMP_BASE = 'rtmp://127.0.0.1:21935'
        m.RTSP_BASE = 'rtsp://127.0.0.1:28554'
        m.MEDIAMTX_API = 'http://127.0.0.1:29997'
        m.YOUTUBE_RTMP_BASE = m.RTMP_BASE  # cam3 is a local test sink
        m.ZMQ_ADDRESS = 'tcp://127.0.0.1:28799'
        def config():
            original_config()
            p = Path(m.MEDIAMTX_CONFIG)
            data = json.loads(p.read_text())
            data.update(apiAddress='127.0.0.1:29997', rtspAddress='127.0.0.1:28554',
                        rtmpAddress='127.0.0.1:21935', hls=False, webrtc=False)
            p.write_text(json.dumps(data))
            return str(p)
        m.generate_mediamtx_config = config
        try:
            assert m.start_mediamtx(), m.get_stream_status()
            def source(camera, color, tone=440):
                cmd = ['ffmpeg', '-hide_banner', '-loglevel', 'error', '-nostdin',
                       '-re', '-f', 'lavfi', '-i', f'color=c={color}:s=640x360:r=30',
                       '-f', 'lavfi', '-i', f'sine=frequency={tone}:sample_rate=48000',
                       '-c:v','libx264','-preset','ultrafast','-tune','zerolatency',
                       '-pix_fmt','yuv420p','-g','30','-c:a','aac','-ac','2',
                       '-f','flv',f'{m.RTMP_BASE}/{camera}']
                return subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            sources += [source('cam1', 'red'), source('cam2', 'blue', 660)]
            wait(lambda: m.get_camera_status()['cam1'] and m.get_camera_status()['cam2'])
            assert m.set_audio_source('active')[0]
            assert m.start_youtube_stream('cam3')[0], m.get_stream_status()
            wait(lambda: m.get_stream_status()['state'] == 'live')
            encoder_pid = m._youtube_proc.proc.pid
            print('PASS: encoder is producing frames', flush=True)
            def picture():
                result = subprocess.run(['ffmpeg','-v','error','-rtsp_transport','tcp','-timeout','5000000',
                    '-i',f'{m.RTSP_BASE}/program','-an','-frames:v','1','-vf','scale=160:90',
                    '-pix_fmt','rgb24','-f','rawvideo','-'],capture_output=True,timeout=15)
                assert result.returncode == 0, result.stderr
                assert len(result.stdout) == 160*90*3
                return result.stdout
            def pixel(frame,x,y):
                i=(y*160+x)*3
                return frame[i:i+3]
            for mode in ('pip','split'):
                assert m.set_layout(mode,'cam1','cam2')[0],m.get_stream_status()
                frame=picture()
                red=pixel(frame,40,70 if mode=='pip' else 45)
                blue=pixel(frame,120,22 if mode=='pip' else 45)
                assert red[0]>150 and red[2]<80,(mode,red)
                assert blue[2]>150 and blue[0]<80,(mode,blue)
                assert m._youtube_proc.proc.pid == encoder_pid and m.get_youtube_running()
                print(f'PASS: {mode} shows both cameras in the expected positions without restarting output',flush=True)
            sources[1].terminate(); sources[1].wait(timeout=5)
            wait(lambda: m.get_stream_status()['secondary_camera'] is None and bool(m.get_stream_status()['layout_warning']),seconds=40)
            assert m.get_active_camera()=='cam1' and m._youtube_proc.proc.pid==encoder_pid
            print('PASS: missing second camera falls back to main-only output',flush=True)
            sources[1]=source('cam2','blue',660)
            wait(lambda:m.get_stream_status()['secondary_camera']=='cam2',seconds=40)
            assert m._youtube_proc.proc.pid==encoder_pid
            print('PASS: returning second camera restores the selected layout',flush=True)
            assert m.set_layout('single','cam1','cam2')[0]
            def metered():
                levels = m.get_audio_levels()
                return all(isinstance(levels.get(k), dict) and levels[k]['peak'] > -40 for k in ('cam1', 'cam2', 'program'))
            wait(metered)
            print(f"PASS: audio meters report live levels {m.get_audio_levels()}", flush=True)
            for camera in ['cam2', 'cam1', 'cam2']:
                ok, message = m.switch_camera(camera)
                assert ok, message
                time.sleep(2)
                assert m._youtube_proc.proc.pid == encoder_pid and m.get_youtube_running(), m.get_stream_status()
            print('PASS: three cuts preserve encoder PID and frame progress', flush=True)
            assert m.set_audio_source('cam1')[0]
            assert m._youtube_proc.proc.pid == encoder_pid
            print('PASS: locked commentary audio preserves output encoder', flush=True)
            assert m.get_stream_status()['audio_on_air'] == ['cam1'], m.get_stream_status()
            sources[0].terminate()
            sources[0].wait(timeout=5)
            lost = time.monotonic()
            wait(lambda: m.get_active_camera() == 'cam2' and m.get_stream_status()['audio_on_air'] == ['cam2']
                 and time.monotonic() - lost > 3, seconds=40)
            assert m.get_stream_status()['audio_warning'], m.get_stream_status()
            assert m._youtube_proc.proc.pid == encoder_pid and m.get_youtube_running(), m.get_stream_status()
            print(f'PASS: commentary loss keeps cam2 picture with its own audio ({time.monotonic() - lost:.1f}s)', flush=True)
            sources[0] = source('cam1', 'red')
            wait(lambda: m.get_stream_status()['audio_on_air'] == ['cam1'] and m.get_active_camera() == 'cam2', seconds=40)
            assert not m.get_stream_status()['audio_warning']
            print('PASS: commentary restored automatically when cam1 returns', flush=True)
            assert m.set_mix('cam2', on=True, gain=0)[0]  # a new source: relay is rebuilt once
            wait(lambda: sorted(m.get_stream_status()['audio_on_air']) == ['cam1', 'cam2'])
            relay_pid = m._relay_proc.proc.pid
            program = lambda: (m.get_audio_levels().get('program') or {}).get('peak', -120)
            assert m.set_mix('cam1', on=False)[0] and m.set_mix('cam2', gain=-60)[0]
            wait(lambda: program() < -60)
            assert m.set_mix('cam2', gain=0)[0]
            wait(lambda: program() > -30)
            assert m._relay_proc.proc.pid == relay_pid and m._youtube_proc.proc.pid == encoder_pid
            print('PASS: live mixing: mute and faders change program level without restarting', flush=True)
            sources[1].terminate()
            sources[1].wait(timeout=5)
            wait(lambda: m.get_active_camera() is None)
            time.sleep(3)
            assert m._youtube_proc.proc.pid == encoder_pid and m.get_youtube_running(), m.get_stream_status()
            print('PASS: source loss keeps output alive on fallback', flush=True)
            assert m.switch_camera('cam1')[0]
            assert m._youtube_proc.proc.pid == encoder_pid
            print('PASS: recovery to available camera', flush=True)
            m._youtube_proc.proc.kill()
            m._youtube_proc.proc.wait(timeout=5)
            wait(lambda: m._youtube_proc.proc.pid != encoder_pid and m.get_stream_status()['state'] == 'live',seconds=35)
            print('PASS: failed encoder automatically restarts and resumes progress', flush=True)
            assert m.stop_youtube_stream()[0]
            assert m.start_youtube_stream('cam3', '1080p')[0], m.get_stream_status()
            wait(lambda: m.get_stream_status()['state'] == 'live')
            probe = subprocess.run(['ffprobe', '-v', 'error', '-rtsp_transport', 'tcp', '-select_streams', 'v:0',
                                    '-show_entries', 'stream=width,height', '-of', 'csv=p=0', f'{m.RTSP_BASE}/program'],
                                   capture_output=True, text=True, timeout=10)
            assert probe.stdout.strip() == '1920,1080', probe.stdout
            print('PASS: 1080p profile streams a 1080p program', flush=True)
            results = []
            cut = threading.Thread(target=lambda: results.append(m.switch_camera('cam1')))
            cut.start()
            time.sleep(.5)
            started = time.monotonic()
            assert m.stop_youtube_stream()[0]
            assert time.monotonic() - started < 5, 'STOP waited for an in-flight cut'
            cut.join(timeout=20)
            assert results and results[0] == (False, m.SUPERSEDED), results
            assert m._relay_proc is None and not m.get_youtube_running(), m.get_stream_status()
            print('PASS: STOP is immediate during a cut and discards the cut', flush=True)
        finally:
            for proc in sources:
                if proc.poll() is None:
                    proc.terminate()
                    proc.wait(timeout=5)
            m.shutdown_all()

ORPHAN = """
import os, sys, time
sys.path.insert(0, sys.argv[1])
import media_engine as m
proc = m.Process(m._ffmpeg() + ['-re', '-f', 'lavfi', '-i', 'color=s=320x240:r=30', '-f', 'null', '-'])
time.sleep(1.5)
print(proc.proc.pid, flush=True)
os._exit(0)  # simulate a crashed control app
"""


def orphan_check():
    root = str(Path(__file__).resolve().parents[1])
    group = int(subprocess.run([sys.executable, '-c', ORPHAN, root], capture_output=True,
                               text=True, timeout=30).stdout.split()[-1])
    def gone():
        try:
            os.killpg(group, 0)
            return False
        except ProcessLookupError:
            return True
    wait(gone, seconds=8)
    print('PASS: media processes exit when the control app dies', flush=True)


if __name__ == '__main__':
    orphan_check()
    run()

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
        m.FRAME_PATH = Path(tmp) / 'overlay.png'
        m.SCOREBOARD_PATH = tmp + '/scoreboard.txt'
        Path(m.SCOREBOARD_PATH).write_text('TEST 0 - 0 TEST | 00:00')
        m.RTMP_BASE = 'rtmp://127.0.0.1:21935'
        m.RTSP_BASE = 'rtsp://127.0.0.1:28554'
        m.MEDIAMTX_API = 'http://127.0.0.1:29997'
        m.YOUTUBE_RTMP_BASE = m.RTMP_BASE  # cam3 is a local test sink
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
            assert m.start_youtube_stream('cam3', record=True, title='Red vs Blue')[0], m.get_stream_status()
            wait(lambda: m.get_stream_status()['state'] == 'live')
            encoder_pid = m._encoder_proc.proc.pid
            print('PASS: encoder is producing frames', flush=True)
            wait(lambda: m.get_recording_status()['writing'])
            session = m.get_recording_status()['session']
            recorder_pid = m._recorder_proc.proc.pid
            m._targets['youtube']['proc'].proc.kill()  # YouTube connection drops
            wait(lambda: m.get_stream_status()['state'] == 'reconnecting', seconds=10)
            wait(lambda: m.get_stream_status()['state'] == 'live', seconds=30)
            assert m._recorder_proc.proc.pid == recorder_pid and m._encoder_proc.proc.pid == encoder_pid
            print('PASS: YouTube reconnect leaves encoder and recording untouched', flush=True)
            time.sleep(3)
            marker = m.recordings.add_marker(session, 'Goal', '12:00', 'Red 1-0 Blue')
            assert marker['offset'] > 5, marker
            print(f"PASS: highlight marked at {marker['offset']}s on the recording", flush=True)
            # A second platform: a separate local RTMP server stands in for Custom RTMP.
            def rtmp_sink():
                sink = subprocess.Popen(['ffmpeg','-hide_banner','-loglevel','error','-listen','1','-i',
                                         'rtmp://127.0.0.1:21998/live/club','-c','copy','-f','null','-'],
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                sources.append(sink)  # cleaned up with the cameras
                time.sleep(1)
                return sink
            sink = rtmp_sink()
            ok, message = m.start_stream({'custom': m.make_target('custom', 'club', 'rtmp://127.0.0.1:21998/live')})
            assert ok and message.startswith('Connecting to Custom RTMP'), message
            custom_state = lambda: m.get_destinations().get('custom', {}).get('state')
            wait(lambda: custom_state() == 'live', seconds=30)
            youtube_pid = m._targets['youtube']['proc'].proc.pid
            print('PASS: second platform added while live', flush=True)
            sink.kill(); sink.wait()
            wait(lambda: custom_state() == 'reconnecting', seconds=30)
            assert m.get_destinations()['youtube']['state'] == 'live' and m._targets['youtube']['proc'].proc.pid == youtube_pid
            assert m._recorder_proc.proc.pid == recorder_pid and m._encoder_proc.proc.pid == encoder_pid
            assert m.get_stream_status()['state'] == 'live'  # still on air elsewhere
            print('PASS: one platform dropping out leaves YouTube, encoder and recording untouched', flush=True)
            sink = rtmp_sink()
            wait(lambda: custom_state() == 'live', seconds=60)
            print('PASS: the dropped platform reconnects by itself', flush=True)
            assert m.stop_stream('custom') == (True, 'Custom RTMP stopped; recording continues')
            assert list(m._targets) == ['youtube'] and m._targets['youtube']['proc'].proc.pid == youtube_pid
            sink.kill()
            print('PASS: stopping one platform keeps the others live', flush=True)
            def program_pixels():
                result = subprocess.run(['ffmpeg','-v','error','-rtsp_transport','tcp','-timeout','5000000',
                    '-i',f'{m.RTSP_BASE}/program','-an','-frames:v','1','-vf','scale=160:90',
                    '-pix_fmt','rgb24','-f','rawvideo','-'],capture_output=True,timeout=15)
                assert result.returncode == 0, result.stderr
                frame = result.stdout
                return lambda x, y: frame[(y*160+x)*3:(y*160+x)*3+3]
            def watch_program(seconds):
                """Average colour of every frame viewers get (the finished output, all 30 per second)
                while something switches."""
                return subprocess.Popen(['ffmpeg','-v','error','-rtsp_transport','tcp','-i',f'{m.RTSP_BASE}/final',
                    '-an','-t',str(seconds),'-vf','crop=iw:ih*0.8:0:0,scale=1:1','-pix_fmt','rgb24','-f','rawvideo','-'],
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            def colours(watcher):
                data = watcher.communicate(timeout=60)[0]
                return [data[i:i+3] for i in range(0, len(data) - 2, 3)]
            def dark_runs(frames):
                """Lengths of dark spells: a handover may show 1-2 frames of the offline card."""
                runs, run = [], 0
                for c in frames + [b'\xff\xff\xff']:
                    if max(c) < 60:
                        run += 1
                    elif run:
                        runs.append(run); run = 0
                return runs
            watcher = watch_program(12)
            time.sleep(2)
            started = time.monotonic()
            assert m.switch_camera('cam2')[0]
            cut_time = time.monotonic() - started
            frames = colours(watcher)
            runs = dark_runs(frames)
            assert len(frames) > 250 and max(runs, default=0) <= 2, (len(frames), runs)
            print(f'PASS: cut red→blue in {cut_time:.1f} s with the old picture on air until the switch ({len(frames)} frames; handover blips: {runs or "none"})', flush=True)
            time.sleep(9)  # blue footage in the recording
            assert m.switch_camera('cam1')[0]  # live is red again
            recorder_pid = m._recorder_proc.proc.pid
            watcher = watch_program(30)
            time.sleep(2)
            started = time.monotonic()
            ok, message = m.play_replay(15)  # reaches back into the blue period
            replay_start = time.monotonic() - started
            assert ok, message
            assert m.get_replay_status()['active'] and m.get_active_camera() is None
            def replay_on_program():  # the first frame read may still be from the switch-over
                px = program_pixels()
                blue, label = px(80, 60), px(8, 6)
                return blue[2] > 150 and blue[0] < 80 and label[0] > 150 and label[2] < 90
            wait(replay_on_program, seconds=5)
            assert not m.get_stream_status()['fallback']
            wait(lambda: not m.get_replay_status()['active'] and m.get_active_camera() == 'cam1', seconds=25)
            assert m._encoder_proc.proc.pid == encoder_pid and m._recorder_proc.proc.pid == recorder_pid
            assert m.get_youtube_running()
            frames = colours(watcher)
            runs = dark_runs(frames)
            assert max(runs, default=0) <= 2, ('dark spell around the replay', runs, len(frames))
            print(f'PASS: replay on air after {replay_start:.1f} s, shows recorded footage with a REPLAY label, '
                  f'returns to live; {len(frames)} frames, handover blips: {runs or "none"}', flush=True)
            assert m.play_replay(4, 0.5)[0]
            on_air = time.monotonic()
            wait(lambda: not m.get_replay_status()['active'], seconds=20)
            airtime = time.monotonic() - on_air
            assert 6 <= airtime <= 13, airtime  # 8 s of slow motion; live takes over when ready
            print(f'PASS: slow-motion replay of 4 s is on air for {airtime:.1f} s', flush=True)
            wait(lambda: m.get_active_camera() == 'cam1', seconds=15)
            assert m.play_replay(marker=marker['id'])[0]
            assert m.switch_camera('cam2')[0]  # operator cut ends the replay at once
            assert not m.get_replay_status()['active'] and m.get_active_camera() == 'cam2'
            time.sleep(2)
            assert m.get_active_camera() == 'cam2' and m._encoder_proc.proc.pid == encoder_pid
            print('PASS: highlight replay starts and an operator cut ends it', flush=True)
            # The end of a quick replay: how long its last frame is held (or how much is cut off).
            time.sleep(8)
            assert m.play_replay(6)[0]
            clip_end = m.get_replay_status()['ends_at']
            wait(lambda: not m.get_replay_status()['active'], seconds=20)
            held = time.time() - clip_end  # > 0: last frame held; < 0: live took over early
            assert -2.5 < held < 2.5, ('replay end', held)
            print(f'PASS: quick replay hands back to live {abs(held):.1f} s {"after" if held > 0 else "before"} its end (cuts take ~{m._cut_seconds:.1f} s here)', flush=True)
            wait(lambda: not m.get_replay_status()['active'] and m.get_active_camera() == 'cam2', seconds=20)
            # CAM REPLAY: preview still, play from a chosen moment, stay on air until a cut.
            edge = m.recordings.get(session, live_wall=time.time())['duration'] - m.REPLAY_MARGIN
            still = m.recordings.frame_at(session, edge - 20)
            assert still[:2] == b'\xff\xd8' and len(still) > 1000, len(still)
            ok, message = m.play_replay(start=edge - 4, speed=1.0, hold=True)
            assert ok and message.startswith('CAM REPLAY on air'), message
            time.sleep(10)  # well past the 4 s that were available
            status = m.get_replay_status()
            assert status['active'] and status['hold'] and m.get_active_camera() is None, status
            assert m.switch_camera('cam1')[0]
            assert not m.get_replay_status()['active'] and m.get_active_camera() == 'cam1'
            assert m._encoder_proc.proc.pid == encoder_pid
            print('PASS: CAM REPLAY plays from the chosen moment, stays on air past its end until a cut', flush=True)
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
                assert m._encoder_proc.proc.pid == encoder_pid and m.get_youtube_running()
                print(f'PASS: {mode} shows both cameras in the expected positions without restarting output',flush=True)
            sources[1].terminate(); sources[1].wait(timeout=5)
            wait(lambda: m.get_stream_status()['secondary_camera'] is None and bool(m.get_stream_status()['layout_warning']),seconds=40)
            assert m.get_active_camera()=='cam1' and m._encoder_proc.proc.pid==encoder_pid
            print('PASS: missing second camera falls back to main-only output',flush=True)
            sources[1]=source('cam2','blue',660)
            wait(lambda:m.get_stream_status()['secondary_camera']=='cam2',seconds=40)
            assert m._encoder_proc.proc.pid==encoder_pid
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
                assert m._encoder_proc.proc.pid == encoder_pid and m.get_youtube_running(), m.get_stream_status()
            print('PASS: three cuts preserve encoder PID and frame progress', flush=True)
            assert m.set_audio_source('cam1')[0]
            assert m._encoder_proc.proc.pid == encoder_pid
            print('PASS: locked commentary audio preserves output encoder', flush=True)
            assert m.get_stream_status()['audio_on_air'] == ['cam1'], m.get_stream_status()
            sources[0].terminate()
            sources[0].wait(timeout=5)
            lost = time.monotonic()
            wait(lambda: m.get_active_camera() == 'cam2' and m.get_stream_status()['audio_on_air'] == ['cam2']
                 and time.monotonic() - lost > 3, seconds=40)
            assert m.get_stream_status()['audio_warning'], m.get_stream_status()
            assert m._encoder_proc.proc.pid == encoder_pid and m.get_youtube_running(), m.get_stream_status()
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
            assert m._relay_proc.proc.pid == relay_pid and m._encoder_proc.proc.pid == encoder_pid
            print('PASS: live mixing: mute and faders change program level without restarting', flush=True)
            sources[1].terminate()
            sources[1].wait(timeout=5)
            wait(lambda: m.get_active_camera() is None)
            time.sleep(3)
            assert m._encoder_proc.proc.pid == encoder_pid and m.get_youtube_running(), m.get_stream_status()
            print('PASS: source loss keeps output alive on fallback', flush=True)
            # Across the camera loss: no audio flood (that wrote hundreds of seconds per second),
            # and marks/replays follow what is really in the file, whatever the wall clock says.
            info = m.recordings._load(session)
            part = info['parts'][-1]
            closed = sum(p['duration'] for p in info['parts'][:-1])
            written = m.recordings._probe_duration(m.recordings.RECORDINGS_DIR / session / part['file'])
            elapsed = time.time() - part['start']
            assert abs(written - elapsed) < 15, ('recording flooded or stalled', written, elapsed)
            assert abs(m.recordings.written_duration(session) - (closed + written)) < 1
            print(f'PASS: no flood after camera loss ({written:.0f}s written, {elapsed:.0f}s elapsed); marks and replays use the written length', flush=True)
            assert m.switch_camera('cam1')[0]
            assert m._encoder_proc.proc.pid == encoder_pid
            print('PASS: recovery to available camera', flush=True)
            m._encoder_proc.proc.kill()
            m._encoder_proc.proc.wait(timeout=5)
            wait(lambda: m._encoder_proc and m._encoder_proc.proc.pid != encoder_pid and m.get_stream_status()['state'] == 'live',seconds=35)
            print('PASS: failed encoder automatically restarts and resumes progress', flush=True)
            wait(lambda: m.get_recording_status()['writing'] and m._recorder_proc.proc.pid != recorder_pid, seconds=35)
            print('PASS: recording continues in a new part after the encoder restart', flush=True)
            time.sleep(4)
            assert m.stop_recording() == (True, 'Recording stopped; saving the file')
            assert m.get_youtube_running() and m.get_stream_status()['state'] == 'live'
            wait(lambda: m.recordings.get(session)['state'] == 'done', seconds=60)
            info = m.recordings.get(session)
            parts_total = info['duration']
            assert info['file'].endswith('_Red-vs-Blue.mp4') and parts_total > 20, info
            assert not list((m.recordings.RECORDINGS_DIR / session).glob('part-*.ts'))
            clip = m.recordings.export_clip(session, marker['id'])
            clip_len = m.recordings._probe_duration(m.recordings.RECORDINGS_DIR / session / clip)
            expected = min(marker['offset'], m.recordings.CLIP_PRE) + m.recordings.CLIP_POST
            assert abs(clip_len - expected) < 3, (clip_len, expected)  # copy cuts start on a keyframe
            assert m.recordings.build_reel(session) == 'highlights.mp4'
            print(f"PASS: stop recording keeps YouTube live; {info['file']} {parts_total:.0f}s, clip {clip_len:.1f}s, reel", flush=True)
            assert m.stop_youtube_stream()[0]
            assert m.start_recording('480p', 'Record only')[0], m.get_stream_status()
            wait(lambda: m.get_recording_status()['writing'])
            assert m.get_stream_status()['state'] == 'stopped' and not m.get_youtube_running()
            only = m.get_recording_status()['session']
            time.sleep(4)
            assert m.stop_recording()[0] and m._encoder_proc is None and m._relay_proc is None
            wait(lambda: m.recordings.get(only)['state'] == 'done', seconds=60)
            probe = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_entries', 'stream=width,height',
                                    '-of', 'csv=p=0', str(m.recordings.RECORDINGS_DIR / only / m.recordings.get(only)['file'])],
                                   capture_output=True, text=True, timeout=10)
            assert probe.stdout.strip() == '854,480', probe.stdout
            print('PASS: record-only mode records without YouTube and stops the output when done', flush=True)
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

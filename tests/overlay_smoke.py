"""Opt-in Chromium + FFmpeg test. No remote services or broadcast output.
Optionally pass the local score67-de/backend folder to test its actual OBS page.
"""
import logging
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from flask import Flask, jsonify, render_template, send_from_directory
from werkzeug.serving import make_server
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from browser_overlay import BrowserOverlay, BLANK, write_frame


def wait(check, seconds=20):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(.1)
    raise AssertionError('Timed out')


def run(source=None):
    if source:
        source = Path(source).resolve()
        sys.path.insert(0, str(source))
        from score67.domain import initial_state
        state = initial_state()
        state.update(match_id='test', language='en', revision=1)
        for team in ('home', 'away'):
            state[team]['score'] = 0
        app = Flask(__name__, template_folder=str(source/'score67/templates'), static_folder=str(source/'score67/static'))
        @app.get('/s/test/overlay')
        def page():
            return render_template('display.html', overlay=True, private=False, slug='test', lang='en')
        @app.get('/i18n/<name>')
        def language(name):
            return send_from_directory(source.parent/'shared/i18n', name)
    else:
        state = {'home': {'score': 0}}
        app = Flask(__name__)
        @app.get('/s/test/overlay')
        def page():
            return '''<html style="background:transparent"><body class="overlay" style="margin:0;background:transparent"><main id="scoreboard" style="background:lime;width:400px;height:150px;font-size:60px"></main><script>
            async function update(){const s=await(await fetch('/api/s/test/state')).json();document.querySelector('main').textContent=s.home.score;} update(); setInterval(update,300);
            </script></body></html>'''
    @app.get('/api/s/test/state')
    def current():
        return jsonify(dict(state, server_now=int(time.time()*1000)))
    logging.getLogger('werkzeug').setLevel(logging.ERROR)
    server = make_server('127.0.0.1', 0, app, threaded=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    with tempfile.TemporaryDirectory() as tmp:
        overlay = BrowserOverlay(Path(tmp)/'overlay.png')
        encoder = None
        try:
            overlay.start()
            overlay.configure('score67', True, f'http://127.0.0.1:{server.server_port}/s/test/overlay')
            wait(lambda: overlay.status()['state'] == 'ready')
            wait(lambda: overlay.path.read_bytes() != BLANK)
            time.sleep(1)
            first = overlay.path.read_bytes()
            state['home']['score'] = 8
            state['revision'] = state.get('revision', 0) + 1
            wait(lambda: overlay.path.read_bytes() != first)
            print('PASS: browser renders live score changes', flush=True)
            Path('/tmp/mcc-score67-render.png').write_bytes(overlay.path.read_bytes())
            # A single long-running encoder must pick up atomically replaced PNGs.
            command = ['ffmpeg','-v','error','-nostdin','-filter_complex_threads','1',
                '-re','-f','lavfi','-i','color=red:s=320x180:r=10',
                '-re','-f','image2','-loop','1','-framerate','2','-i',str(overlay.path),
                '-filter_complex','[1:v]scale=320:180,format=rgba[o];[0:v][o]overlay=0:0:format=auto,format=rgb24[v]',
                '-map','[v]','-f','rawvideo','pipe:1']
            encoder = subprocess.Popen(command,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
            latest = [None]
            def read():
                while True:
                    frame = encoder.stdout.read(320*180*3)
                    if len(frame) != 320*180*3:
                        return
                    latest[0] = frame
            threading.Thread(target=read,daemon=True).start()
            wait(lambda: latest[0] is not None)
            # Pixel far from the overlay must remain the original red video.
            def red(pixel):
                return pixel[0] > 230 and pixel[1] < 15 and pixel[2] < 15
            assert red(latest[0][-3:]), 'Background is not transparent'
            active = latest[0]
            assert any(not red(active[i:i+3]) for i in range(0,len(active),3)), 'Overlay missing'
            overlay.configure('score67',False,'')
            wait(lambda: all(red(latest[0][i:i+3]) for i in range(0,len(latest[0]),3)), seconds=10)
            assert encoder.poll() is None
            print('PASS: FFmpeg transparency and live hide without encoder restart', flush=True)
            overlay.configure('score67',True,f'http://127.0.0.1:{server.server_port}/missing')
            wait(lambda: overlay.status()['state']=='error')
            assert overlay.path.read_bytes() == BLANK
            print('PASS: invalid page clears overlay and reports an error safely', flush=True)
        finally:
            overlay.stop()
            if encoder:
                encoder.terminate()
                encoder.communicate(timeout=5)
            server.shutdown()

if __name__ == '__main__':
    run(sys.argv[1] if len(sys.argv)>1 else None)

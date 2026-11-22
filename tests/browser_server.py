"""Isolated HTTP fixture: no hardware, runtime state, or real media processes."""
import glob
import os
import shutil
import sys
import tempfile
from pathlib import Path
os.environ['MCC_CAMERA_PASSWORD']='browser-camera'
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import app as a

a.app.config['OPERATOR_PASSWORD']='browser-test'
a._diagnostics={'network':{'internet_ok':True,'hotspot_active':True,'hotspot_ssid':'MatchdayLive','wifi_ip':'10.55.0.1','camera_mode':'hotspot','camera_ip':'10.55.0.1','uplink':'USB tether','usb_iface':'usb0','usb_state':'connected','usb_status':'usb0 (connected)'},'audio_devices':[{'id':'alsa_input.usb-test','name':'Test USB Mic','type':'usb'}]}
a.get_mediamtx_status=lambda: {'running':True}
a.get_camera_status=lambda: {'cam1':True,'cam2':False,'cam3':False,'program':True}
recording={'active':False,'session':None}
a.get_recording_status=lambda: {'active':recording['active'],'session':recording['session'] if recording['active'] else None,
                                'writing':True,'warning':'','duration':65.0,'bytes':150*1024**2,'markers':1}
replay={'active':False,'calls':[]}
a.get_replay_status=lambda: ({'active':True,'available':True,'label':'Replay','speed':replay['speed'],'ends_at':__import__('time').time()+8,'camera':'cam1',
                              'hold':replay.get('hold',False),'position':replay.get('position',0)}
                             if replay['active'] else {'active':False,'available':recording['active']})
def fake_replay(seconds=10, speed=1.0, marker=None, start=None, hold=False):
    replay['calls'].append([seconds,speed,marker] + ([start,hold] if hold else []))
    replay.update(active=True,speed=speed,hold=hold,position=start or 0)
    return True,'Replay on air (10 s)'
a.play_replay=fake_replay
a.stop_replay=lambda: (replay.update(active=False) or True,'Back to live')
# Streaming destinations: Facebook "reconnects", everything else goes live at once.
streams={'calls':[],'stops':[],'live':{}}
def fake_start(targets, profile='720p', record=False, title=''):
    streams['calls'].append({'profile':profile,'record':record,'urls':{n:t['url'] for n,t in targets.items()}})
    for name,target in targets.items():
        streams['live'][name]={'label':target['label'],'state':'live','error':'','started_at':None}
        if name=='facebook':
            streams['live'][name].update(state='reconnecting',error='Connection refused')
    return True,'Going live'
def fake_stop(name=None):
    streams['stops'].append(name)
    streams['live'].pop(name,None) if name else streams['live'].clear()
    return True,'Stopped'
a.start_stream=fake_start
a.stop_stream=fake_stop
@a.app.route('/test/stream-calls')
def stream_calls():
    return a.jsonify(streams)
a.get_stream_status=lambda: {'active':bool(streams['live']),'state':'live' if streams['live'] else 'stopped','active_camera':None,
                             'selected_camera':'cam1' if replay['active'] else None,'destinations':dict(streams['live']),
                             'program_path':'program','encoder':{'active':'software' if streams['live'] else None},
                             'audio_source':'active','audio_on_air':['cam1'],'error':'','fallback':not replay['active'],
                             'recording':a.get_recording_status(),'replay':a.get_replay_status()}
@a.app.route('/test/replay-calls')
def replay_calls():
    return a.jsonify(replay['calls'])
a.start_recording=lambda profile,title='': (recording.update(active=True) or True,'Recording starting')
a.stop_recording=lambda: (recording.update(active=False) or True,'Recording stopped; saving the file')
a.switch_camera=lambda camera, source='manual': (False,'Camera unavailable')
# Real camera settings, saved in a throwaway folder; MediaMTX is never touched.
import media_engine as m
m.CAMERA_SETTING=Path(tempfile.mkdtemp())/'cameras.json'
m.get_mediamtx_running=lambda: False
a.get_camera_inputs=lambda: {'cam1':{'protocol':'srt','codec':'H265','width':1920,'height':1080,'kbps':5800,
                                     'loss_pct':0.4,'rtt_ms':9,'audio':True,'warning':''}}
# cam1 is a Chameleon Stream phone that reports battery / temperature and allows remote zoom.
phone={'battery':64.0,'charging':False,'temp_c':38.0,'thermal':'none','headroom':None,'zoom':0.6,'zoom_min':0.6,
       'zoom_max':10.0,'remote_zoom':True,'mode':'fixed','app':'chameleon-stream test','age_s':1.0,'warning':'','zoom_target':None}
zoom_calls=[]
a.camera_control.get_reports=lambda: {'cam1':dict(phone)}
def fake_zoom(cam, zoom=None, step=None, wide=False):
    zoom_calls.append([cam, zoom, step, wide])
    phone['zoom']=0.6 if wide else round(phone['zoom']*1.25**(step or 0),2) if step else zoom
    return phone['zoom']
a.camera_control.request_zoom=fake_zoom
# AI Director: fake worker state (cam1 best, cam2 rated, cam3 offline) and a 1x1 JPEG thumbnail.
import base64, time as _time
director={'enabled':False,'fps':5}
director_calls=[]
_jpg=Path(tempfile.mkdtemp())/'cam1.jpg'
_jpg.write_bytes(base64.b64decode('/9j/4AAQSkZJRgABAQEASABIAAD/2wBDAAMCAgICAgMCAgIDAwMDBAYEBAQEBAgGBgUGCQgKCgkICQkKDA8MCgsOCwkJDRENDg8QEBEQCgwSExIQEw8QEBD/yQALCAABAAEBAREA/8wABgAQEAX/2gAIAQEAAD8A0s8g/9k='))
def fake_director_status():
    on=director['enabled']
    cam=lambda score, ball, near: {'online':True,'score':score,'raw':score,'image_t':1.0,
        'ball':{'seen':ball,'remembered':False,'conf':0.62 if ball else None,'part':0.8 if ball else 0.0},
        'action':{'part':0.75,'players':9,'near':near},'quality':{'part':0.9,'sharp':0.95,'exposure':0.9,'frozen':False}}
    return {'enabled':on,'fps':director['fps'],'fps_choices':[2,5,10],'phase':'running' if on else 'off','setup_problem':None,
            'gpu':'Test GPU' if on else None,'busy':0.31 if on else None,'recommended':'cam1' if on else None,
            'cameras':['cam1','cam2','cam3','cam4'],'error':'','restarts':0,
            'cams':{'cam1':cam(78.4,True,3),'cam2':cam(41.0,False,1),'cam3':{'online':False}} if on else {}}
def fake_director_configure(enabled=None, fps=None):
    director_calls.append([enabled, fps])
    if enabled is not None: director['enabled']=enabled
    if fps is not None: director['fps']=fps
    return fake_director_status()
a.ai_director.status=fake_director_status
a.ai_director.configure=fake_director_configure
a.ai_director.image_path=lambda cam: _jpg if cam=='cam1' else None
a.ai_director.autostart=lambda: None
a.set_audio_source=lambda mode: (True,'Audio updated')
mic_delay=[]
a.set_mic_delay=lambda ms: (mic_delay.append(ms) or True,f'Microphone delay {ms/1000:.1f} s')  # never the real data/audio.json
audio_calls=[]
def fake_gate(on=None, threshold_db=None):
    audio_calls.append(['gate', on, threshold_db])
    __import__("media_engine")._gate.update({k: v for k, v in (('on', on), ('threshold_db', threshold_db)) if v is not None})
    return True, 'Noise gate updated'
def fake_monitor(on=None, mode=None, output=None):
    audio_calls.append(['monitor', on, mode, output])
    __import__("media_engine")._monitor.update({k: v for k, v in (('on', on), ('mode', mode), ('output', output)) if v is not None})
    return True, 'Listening updated'
a.set_gate=fake_gate  # never the real data/audio.json or a real sound card
a.set_monitor=fake_monitor
a.get_audio_outputs=lambda: [{'id':'alsa_output.usb-headset','name':'USB Headset','bus':'usb'},
                             {'id':'alsa_output.pci.analog','name':'Speaker + Headphones','bus':'pci'}]
@a.app.route('/test/audio-calls')
def audio_calls_list():
    return a.jsonify(audio_calls)
@a.app.route('/test/mic-delay')
def mic_delay_calls():
    return a.jsonify(mic_delay)
# Hotspot controls must never touch the real NetworkManager from tests.
hotspot_calls=[]
a.get_hotspot_config=lambda: {'ssid':'MatchdayLive','password':'saved-secret','saved':True}
a.start_hotspot=lambda ssid,password,force=False: (hotspot_calls.append(('start',ssid,password)) or True,'Hotspot started')
a.stop_hotspot=lambda: (hotspot_calls.append(('stop',)) or True,'Hotspot stopped')
a._refresh_network=lambda: None
a.get_audio_levels=lambda: {'cam1':{'peak':-12.0,'rms':-20.0},'cam2':'no audio',
    'usb:alsa_input.usb-test':{'peak':-30.0,'rms':-38.0},'program':{'peak':-0.5,'rms':-9.0}}
# Playwright kills this server without letting it clean up: remove earlier runs' folders.
for old in glob.glob(tempfile.gettempdir() + '/mcc-browser-*'):
    shutil.rmtree(old, ignore_errors=True)
if True:
    tmp = tempfile.mkdtemp(prefix='mcc-browser-')
    # Recordings never touch the real library: one finished match, one being recorded.
    r=a.recordings
    r.RECORDINGS_DIR=Path(tmp)/'recordings'
    r.free_bytes=lambda: 40*1024**3
    done=r.create_session('720p','Polonia vs HEBC')
    r.new_part(done); r.part_started(done,1000.0)
    r.add_marker(done,'Goal','12:30','Polonia 1-0 HEBC',wall=1750.0)
    r.add_marker(done,'Highlight','40:02','Polonia 1-0 HEBC',wall=3400.0)
    session=r._load(done)
    session['parts'][0]['duration']=5400.0
    session.update(state='done',file='20261101_Polonia-vs-HEBC.mp4',duration=5400.0)
    (r.RECORDINGS_DIR/done/session['file']).write_bytes(b'\0'*1024)
    r._save(session)
    import time as _time; _time.sleep(1.1)  # distinct session id
    recording['session']=r.create_session('720p','Polonia vs Altona')
    # Two minutes "recorded" so far with one highlight, for the CAM REPLAY timeline.
    r.new_part(recording['session']); r.part_started(recording['session'],_time.time()-120)
    r.add_marker(recording['session'],'Chance','58:10','Polonia 0-0 Altona',wall=_time.time()-60)
    a.browser_overlay=a.BrowserOverlay(Path(tmp)/'overlay.png')
    a.SCOREBOARD_DIR=tmp
    a.SCOREBOARD_PATH=tmp+'/scoreboard.txt'
    a.MATCH_PATH=Path(tmp)/'match.json'
    a._keys_path=lambda: Path(tmp)/'stream-keys.json'  # never the real saved keys
    a.app.run(host='127.0.0.1',port=25000,threaded=True)

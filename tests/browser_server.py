"""Isolated HTTP fixture: no hardware, runtime state, or real media processes."""
import sys
import tempfile
from pathlib import Path
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
a.get_replay_status=lambda: ({'active':True,'available':True,'label':'Replay','speed':replay['speed'],'ends_at':__import__('time').time()+8,'camera':'cam1'}
                             if replay['active'] else {'active':False,'available':recording['active']})
a.play_replay=lambda seconds=10,speed=1.0,marker=None: (replay['calls'].append([seconds,speed,marker]) or replay.update(active=True,speed=speed) or True,'Replay on air (10 s)')
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
                             'audio_source':'active','audio_on_air':['cam1'],'error':'','fallback':not replay['active'],
                             'recording':a.get_recording_status(),'replay':a.get_replay_status()}
@a.app.route('/test/replay-calls')
def replay_calls():
    return a.jsonify(replay['calls'])
a.start_recording=lambda profile,title='': (recording.update(active=True) or True,'Recording starting')
a.stop_recording=lambda: (recording.update(active=False) or True,'Recording stopped; saving the file')
a.switch_camera=lambda camera: (False,'Camera unavailable')
a.set_audio_source=lambda mode: (True,'Audio updated')
# Hotspot controls must never touch the real NetworkManager from tests.
hotspot_calls=[]
a.get_hotspot_config=lambda: {'ssid':'MatchdayLive','password':'saved-secret','saved':True}
a.start_hotspot=lambda ssid,password: (hotspot_calls.append(('start',ssid,password)) or True,'Hotspot started')
a.stop_hotspot=lambda: (hotspot_calls.append(('stop',)) or True,'Hotspot stopped')
a._refresh_network=lambda: None
a.get_audio_levels=lambda: {'cam1':{'peak':-12.0,'rms':-20.0},'cam2':'no audio',
    'usb:alsa_input.usb-test':{'peak':-30.0,'rms':-38.0},'program':{'peak':-0.5,'rms':-9.0}}
with tempfile.TemporaryDirectory() as tmp:
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
    a.browser_overlay=a.BrowserOverlay(Path(tmp)/'overlay.png')
    a.SCOREBOARD_DIR=tmp
    a.SCOREBOARD_PATH=tmp+'/scoreboard.txt'
    a.MATCH_PATH=Path(tmp)/'match.json'
    a.app.run(host='127.0.0.1',port=25000,threaded=True)

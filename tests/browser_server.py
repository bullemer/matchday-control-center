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
a.get_stream_status=lambda: {'active':False,'state':'stopped','active_camera':None,'audio_source':'active','audio_on_air':['cam1'],'error':'','fallback':True}
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
    a.SCOREBOARD_DIR=tmp
    a.SCOREBOARD_PATH=tmp+'/scoreboard.txt'
    a.MATCH_PATH=Path(tmp)/'match.json'
    a.app.run(host='127.0.0.1',port=25000,threaded=True)

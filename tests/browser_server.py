"""Isolated HTTP fixture: no hardware, runtime state, or real media processes."""
import sys
import tempfile
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import app as a

a.app.config['OPERATOR_PASSWORD']='browser-test'
a._diagnostics={'network':{'internet_ok':True,'hotspot_active':True,'hotspot_ssid':'MatchdayLive','wifi_ip':'10.55.0.1','camera_mode':'hotspot','camera_ip':'10.55.0.1','uplink':'USB tether','usb_iface':'usb0','usb_status':'usb0 (connected)'},'audio_devices':[{'id':'alsa_input.usb-test','name':'Test USB Mic','type':'usb'}]}
a.get_mediamtx_status=lambda: {'running':True}
a.get_camera_status=lambda: {'cam1':True,'cam2':False,'cam3':False,'program':True}
a.get_stream_status=lambda: {'active':False,'state':'stopped','active_camera':None,'audio_source':'active','error':'','fallback':True}
a.switch_camera=lambda camera: (False,'Camera unavailable')
a.set_audio_source=lambda mode: (True,'Audio updated')
with tempfile.TemporaryDirectory() as tmp:
    a.SCOREBOARD_DIR=tmp
    a.SCOREBOARD_PATH=tmp+'/scoreboard.txt'
    a.MATCH_PATH=Path(tmp)/'match.json'
    a.app.run(host='127.0.0.1',port=25000,threaded=True)

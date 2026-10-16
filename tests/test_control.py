import base64
import copy
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch, MagicMock
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app as a
import media_engine as m
import network_manager as n


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for name, value in [('SCOREBOARD_DIR', self.tmp.name), ('SCOREBOARD_PATH',self.tmp.name+'/scoreboard.txt'), ('MATCH_PATH',Path(self.tmp.name)/'match.json')]:
            p=patch.object(a,name,value); p.start(); self.addCleanup(p.stop)
        self.old_score, self.old_clock = copy.deepcopy(a._scoreboard), copy.deepcopy(a._clock)
        self.addCleanup(lambda: a._scoreboard.update(self.old_score))
        self.addCleanup(lambda: a._clock.update(self.old_clock))
        a.app.config['OPERATOR_PASSWORD']='test'
        self.client=a.app.test_client()
        self.headers={'Authorization':'Basic '+base64.b64encode(b'operator:test').decode()}

    def post(self,url,body):
        return self.client.post(url,json=body,headers=self.headers)

    def test_auth_and_origin(self):
        self.assertEqual(self.client.get('/api/status').status_code,401)
        r=self.client.post('/api/stream/stop',json={},headers={**self.headers,'Origin':'https://evil.invalid'})
        self.assertEqual(r.status_code,403)
        self.assertNotIn('Access-Control-Allow-Origin',r.headers)

    def test_json_validation(self):
        for body in [[],None,'bad',1]:
            self.assertEqual(self.post('/api/scoreboard/update',body).status_code,400)
        for body in [{'home_score':-1},{'home_score':'2'},{'home_name':'a\nb'}, {'home_name':'a'*41}]:
            self.assertEqual(self.post('/api/scoreboard/update',body).status_code,400)
        self.assertEqual(self.post('/api/scoreboard/clock',{'action':'set_half','half':3}).status_code,400)
        self.assertEqual(self.post('/api/stream/start',{'stream_key':123}).status_code,400)

    def test_media_contract(self):
        for url, function, body in [('/api/stream/start','start_youtube_stream',{'stream_key':'test'}),('/api/stream/stop','stop_youtube_stream',{}),('/api/switch/cam2','switch_camera',{}),('/api/audio-source','set_audio_source',{'mode':'active'})]:
            with patch.object(a,function,return_value=(True,'ok')):
                self.assertEqual(self.post(url,body).status_code,200)
            with patch.object(a,function,return_value=(False,'failed')):
                self.assertEqual(self.post(url,body).status_code,502)

    def test_superseded_cut_is_conflict(self):
        with patch.object(a,'switch_camera',return_value=(False,a.SUPERSEDED)):
            r=self.post('/api/switch/cam2',{})
        self.assertEqual(r.status_code,409)
        self.assertTrue(r.json['superseded'])

    def test_hotspot_start_accepts_empty_password(self):
        with patch.object(a,'start_hotspot',return_value=(True,'ok')) as start,patch.object(a,'_refresh_network'):
            self.assertEqual(self.post('/api/network/hotspot-start',{'ssid':'Net','password':''}).status_code,200)
            start.assert_called_once_with('Net','')
        self.assertEqual(self.post('/api/network/hotspot-start',{'ssid':'Net','password':'short'}).status_code,400)

    def test_audio_case_preserved(self):
        with patch.object(a,'set_audio_source',return_value=(True,'ok')) as fn:
            self.post('/api/audio-source',{'mode':'usb:alsa_input.USB-Device'})
            fn.assert_called_once_with('usb:alsa_input.USB-Device')

    def test_score_and_clock(self):
        r=self.post('/api/scoreboard/update',{'home_name':'FC 100%','home_score':2})
        self.assertEqual(r.status_code,200)
        r=self.post('/api/scoreboard/update',{'team':'home','delta':1})
        self.assertEqual(r.json['scoreboard']['home_score'],3)
        self.assertEqual(self.post('/api/scoreboard/clock',{'action':'set_half','half':2}).json['scoreboard']['clock'],'45:00')
        with patch.object(a.time,'monotonic',return_value=100):
            self.post('/api/scoreboard/clock',{'action':'start'})
        with patch.object(a.time,'monotonic',return_value=102):
            a._write_scoreboard_file()
        self.assertIn('45:02',Path(a.SCOREBOARD_PATH).read_text())
        self.assertTrue(a.MATCH_PATH.exists())

    def test_restore_pauses_clock(self):
        self.post('/api/scoreboard/update',{'home_score':5})
        self.post('/api/scoreboard/clock',{'action':'set_half','half':2})
        a._scoreboard['home_score']=0
        a._restore_match()
        self.assertEqual(a._scoreboard['home_score'],5)
        self.assertFalse(a._clock['running'])

    def test_status_uses_cache(self):
        with patch.object(a,'_diagnostics',{'network':{'hotspot_active':True},'audio_devices':[]}), patch.object(a,'get_network_status',side_effect=AssertionError('must not spawn probes')):
            r=self.client.get('/api/status',headers=self.headers)
            self.assertEqual(r.status_code,200)
            self.assertTrue(r.json['network']['hotspot_active'])
            self.assertIn('state',r.json['stream'])
            self.assertIn('clock_running',r.json['scoreboard'])

    def test_shutdown_cancels_delayed_initialization(self):
        with patch.object(a, '_workers_stop', threading.Event()) as stop, patch.object(a, 'ensure_mediamtx', return_value=True), patch.object(a, 'start_mediamtx') as start:
            stop.set()
            a.init_mediamtx()
            start.assert_not_called()

    def test_page_offline_assets(self):
        r=self.client.get('/',headers=self.headers,base_url='http://192.168.5.1:5000')
        self.assertEqual(r.status_code,200)
        self.assertIn('http://192.168.5.1:8889/cam1',r.text)
        self.assertNotIn('cdn.tailwindcss.com',r.text)
        self.assertNotIn('/api/clock/',r.text)


class NetworkTests(unittest.TestCase):
    def test_stop_does_not_disconnect_unowned_wifi(self):
        with patch.object(n,'_owned_uuid',return_value=None),patch.object(n,'run_cmd') as run:
            self.assertFalse(n.stop_hotspot()[0])
            run.assert_not_called()

    def test_stop_only_owned_uuid(self):
        with patch.object(n,'_owned_uuid',return_value='owned'),patch.object(n,'run_cmd',return_value=(True,'')) as run:
            self.assertTrue(n.stop_hotspot()[0])
            self.assertEqual(run.call_args.args[0],['nmcli','connection','down','uuid','owned'])

    def firewall(self, conf, established='', firewalld='inactive', network=None):
        def run(cmd, timeout=10):
            if cmd[:3] == ['ss','-H','-lntu']:
                return True, 'tcp LISTEN 0 128 0.0.0.0:5000 0.0.0.0:*'
            if cmd[:2] == ['ss','-H']:
                return True, established
            return (firewalld == 'active'), firewalld
        n._reached.clear()
        with patch.object(n,'run_cmd',side_effect=run),patch.object(n,'_conf_value',side_effect=lambda path,key: conf.get(key)):
            return n.get_firewall_status(network or {'camera_mode':'hotspot','camera_ip':'10.42.0.1','hotspot_iface':'wlp1s0'})

    def test_firewall_never_claims_deny_is_open(self):
        status=self.firewall({'ENABLED':'yes','DEFAULT_INPUT_POLICY':'drop'})
        self.assertIsNone(status['all_open'])
        self.assertIn('unverified',status['ports']['5000/tcp'])
        self.assertEqual(status['status'],'error')  # 1935 etc. are not listening in this fixture

    def test_enabled_ufw_gives_hotspot_commands(self):
        with patch.object(n,'REQUIRED_TCP_PORTS',[5000]),patch.object(n,'REQUIRED_UDP_PORTS',[]):
            status=self.firewall({'ENABLED':'yes','DEFAULT_INPUT_POLICY':'drop'})
        self.assertEqual(status['status'],'warning')
        self.assertEqual(status['commands'],['sudo ufw allow in on wlp1s0','sudo ufw route allow in on wlp1s0'])

    def test_disabled_ufw_is_no_firewall(self):
        with patch.object(n,'REQUIRED_TCP_PORTS',[5000]),patch.object(n,'REQUIRED_UDP_PORTS',[]):
            status=self.firewall({'ENABLED':'no','DEFAULT_INPUT_POLICY':'drop'})
        self.assertEqual((status['status'],status['label'],status['commands']),('ok','No firewall',[]))

    def test_phone_connection_verifies_reachability(self):
        with patch.object(n,'REQUIRED_TCP_PORTS',[5000]),patch.object(n,'REQUIRED_UDP_PORTS',[]):
            status=self.firewall({'ENABLED':'yes','DEFAULT_INPUT_POLICY':'drop'},
                                 established='0 0 10.42.0.1:5000 10.42.0.47:51234\n0 0 127.0.0.1:5000 127.0.0.1:4000\n0 0 [::ffff:192.168.9.9]:1935 [::ffff:192.168.9.20]:3333')
        self.assertEqual((status['status'],status['reached']),('ok',['10.42.0.47']))

    def test_password_not_in_timeout(self):
        import subprocess
        with patch.object(n.shutil,'which',return_value='/bin/nmcli'),patch.object(n.subprocess,'run',side_effect=subprocess.TimeoutExpired(['nmcli','password','secret'],1)):
            self.assertNotIn('secret',n.run_cmd(['nmcli','password','secret'])[1])

    def fake_nmcli(self, devices, route='8.8.8.8 via 192.168.1.1 dev wlan0 src 192.168.1.50'):
        def run(cmd, timeout=10):
            if cmd[:3] == ['nmcli','-t','-f']:
                return True, devices
            if cmd[:3] == ['nmcli','-g','IP4.ADDRESS']:
                return True, {'wlan0':'192.168.1.50/24','eth0':'10.0.0.5/24'}.get(cmd[-1], '')
            if cmd[:2] == ['ip','-4']:
                return True, route
            return True, ''
        return run

    def test_hotspot_running_unchanged_is_not_restarted(self):
        with patch.object(n,'_owned_uuid',return_value='owned'),patch.object(n,'get_hotspot_config',return_value={'ssid':'Net','password':'secret123','saved':True}),patch.object(n,'_hotspot_active',return_value=True),patch.object(n,'run_cmd') as run:
            self.assertEqual(n.start_hotspot('Net',''),(True,'Hotspot is already running'))
            run.assert_not_called()

    def test_empty_password_reuses_saved(self):
        with patch.object(n,'_owned_uuid',return_value='owned'),patch.object(n,'get_hotspot_config',return_value={'ssid':'Net','password':'secret123','saved':True}),patch.object(n,'_hotspot_active',return_value=False),patch.object(n,'run_cmd',return_value=(True,'')) as run:
            self.assertTrue(n.start_hotspot('Net','')[0])
            modify=run.call_args_list[0].args[0]
            self.assertEqual(modify[modify.index('wifi-sec.psk')+1],'secret123')
            self.assertIn('rsn',modify)

    def test_new_hotspot_requires_password(self):
        with patch.object(n,'_owned_uuid',return_value=None),patch.object(n,'run_cmd') as run:
            self.assertFalse(n.start_hotspot('Net','')[0])
            run.assert_not_called()

    def test_wifi_client_is_camera_network_without_hotspot(self):
        devices='docker0:bridge:connected (externally):docker0\nwlan0:wifi:connected:Club\\:WiFi\neth0:ethernet:unavailable:'
        with patch.object(n,'run_cmd',side_effect=self.fake_nmcli(devices)),patch.object(n,'_owned_uuid',return_value=None),patch.object(n,'_is_usb_device',return_value=False):
            status=n.get_network_status()
        self.assertEqual((status['camera_mode'],status['camera_ip']),('lan','192.168.1.50'))
        self.assertEqual((status['lan_name'],status['lan_type'],status['uplink']),('Club:WiFi','wifi','Club:WiFi'))
        self.assertFalse(status['hotspot_active'])

    def test_usb_tether_is_never_camera_network(self):
        devices='usb0:ethernet:connected:Tether\neth0:ethernet:connected:Wired'
        with patch.object(n,'run_cmd',side_effect=self.fake_nmcli(devices,'8.8.8.8 dev usb0 src 192.168.42.10')),patch.object(n,'_owned_uuid',return_value=None),patch.object(n,'_is_usb_device',side_effect=lambda d: d=='usb0'):
            status=n.get_network_status()
        self.assertEqual(status['usb_iface'],'usb0')
        self.assertEqual((status['camera_mode'],status['lan_iface'],status['uplink']),('lan','eth0','USB tether'))

    def test_pulse_mapping_by_properties(self):
        with patch.object(n,'_parse_arecord_devices',return_value=[dict(card_num=2,device_num=0,card_name='USB Mic',alsa_device='hw:2,0',is_usb=True)]),patch.object(n,'_parse_pulse_sources',return_value=[{'name':'alsa_input.USB-Microphone','properties':{'alsa.card':'2','alsa.device':'0'}}]):
            self.assertEqual(n.get_usb_audio_devices()[0]['id'],'alsa_input.USB-Microphone')


class MediaTests(unittest.TestCase):
    def test_bad_camera_does_not_kill_current(self):
        with patch.object(m,'get_mediamtx_running',return_value=True),patch.object(m,'_relay_command',side_effect=ValueError('offline')),patch.object(m,'_kill_process') as kill:
            self.assertFalse(m.switch_camera('cam2')[0])
            kill.assert_not_called()

    def test_audio_failure_rolls_back_mode(self):
        with patch.object(m,'_active_camera','cam1'),patch.object(m,'_audio_source_mode','cam1'),patch.object(m,'switch_camera',return_value=(False,'bad')):
            self.assertFalse(m.set_audio_source('active')[0])
            self.assertEqual(m.get_audio_source(),'cam1')

    def test_start_without_camera_does_not_launch(self):
        with patch.object(m,'_want_stream',False),patch.object(m.shutil,'which',return_value='/bin/tool'),patch.object(m,'get_mediamtx_running',return_value=True),patch.object(m,'get_active_camera',return_value=None),patch.object(m,'_paths',return_value={}),patch.object(m,'_launch_encoder') as launch:
            self.assertFalse(m.start_youtube_stream('test')[0])
            launch.assert_not_called()

    def test_config_has_fallback_and_protected_program(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(m,'MEDIAMTX_CONFIG',tmp+'/config'):
            m.generate_mediamtx_config()
            config=json.loads(Path(m.MEDIAMTX_CONFIG).read_text())
            self.assertTrue(config['paths']['program']['alwaysAvailable'])
            public=config['authInternalUsers'][0]['permissions']
            self.assertNotIn({'action':'publish','path':'program'},public)

    def test_process_requires_progress(self):
        proc=object.__new__(m.Process)
        proc.proc=MagicMock();proc.proc.poll.return_value=None
        proc.last_progress=0
        self.assertFalse(proc.healthy())
        proc.last_progress=m.time.monotonic()
        self.assertTrue(proc.healthy())

    def test_missing_commentary_degrades_instead_of_failing(self):
        def probe(cam):
            return {'video'} if cam == 'cam1' else {'video', 'audio'}
        with patch.object(m,'_probe',side_effect=probe):
            cmd, actual, warning = m._relay_command('cam2','cam1')
        self.assertEqual(actual,'camera')
        self.assertIn('Cam 1 commentary unavailable',warning)
        self.assertEqual(cmd.count('-i'),1)
        with patch.object(m,'_probe',return_value={'video'}):
            cmd, actual, warning = m._relay_command('cam1','cam1')
        self.assertEqual(actual,'silence')
        self.assertIn('anullsrc=r=48000:cl=stereo',cmd)

    def test_missing_usb_mic_degrades(self):
        with patch.object(m,'_probe',return_value={'video','audio'}),patch.object(m,'_usb_device_available',return_value=False):
            _, actual, warning = m._relay_command('cam2','usb:hw:1,0')
        self.assertEqual(actual,'camera')
        self.assertIn('USB microphone unavailable',warning)

    def test_slow_cut_does_not_hold_control_lock(self):
        entered, release = threading.Event(), threading.Event()
        def slow(cam, mode):
            entered.set(); release.wait(5)
            raise ValueError('offline')
        with patch.object(m,'get_mediamtx_running',return_value=True),patch.object(m,'_relay_command',side_effect=slow):
            worker=threading.Thread(target=m.switch_camera,args=('cam2',)); worker.start()
            self.assertTrue(entered.wait(2))
            self.assertTrue(m._lock.acquire(timeout=1), 'control lock held during probe')
            m._lock.release()
            self.assertEqual(m.switch_camera('cam3',automatic=True),(False,m.BUSY))
            release.set(); worker.join(5)

    def test_supervisor_restarts_mediamtx_when_not_live(self):
        with patch.object(m,'_paths',side_effect=m.requests.ConnectionError()),patch.object(m,'get_mediamtx_running',return_value=False),patch.object(m,'_want_stream',False),patch.object(m,'_mediamtx_retry_at',0),patch.object(m,'start_mediamtx',return_value=False) as start:
            m._supervise_once()
            start.assert_called_once()
            m._supervise_once()  # backoff: not again immediately
            start.assert_called_once()

    def test_1080p_profile_end_to_end_resolution(self):
        with patch.object(m,'_profile','1080p'),patch.object(m,'_probe',return_value={'video','audio'}):
            cmd,_,_=m._relay_command('cam2','active')
            self.assertIn('scale=1920:1080',' '.join(cmd))
            self.assertEqual(cmd[cmd.index('-b:v')+1],'8000k')
            with patch.object(m,'Process') as proc,patch.object(m,'_youtube_stream_key','key'):
                m._launch_encoder()
            enc=proc.call_args.args[0]
            vf=enc[enc.index('-vf')+1]
            self.assertIn('scale=1920:1080',vf); self.assertIn('fontsize=42',vf)
            self.assertEqual((enc[enc.index('-b:v')+1],enc[enc.index('-bufsize')+1]),('6000k','12000k'))

    def test_profile_change_rebuilds_relay_before_live(self):
        with patch.object(m,'_want_stream',False),patch.object(m.shutil,'which',return_value='/bin/tool'),patch.object(m,'get_mediamtx_running',return_value=True),patch.object(m,'get_active_camera',return_value='cam1'),patch.object(m,'_active_camera','cam1'),patch.object(m,'_relay_profile','720p'),patch.object(m,'_profile','720p'),patch.object(m,'switch_camera',return_value=(True,'ok')) as cut,patch.object(m,'_launch_encoder'),patch.object(m.Path,'is_file',return_value=True):
            self.assertTrue(m.start_youtube_stream('key','1080p')[0])
            cut.assert_called_once_with('cam1')
            m.stop_youtube_stream()

    def test_log_redaction(self):
        proc=object.__new__(m.Process)
        proc.secret='SECRET';proc.error=''
        proc._read(io.StringIO('failed rtmp://host/SECRET\n'),False)
        self.assertNotIn('SECRET',proc.error)

if __name__=='__main__':
    unittest.main()

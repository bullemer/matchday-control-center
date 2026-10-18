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
        overlay_patch=patch.object(a, 'browser_overlay', a.BrowserOverlay(Path(self.tmp.name)/'overlay.png'))
        overlay_patch.start(); self.addCleanup(overlay_patch.stop)
        self.old_score, self.old_clock = copy.deepcopy(a._scoreboard), copy.deepcopy(a._clock)
        self.addCleanup(lambda: a._scoreboard.update(self.old_score))
        self.addCleanup(lambda: a._clock.update(self.old_clock))
        a.app.config['OPERATOR_PASSWORD']='test'
        self.client=a.app.test_client()
        self.headers={'Authorization':'Basic '+base64.b64encode(b'operator:test').decode()}

    def post(self,url,body):
        return self.client.post(url,json=body,headers=self.headers)

    def test_external_source_validation_persistence_and_builtin_restoration(self):
        self.assertEqual(self.post('/api/scoreboard/update', {'source':'score67'}).status_code, 400)
        self.assertEqual(self.post('/api/scoreboard/update', {'source':'other'}).status_code, 400)
        result = self.post('/api/scoreboard/update', {'source':'score67', 'overlay_url':'https://score67.de/s/game?k=private&preview=1'})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(Path(a.SCOREBOARD_PATH).read_text(), '')
        a._scoreboard['source'] = 'builtin'
        a._restore_match()
        self.assertEqual(a._scoreboard['source'], 'score67')
        self.assertEqual(a._scoreboard['overlay_url'], 'https://score67.de/s/game/overlay?k=private')
        self.post('/api/scoreboard/update', {'source':'builtin', 'visible':True})
        self.assertIn('HOME', Path(a.SCOREBOARD_PATH).read_text())

    def test_scoreboard_visibility_preserves_match_and_persists(self):
        self.post('/api/scoreboard/update',{'home_score':3})
        self.post('/api/scoreboard/clock',{'action':'set','seconds':123})
        self.post('/api/scoreboard/clock',{'action':'start'})
        response=self.post('/api/scoreboard/update',{'visible':False})
        self.assertEqual(response.status_code,200)
        self.assertFalse(response.json['scoreboard']['visible'])
        self.assertTrue(response.json['scoreboard']['clock_running'])
        self.assertEqual(Path(a.SCOREBOARD_PATH).read_text(),'')
        self.post('/api/scoreboard/update',{'team':'home','delta':1})
        a._write_scoreboard_file()
        self.assertEqual(Path(a.SCOREBOARD_PATH).read_text(),'')
        a._scoreboard['visible']=True
        a._restore_match()
        self.assertFalse(a._scoreboard['visible'])
        self.assertEqual(a._scoreboard['home_score'],4)
        self.post('/api/scoreboard/update',{'visible':True})
        self.assertIn('4 - 0',Path(a.SCOREBOARD_PATH).read_text())
        self.assertIn('02:03',Path(a.SCOREBOARD_PATH).read_text())
        for value in ('false',0,None):
            self.assertEqual(self.post('/api/scoreboard/update',{'visible':value}).status_code,400)

    def test_legacy_match_defaults_to_visible(self):
        state=a._get_scoreboard_state()
        state.pop('visible')
        a.MATCH_PATH.write_text(json.dumps(state))
        a._scoreboard['visible']=False
        a._restore_match()
        self.assertTrue(a._scoreboard['visible'])

    def test_layout_api_validates_and_applies(self):
        with patch.object(a,'set_layout',return_value=(True,'Layout applied')) as layout:
            response=self.post('/api/layout',{'mode':'pip','main':'cam1','secondary':'cam2'})
            self.assertEqual(response.status_code,200)
            layout.assert_called_once_with('pip','cam1','cam2','top-right')
            self.assertEqual(self.post('/api/layout',{'mode':'split','main':'cam1','secondary':'cam1'}).status_code,400)
            self.assertEqual(self.post('/api/layout',{'mode':'wrong','main':'cam1'}).status_code,400)
            self.assertEqual(self.post('/api/layout',{'mode':'pip','main':'cam1','position':'wrong'}).status_code,400)

    def test_auth_and_origin(self):
        self.assertEqual(self.client.get('/api/status').status_code,401)
        r=self.client.post('/api/stream/stop',json={},headers={**self.headers,'Origin':'https://evil.invalid'})
        self.assertEqual(r.status_code,403)
        self.assertNotIn('Access-Control-Allow-Origin',r.headers)

    def test_tally_accessible_without_auth(self):
        r = self.client.get('/tally/cam1')
        self.assertEqual(r.status_code, 200)
        self.assertIn(b'CAM1', r.data)
        self.assertIn(b'RTMP Publishing URL', r.data)

        self.assertEqual(self.client.get('/tally/cam99').status_code, 404)
        self.assertEqual(self.client.get('/api/tally/cam99').status_code, 404)

        with patch.object(a, 'get_camera_status', return_value={'cam1': True, 'cam2': False}), \
             patch.object(a, 'get_stream_status', return_value={'active_camera': 'cam1', 'secondary_camera': None, 'active': True, 'state': 'live'}):
            r = self.client.get('/api/tally/cam1')
            self.assertEqual(r.status_code, 200)
            data = r.json
            self.assertTrue(data['ok'])
            self.assertEqual(data['cam_id'], 'cam1')
            self.assertTrue(data['publishing'])
            self.assertTrue(data['on_air'])
            self.assertFalse(data['secondary'])
            self.assertTrue(data['stream_active'])

            r2 = self.client.get('/api/tally/cam2')
            self.assertEqual(r2.status_code, 200)
            data2 = r2.json
            self.assertFalse(data2['publishing'])
            self.assertFalse(data2['on_air'])
            self.assertFalse(data2['secondary'])

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

    def test_audio_levels_endpoint(self):
        with patch.object(a,'get_audio_levels',return_value={'program':{'peak':-6.0,'rms':-14.0}}):
            r=self.client.get('/api/audio/levels',headers=self.headers)
        self.assertEqual(r.status_code,200)
        self.assertEqual(r.json['levels']['program']['peak'],-6.0)
        self.assertIn('audio_on_air',r.json)
        self.assertIn('channels',r.json['mixer'])

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

    def test_clock_set_and_adjust_keep_running(self):
        with patch.object(a.time,'monotonic',return_value=1000):
            self.post('/api/scoreboard/clock',{'action':'reset'})
            self.post('/api/scoreboard/clock',{'action':'start'})
        with patch.object(a.time,'monotonic',return_value=1096):  # 01:36 running
            r=self.post('/api/scoreboard/clock',{'action':'set','seconds':540})
        self.assertEqual((r.json['scoreboard']['clock'],r.json['scoreboard']['clock_running']),('09:00',True))
        with patch.object(a.time,'monotonic',return_value=1106):  # 10 s later, still running
            r=self.post('/api/scoreboard/clock',{'action':'adjust','seconds':60})
        self.assertEqual(r.json['scoreboard']['clock'],'10:10')
        with patch.object(a.time,'monotonic',return_value=1106):
            self.post('/api/scoreboard/clock',{'action':'pause'})
            r=self.post('/api/scoreboard/clock',{'action':'adjust','seconds':-3600})
        self.assertEqual((r.json['scoreboard']['clock'],r.json['scoreboard']['clock_running']),('00:00',False))
        for body in [{'action':'set','seconds':-1},{'action':'set','seconds':'9:00'},{'action':'adjust','seconds':1.5},{'action':'set','seconds':9000}]:
            self.assertEqual(self.post('/api/scoreboard/clock',body).status_code,400)

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
    def test_https_confirms_internet_when_ping_is_blocked(self):
        with patch.object(n,'run_cmd',return_value=(False,'ping unavailable')), patch.object(n.requests,'head') as head:
            self.assertTrue(n._internet_available())
            head.assert_called_once_with('https://www.youtube.com/',timeout=2,allow_redirects=False)

    def test_internet_uses_second_https_host_if_first_unreachable(self):
        with patch.object(n,'run_cmd',return_value=(False,'')), patch.object(n.requests,'head',side_effect=[n.requests.Timeout(),MagicMock()]) as head:
            self.assertTrue(n._internet_available())
            self.assertEqual(head.call_count,2)

    def test_offline_requires_all_connectivity_probes_to_fail(self):
        with patch.object(n,'run_cmd',return_value=(False,'')), patch.object(n.requests,'head',side_effect=n.requests.ConnectionError()):
            self.assertFalse(n._internet_available())

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

    def test_disabled_ufw_leaves_reachability_unverified(self):
        with patch.object(n,'REQUIRED_TCP_PORTS',[5000]),patch.object(n,'REQUIRED_UDP_PORTS',[]):
            status=self.firewall({'ENABLED':'no','DEFAULT_INPUT_POLICY':'drop'})
        self.assertEqual((status['status'],status['label'],status['commands']),('unknown','Reachability unverified',[]))

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

    def test_get_network_status_non_blocking_skips_internet_probes(self):
        devices = 'docker0:bridge:connected:docker0\nwlan0:wifi:connected:ClubWiFi'
        with patch.object(n, 'run_cmd', side_effect=self.fake_nmcli(devices)), \
             patch.object(n, '_owned_uuid', return_value=None), \
             patch.object(n, '_is_usb_device', return_value=False), \
             patch.object(n, '_internet_available', side_effect=AssertionError("Should not probe internet")) as probe:
            status = n.get_network_status(check_internet=False)
            probe.assert_not_called()
            self.assertEqual(status['camera_mode'], 'lan')

    def test_failed_hotspot_activation_cleans_up_profile(self):
        deleted = []
        def fake_run(cmd, timeout=10):
            if cmd[:2] == ['nmcli', '-t']:
                return True, 'wlan0:wifi'
            if cmd[:3] == ['nmcli', '-g', 'WIFI-PROPERTIES.AP']:
                return True, 'yes'
            if cmd[:3] == ['nmcli', 'connection', 'add']:
                return True, ''
            if cmd[:3] == ['nmcli', 'connection', 'up']:
                return False, 'Error: activation failed'
            if cmd[:3] == ['nmcli', 'connection', 'delete']:
                deleted.append(cmd)
                return True, ''
            return True, ''
        with tempfile.TemporaryDirectory() as tmp:
            test_profile = Path(tmp) / 'hotspot-uuid'
            with patch.object(n, '_owned_uuid', return_value=None), \
                 patch.object(n, 'run_cmd', side_effect=fake_run), \
                 patch.object(n, 'PROFILE_FILE', test_profile):
                ok, msg = n.start_hotspot('TestNet', 'password123')
                self.assertFalse(ok)
                self.assertIn('Hotspot activation failed', msg)
                self.assertTrue(len(deleted) > 0)
                self.assertFalse(test_profile.exists())


class MediaTests(unittest.TestCase):
    def test_bad_camera_does_not_kill_current(self):
        with patch.object(m,'get_mediamtx_running',return_value=True),patch.object(m,'_relay_command',side_effect=ValueError('offline')),patch.object(m,'_kill_process') as kill:
            self.assertFalse(m.switch_camera('cam2')[0])
            kill.assert_not_called()

    def mixer(self, **channels):
        mix={c:{'on':c=='cam1','gain':0.0} for c in m.CAMERA_IDS}
        mix.update(channels)
        return patch.object(m,'_mix',mix)

    def test_audio_failure_rolls_back_mode(self):
        with self.mixer(),patch.object(m,'_follow',False),patch.object(m,'_active_camera','cam1'),patch.object(m,'_relay_proc',None),patch.object(m,'switch_camera',return_value=(False,'bad')):
            self.assertFalse(m.set_audio_source('active')[0])
            self.assertEqual(m.get_audio_source(),'cam1')

    def test_presets_and_custom_mix(self):
        with self.mixer(**{'usb:mic':{'on':False,'gain':0.0}}),patch.object(m,'_follow',False),patch.object(m,'_active_camera',None):
            self.assertEqual(m.get_audio_source(),'cam1')
            self.assertTrue(m.set_audio_source('active')[0])
            self.assertEqual(m.get_audio_source(),'active')
            self.assertEqual(m._effective_mix('cam2'),{'cam2':0.0})  # camera sound follows the cut
            self.assertTrue(m.set_mix('usb:mic',on=True,gain=-6)[0])
            self.assertEqual(m.get_audio_source(),'mix')
            self.assertEqual(m._effective_mix('cam3'),{'cam3':0.0,'usb:mic':-6.0})
            self.assertTrue(m.set_mix('usb:mic',gain=99)[0])
            self.assertEqual(m._mix['usb:mic']['gain'],m.GAIN_MAX)
            self.assertFalse(m.set_mix('nope',on=True)[0])

    def test_gain_change_is_live_without_rebuild(self):
        relay=MagicMock(); relay.poll.return_value=None
        with self.mixer(**{'usb:mic':{'on':True,'gain':0.0}}),patch.object(m,'_follow',True),patch.object(m,'_active_camera','cam2'),patch.object(m,'_relay_proc',relay),patch.object(m,'_relay_channels',{'cam2':'m0','usb:mic':'m1'}),patch.object(m,'_relay_missing',set()),patch.object(m,'_send_volume',return_value=True) as send,patch.object(m,'switch_camera') as cut:
            self.assertTrue(m.set_mix('usb:mic',gain=-20)[0])
            cut.assert_not_called()
            self.assertAlmostEqual(dict((c.args[0],c.args[1]) for c in send.call_args_list)['m1'],0.1,places=3)
            send.reset_mock()
            self.assertTrue(m.set_mix('usb:mic',on=False)[0])  # off = live mute, still no rebuild
            cut.assert_not_called()
            self.assertEqual(dict((c.args[0],c.args[1]) for c in send.call_args_list)['m1'],0.0)

    def test_new_source_rebuilds_relay(self):
        relay=MagicMock(); relay.poll.return_value=None
        with self.mixer(),patch.object(m,'_follow',False),patch.object(m,'_active_camera','cam2'),patch.object(m,'_relay_proc',relay),patch.object(m,'_relay_channels',{'cam1':'m0'}),patch.object(m,'_relay_missing',set()),patch.object(m,'_send_volume',return_value=True),patch.object(m,'switch_camera',return_value=(True,'ok')) as cut:
            self.assertTrue(m.set_mix('cam3',on=True)[0])
            cut.assert_called_once_with('cam2', automatic=True, expected_stop=m._stop_generation)

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
            cmd, channels, missing, warning, _, _ = m._relay_command('cam2',{'cam1':0.0})
        self.assertEqual((list(channels),missing),(['cam2'],{'cam1'}))
        self.assertIn('Cam 1 commentary unavailable',warning)
        self.assertEqual(cmd.count('-i'),2)  # camera + generated silence base
        with patch.object(m,'_probe',return_value={'video'}):
            cmd, channels, missing, warning, _, _ = m._relay_command('cam1',{'cam1':0.0})
        self.assertEqual(channels,{})
        self.assertIn('broadcasting silence',warning)
        self.assertIn('anullsrc=r=48000:cl=stereo',cmd)

    def test_mix_graph_uses_each_source_once_with_gain(self):
        with patch.object(m,'_probe',return_value={'video','audio'}),patch.object(m,'_usb_device_available',return_value=True):
            cmd, channels, missing, _, _, _ = m._relay_command('cam2',{'cam2':-6.0,'cam1':0.0,'usb:alsa_input.mic':-60.0})
        graph=cmd[cmd.index('-filter_complex')+1]
        self.assertEqual(channels,{'cam2':'m0','cam1':'m1','usb:alsa_input.mic':'m2'})
        self.assertIn('[0:a:0]',graph); self.assertIn('[1:a:0]',graph); self.assertIn('[2:a:0]',graph)
        self.assertIn('[3:a:0][m0][m1][m2]amix=inputs=4',graph)  # silence base first
        self.assertIn('volume@m0=0.5012',graph)
        self.assertIn('alimiter',graph); self.assertIn('azmq=bind_address=tcp',graph)
        self.assertEqual(cmd[cmd.index('-f',cmd.index('alsa_input.mic')-3)+1],'pulse')

    def test_missing_usb_mic_degrades(self):
        with patch.object(m,'_probe',return_value={'video','audio'}),patch.object(m,'_usb_device_available',return_value=False):
            _, channels, _, warning, _, _ = m._relay_command('cam2',{'usb:hw:1,0':0.0})
        self.assertEqual(list(channels),['cam2'])
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
            cmd,_,_,_,_,_=m._relay_command('cam2',{'cam2':0.0})
            self.assertIn('scale=1920:1080',' '.join(cmd))
            self.assertEqual(cmd[cmd.index('-b:v')+1],'8000k')
            with patch.object(m,'Process') as proc,patch.object(m,'ensure_frame'),patch.object(m,'_youtube_stream_key','key'):
                m._launch_encoder()
            enc=proc.call_args.args[0]
            self.assertEqual(enc[enc.index('-f')+1], 'image2')
            vf=enc[enc.index('-filter_complex')+1]
            self.assertIn('scale=1920:1080',vf); self.assertIn('fontsize=42',vf)
            self.assertEqual((enc[enc.index('-b:v')+1],enc[enc.index('-bufsize')+1]),('6000k','12000k'))

    def test_profile_change_rebuilds_relay_before_live(self):
        with patch.object(m,'_want_stream',False),patch.object(m.shutil,'which',return_value='/bin/tool'),patch.object(m,'get_mediamtx_running',return_value=True),patch.object(m,'get_active_camera',return_value='cam1'),patch.object(m,'_active_camera','cam1'),patch.object(m,'_relay_profile','720p'),patch.object(m,'_profile','720p'),patch.object(m,'switch_camera',return_value=(True,'ok')) as cut,patch.object(m,'_launch_encoder'),patch.object(m.Path,'is_file',return_value=True):
            self.assertTrue(m.start_youtube_stream('key','1080p')[0])
            cut.assert_called_once_with('cam1', expected_stop=m._stop_generation)
            m.stop_youtube_stream()

    def test_meter_parses_levels_and_silence(self):
        meter=object.__new__(m.Meter)
        meter.peak=meter.rms=None; meter.updated=0.0
        meter._read(io.StringIO('frame:0 pts:0\nlavfi.astats.Overall.Peak_level=-12.04\nlavfi.astats.Overall.RMS_level=-inf\n'),True)
        self.assertEqual((meter.peak,meter.rms),(-12.0,-120.0))
        self.assertGreater(meter.updated,0)

    def test_meters_follow_sources_and_flag_missing_audio(self):
        started=[]
        class FakeMeter:
            def __init__(self,cmd): self.cmd=cmd; self.updated=0.0; self.exit=None; started.append(self)
            def poll(self): return self.exit
        with patch.object(m,'Meter',FakeMeter),patch.object(m,'_kill_process'),patch.object(m,'_meters',{}),patch.object(m,'_meter_retry_at',{}),patch.object(m,'_meter_no_audio',set()),patch.object(m,'_mic_sources',['alsa_input.usb-mic']),patch.object(m,'_camera_status',{'cam1':True,'cam2':False,'cam3':False,'program':True}):
            m._supervise_meters(100)
            self.assertEqual(sorted(m._meters),['cam1','program','usb:alsa_input.usb-mic'])
            self.assertIn('-allowed_media_types',m._meters['cam1'].cmd)
            self.assertEqual(m._meters['usb:alsa_input.usb-mic'].cmd[m._meters['usb:alsa_input.usb-mic'].cmd.index('-f')+1],'pulse')
            m._meters['cam1'].exit=1  # camera stream without an audio track
            m._supervise_meters(101)
            self.assertEqual(m.get_audio_levels()['cam1'],'no audio')
            self.assertNotIn('cam1',m._meters)  # retried later, not every tick
            m._supervise_meters(102)
            self.assertNotIn('cam1',m._meters)

    def test_log_redaction(self):
        proc=object.__new__(m.Process)
        proc.secret='SECRET';proc.error=''
        proc._read(io.StringIO('failed rtmp://host/SECRET\n'),False)
        self.assertNotIn('SECRET',proc.error)

class LayoutTests(unittest.TestCase):
    def test_pip_reuses_secondary_for_audio(self):
        layout=dict(mode='pip',main='cam1',secondary='cam2',position='top-right')
        with patch.object(m,'_layout',layout),patch.object(m,'_probe',return_value={'video','audio'}):
            cmd, channels, _, _, secondary, warning=m._relay_command('cam1',{'cam2':0.0})
        self.assertEqual(cmd.count(m.RTSP_BASE+'/cam2'),1)
        self.assertEqual(secondary,'cam2')
        self.assertFalse(warning)
        graph=cmd[cmd.index('-filter_complex')+1]
        self.assertIn('overlay=x=W-w-16:y=16',graph)
        self.assertIn('[1:a:0]',graph)
        self.assertEqual(list(channels),['cam2'])

    def test_offline_second_camera_keeps_main_video(self):
        def probe(cam):
            if cam=='cam2': raise ValueError('offline')
            return {'video','audio'}
        with patch.object(m,'_layout',dict(mode='split',main='cam1',secondary='cam2',position='top-right')),patch.object(m,'_probe',side_effect=probe):
            cmd,_,_,_,secondary,warning=m._relay_command('cam1',{'cam1':0})
        self.assertIsNone(secondary)
        self.assertIn('main camera only',warning)
        self.assertNotIn('hstack',' '.join(cmd))
        self.assertIn('[vout]',' '.join(cmd))

    def test_stop_cancels_queued_layout_change(self):
        entered, gate=threading.Event(),threading.Lock()
        class WaitingLock:
            def __enter__(self): entered.set(); gate.acquire()
            def __exit__(self,*args): gate.release()
        gate.acquire()
        results=[]
        previous=dict(m._layout)
        with patch.object(m,'_switch_lock',WaitingLock()),patch.object(m,'_generation',0),patch.object(m,'_pending_camera',None),patch.object(m,'_stop_generation',0),patch.object(m,'_replace_relay') as replace:
            worker=threading.Thread(target=lambda:results.append(m.set_layout('pip','cam1','cam2')))
            worker.start()
            try:
                self.assertTrue(entered.wait(2))
                # STOP invalidates pending work without waiting on the composition lock.
                with m._lock:
                    m._stop_generation+=1; m._generation+=1; m._pending_camera=None
            finally:
                gate.release(); worker.join(3)
            self.assertEqual(results,[(False,m.SUPERSEDED)])
            replace.assert_not_called()
            self.assertEqual(m._layout,previous)

    def test_invalid_pair_does_not_change_layout_or_relay(self):
        before=dict(m._layout)
        with patch.object(m,'_replace_relay') as replace:
            self.assertFalse(m.set_layout('pip','cam1','cam1')[0])
            self.assertFalse(m.set_layout('bad','cam1','cam2')[0])
            self.assertFalse(m.set_layout('pip','cam1','cam2','bad')[0])
            replace.assert_not_called()
        self.assertEqual(m._layout,before)

    def test_failed_layout_restores_previous_configuration(self):
        previous=dict(mode='single',main='cam1',secondary='cam2',position='top-right')
        with patch.object(m,'_layout',previous),patch.object(m,'_relay_proc',MagicMock()),patch.object(m,'_replace_relay',return_value=(False,'No video')),patch.object(m,'_generation',0),patch.object(m,'_pending_camera',None):
            self.assertFalse(m.set_layout('split','cam1','cam2')[0])
            self.assertEqual(m._layout,previous)

    def test_offline_main_can_be_configured_before_cameras_connect(self):
        with patch.object(m,'_layout',dict(m._layout)),patch.object(m,'_relay_proc',None),patch.object(m,'_camera_status',{}),patch.object(m,'_active_camera',None),patch.object(m,'_generation',0),patch.object(m,'_pending_camera',None):
            self.assertTrue(m.set_layout('pip','cam3','cam2')[0])
            self.assertEqual(m._layout['main'],'cam3')
            self.assertEqual(m._active_camera,'cam3')


class ConcurrencyRegressionTests(unittest.TestCase):
    def setUp(self):
        from contextlib import ExitStack
        self.stack = self.enterContext(ExitStack())
        values = dict(_want_stream=False, _state='stopped', _active_camera=None,
                      _relay_proc=None, _youtube_proc=None, _relay_channels={}, _relay_muted=set(),
                      _relay_missing=set(), _audio_warning='', _generation=0, _stop_generation=0,
                      _pending_camera=None, _follow=False, _profile='720p', _relay_profile='720p',
                      _mix={c:{'on':c=='cam1','gain':0.0} for c in m.CAMERA_IDS})
        for key, value in values.items():
            self.stack.enter_context(patch.object(m, key, value))
        self.stack.enter_context(patch.object(m, 'get_mediamtx_running', return_value=True))
        self.stack.enter_context(patch.object(m, '_kill_process'))
        self.stack.enter_context(patch.object(m.Path, 'is_file', return_value=True))
        self.stack.enter_context(patch.object(m.shutil, 'which', return_value='/mock'))

    def test_stop_cancels_start_waiting_for_camera_readiness(self):
        entered, release = threading.Event(), threading.Event()
        def paths():
            entered.set()
            release.wait(3)
            return {'cam1':True}
        results = []
        with patch.object(m, '_paths', side_effect=paths), patch.object(m, 'switch_camera') as cut, patch.object(m, '_launch_encoder') as launch:
            worker = threading.Thread(target=lambda: results.append(m.start_youtube_stream('key')))
            worker.start()
            try:
                self.assertTrue(entered.wait(2))
                self.assertFalse(m.start_youtube_stream('duplicate')[0])
                self.assertTrue(m.stop_youtube_stream()[0])
            finally:
                release.set()
                worker.join(3)
            self.assertEqual(results, [(False, m.SUPERSEDED)])
            cut.assert_not_called()
            launch.assert_not_called()
            self.assertFalse(m._want_stream)
            self.assertEqual(m._state, 'stopped')

    def test_stop_after_camera_selection_prevents_encoder_launch(self):
        def selected(*args, **kwargs):
            m.stop_youtube_stream()
            return True, 'selected'
        with patch.object(m, '_paths', return_value={'cam1':True}), patch.object(m, 'switch_camera', side_effect=selected), patch.object(m, '_launch_encoder') as launch:
            self.assertEqual(m.start_youtube_stream('key'), (False, m.SUPERSEDED))
            launch.assert_not_called()
            self.assertEqual(m._state, 'stopped')

    def test_cancelled_start_cannot_register_a_new_cut(self):
        m.stop_youtube_stream()
        with patch.object(m, '_replace_relay') as replace:
            self.assertEqual(m.switch_camera('cam1', expected_stop=0), (False, m.SUPERSEDED))
            replace.assert_not_called()

    def test_follow_change_waits_for_cut_and_applies_to_new_camera(self):
        entered, release, audio_entered = threading.Event(), threading.Event(), threading.Event()
        relay = MagicMock(); relay.poll.return_value=None; relay.healthy.return_value=True
        m._relay_proc=relay; m._active_camera='cam1'; m._relay_channels={'cam1':'m0'}
        built=[]; results=[]
        def command(cam, wanted):
            built.append((cam, dict(wanted)))
            if len(built)==1:
                entered.set(); release.wait(3)
            return ['mock'], {c:f'm{i}' for i,c in enumerate(wanted)}, set(), '', None, ''
        def audio():
            audio_entered.set()
            results.append(m.set_audio_source('active'))
        with patch.object(m,'_relay_command',side_effect=command), patch.object(m,'Process',return_value=relay), patch.object(m,'_wait_progress',return_value=True):
            cut=threading.Thread(target=lambda: results.append(m.switch_camera('cam2')))
            cut.start()
            self.assertTrue(entered.wait(2))
            change=threading.Thread(target=audio); change.start()
            try:
                self.assertTrue(audio_entered.wait(2))
                self.assertEqual(m.get_audio_source(),'cam1')
            finally:
                release.set(); cut.join(3); change.join(3)
            self.assertEqual(len(results),2)
            self.assertTrue(all(r[0] for r in results), results)
            self.assertEqual(built[-1],('cam2',{'cam2':0.0}))
            self.assertEqual(m.get_stream_status()['audio_on_air'],['cam2'])

    def test_muted_channel_removed_and_restored_in_status(self):
        relay=MagicMock(); relay.poll.return_value=None; relay.healthy.return_value=True
        m._relay_proc=relay; m._active_camera='cam1'; m._relay_channels={'cam1':'m0'}
        with patch.object(m,'_send_volume',return_value=True), patch.object(m,'switch_camera') as cut:
            self.assertTrue(m.set_mix('cam1',on=False)[0])
            self.assertEqual(m.get_stream_status()['audio_on_air'],[])
            self.assertTrue(m.set_mix('cam1',on=True)[0])
            self.assertEqual(m.get_stream_status()['audio_on_air'],['cam1'])
            cut.assert_not_called()

    def test_mixer_rebuild_preserves_newer_operator_cut_generation(self):
        m._active_camera='cam1'
        m._generation=12
        m._pending_camera='cam3'
        with patch.object(m, '_replace_relay', return_value=(True,'selected')) as replace:
            self.assertTrue(m.set_audio_source('active')[0])
            self.assertEqual(m._generation,12)
            self.assertEqual(m._pending_camera,'cam3')
            replace.assert_called_once_with('cam1',12)

    def test_encoder_recovery_does_not_run_relay_probes(self):
        m._want_stream=True
        with patch.object(m,'_relay_command',side_effect=AssertionError('slow probe')), patch.object(m,'_launch_encoder') as launch, patch.object(m,'_retry_at',0):
            m._supervise_encoder()
            launch.assert_called_once()


if __name__=='__main__':
    unittest.main()

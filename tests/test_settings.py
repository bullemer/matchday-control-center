import base64
import copy
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app as a
import media_engine as m
import recordings
import settings_store as st


class SettingsStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = patch.object(st, 'PATH', Path(self.tmp.name) / 'settings.json')
        p.start(); self.addCleanup(p.stop)
        st.load()
        self.addCleanup(lambda: st._values.update(st.DEFAULTS))

    def test_defaults_validation_and_persistence(self):
        self.assertEqual(st.get('home_name'), 'Home')
        for bad in ({'camera_password': 'short'}, {'camera_password': 'has:colon1'}, {'half_length': 5},
                    {'half_length': '45'}, {'profile': '4k'}, {'recordings_dir': 'relative/path'},
                    {'min_free_gb': 0}, {'home_name': ''}, {'nonsense': 1}, {}):
            with self.assertRaises(ValueError, msg=bad):
                st.update(bad)
        st.update({'home_name': ' Polonia ', 'half_length': 40, 'camera_password': 'Secret_123'})
        self.assertEqual(oct(st.PATH.stat().st_mode & 0o777), '0o600')
        st._values.update(st.DEFAULTS)
        self.assertEqual((st.load()['home_name'], st.get('half_length')), ('Polonia', 40))

    def test_broken_file_values_fall_back_to_defaults(self):
        st.PATH.write_text('{"half_length": 999, "profile": "720p", "home_name": "FC"}')
        values = st.load()
        self.assertEqual((values['half_length'], values['home_name']), (45, 'FC'))


class SettingsApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        tmp = Path(self.tmp.name)
        for target, name, value in [(st, 'PATH', tmp / 'settings.json'), (a, 'RUNTIME_DIR', tmp),
                                    (a, 'MATCH_PATH', tmp / 'match.json'), (a, 'SCOREBOARD_PATH', str(tmp / 'sb.txt')),
                                    (recordings, 'RECORDINGS_DIR', tmp / 'rec'), (recordings, 'MIN_FREE_START', recordings.MIN_FREE_START)]:
            p = patch.object(target, name, value); p.start(); self.addCleanup(p.stop)
        st.load()
        self.addCleanup(lambda: st._values.update(st.DEFAULTS))
        old_score, old_clock = copy.deepcopy(a._scoreboard), copy.deepcopy(a._clock)
        self.addCleanup(lambda: a._scoreboard.update(old_score))
        self.addCleanup(lambda: a._clock.update(old_clock))
        old_user = a.app.config['OPERATOR_USER']
        self.addCleanup(lambda: a.app.config.update(OPERATOR_USER=old_user))
        a.app.config.update(OPERATOR_PASSWORD='test', OPERATOR_USER='operator')
        env = patch.dict(os.environ)
        env.start(); self.addCleanup(env.stop)
        for key in ('MCC_CAMERA_PASSWORD', 'MCC_RECORDINGS_DIR', 'MCC_OPERATOR_PASSWORD', 'MCC_OPERATOR_USER'):
            os.environ.pop(key, None)
        self.client = a.app.test_client()

    def auth(self, user='operator', password='test'):
        return {'Authorization': 'Basic ' + base64.b64encode(f'{user}:{password}'.encode()).decode()}

    def post(self, url, body, **kw):
        return self.client.post(url, json=body, headers=kw.get('headers', self.auth()))

    def test_get_needs_login_and_shows_everything(self):
        self.assertEqual(self.client.get('/api/settings').status_code, 401)
        d = self.client.get('/api/settings', headers=self.auth()).get_json()
        self.assertEqual(d['settings']['home_name'], 'Home')
        self.assertEqual(d['login'], {'user': 'operator', 'locked': False})
        self.assertFalse(d['camera_password_locked'])
        self.assertIn('free_gb', d['recordings'])

    def test_match_defaults_and_new_match(self):
        self.assertEqual(self.post('/api/settings', {'home_name': 'Polonia', 'away_name': 'Gäste', 'half_length': 35}).status_code, 200)
        a._scoreboard.update(home_name='X', away_name='Y', home_score=2, away_score=1)
        a._clock.update(elapsed=1234.0, half=2)
        with patch.object(a, 'get_stream_status', return_value={'active': True}):
            self.assertEqual(self.post('/api/settings/new-match', {}).status_code, 400)  # not while live
        with patch.object(a, 'get_stream_status', return_value={'active': False}), \
                patch.object(a, 'get_recording_status', return_value={'active': False}):
            r = self.post('/api/settings/new-match', {})
        self.assertEqual(r.get_json()['message'], 'New match: Polonia 0:0 Gäste')
        self.assertEqual((a._scoreboard['home_score'], a._clock['elapsed'], a._clock['half'], a._clock['half_length']), (0, 0.0, 1, 35))
        # clearing a team name falls back to the default from the settings
        self.post('/api/scoreboard/update', {'home_name': ''})
        self.assertEqual(a._scoreboard['home_name'], 'Polonia')
        def profile_select():
            page = self.client.get('/', headers=self.auth()).get_data(as_text=True)
            return page.split('id="quality-profile"')[1].split('</select>')[0]
        self.assertIn('<option value="720p" selected>', profile_select())
        self.post('/api/settings', {'profile': '1080p'})
        self.assertIn('<option value="1080p" selected>', profile_select())
        self.assertNotIn('<option value="720p" selected>', profile_select())

    def test_camera_password_reaches_links_and_mediamtx(self):
        with patch.object(a, 'get_mediamtx_running', return_value=True), patch.object(a, 'generate_mediamtx_config') as gen:
            self.assertEqual(self.post('/api/settings', {'camera_password': 'Club-Cam-2026'}).status_code, 200)
            gen.assert_called_once()
        self.assertEqual(m.camera_password(), 'Club-Cam-2026')
        cam1 = next(c for c in m.get_cameras() if c['id'] == 'cam1')
        self.assertEqual(cam1['srt_stream_id'], 'publish:cam1:camera:Club-Cam-2026')
        self.post('/api/settings', {'camera_password': ''})
        self.assertEqual(m.camera_password(), '')
        os.environ['MCC_CAMERA_PASSWORD'] = 'from-env-123'
        self.assertEqual(m.camera_password(), 'from-env-123')
        self.assertEqual(self.post('/api/settings', {'camera_password': 'Other-1234'}).status_code, 400)

    def test_recording_storage(self):
        target = Path(self.tmp.name) / 'usb' / 'matches'
        r = self.post('/api/settings', {'recordings_dir': str(target), 'min_free_gb': 10})
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertTrue(target.is_dir())
        self.assertEqual((recordings.RECORDINGS_DIR, recordings.MIN_FREE_START), (target, 10 * recordings.GB))
        self.assertEqual(r.get_json()['recordings']['dir'], str(target))
        self.assertEqual(self.post('/api/settings', {'recordings_dir': '/proc/not-writable'}).status_code, 400)
        self.assertEqual(st.get('recordings_dir'), str(target))   # a failed change saves nothing
        with patch.object(a, 'get_recording_status', return_value={'active': True}):
            self.assertEqual(self.post('/api/settings', {'min_free_gb': 5}).status_code, 400)

    def test_login_change(self):
        self.assertEqual(self.post('/api/settings/login', {'current_password': 'wrong', 'new_password': 'longenough'}).status_code, 403)
        self.assertEqual(self.post('/api/settings/login', {'current_password': 'test', 'new_password': 'short'}).status_code, 400)
        self.assertEqual(self.post('/api/settings/login', {'current_password': 'test', 'user': 'bad name', 'new_password': 'longenough'}).status_code, 400)
        r = self.post('/api/settings/login', {'current_password': 'test', 'user': 'admin', 'new_password': 'NeuesPasswort1'})
        self.assertEqual(r.status_code, 200)
        self.assertEqual((Path(self.tmp.name) / 'operator-password').read_text(), 'NeuesPasswort1')
        self.assertEqual(oct((Path(self.tmp.name) / 'operator-password').stat().st_mode & 0o777), '0o600')
        self.assertEqual(self.client.get('/api/settings', headers=self.auth()).status_code, 401)   # old login
        self.assertEqual(self.client.get('/api/settings', headers=self.auth('admin', 'NeuesPasswort1')).status_code, 200)
        os.environ['MCC_OPERATOR_PASSWORD'] = 'env'
        r = self.post('/api/settings/login', {'current_password': 'NeuesPasswort1', 'new_password': 'whatever123'},
                      headers=self.auth('admin', 'NeuesPasswort1'))
        self.assertEqual(r.status_code, 400)


if __name__ == '__main__':
    unittest.main()

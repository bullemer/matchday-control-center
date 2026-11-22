import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import camera_control as cc


REPORT = {'battery': 78, 'charging': False, 'temp_c': 36.5, 'thermal': 'none', 'zoom': 0.6,
          'zoom_min': 0.6, 'zoom_max': 10.0, 'remote_zoom': True, 'mode': 'fixed', 'app': 'chameleon-stream 0.7.0'}


class CameraControlTests(unittest.TestCase):
    def setUp(self):
        cc.reset()

    def test_report_is_cleaned_and_shown(self):
        answer = cc.record_report('cam3', dict(REPORT))
        self.assertEqual(answer, {'zoom': None, 'zoom_seq': cc._SEQ_BASE})
        r = cc.get_reports()['cam3']
        self.assertEqual((r['battery'], r['temp_c'], r['mode'], r['warning']), (78, 36.5, 'fixed', ''))
        for bad in ({'battery': 150}, {'temp_c': 'hot'}, {'thermal': 'boiling'}, {'charging': 'yes'},
                    {'zoom_min': 5, 'zoom_max': 2}, 'text'):
            with self.assertRaises(ValueError):
                cc.clean_report(bad)

    def test_warnings(self):
        cc.record_report('cam1', dict(REPORT, battery=15, thermal='severe'))
        w = cc.get_reports()['cam1']['warning']
        self.assertIn('overheating', w)
        self.assertIn('battery low', w)
        cc.record_report('cam1', dict(REPORT, battery=15, charging=True, temp_c=44))
        w = cc.get_reports()['cam1']['warning']
        self.assertNotIn('battery', w)
        self.assertIn('getting hot', w)

    def test_request_numbers_grow_across_restarts(self):
        self.assertAlmostEqual(cc._SEQ_BASE, time.time() - 1_700_000_000, delta=3600)  # time based, not 0
        self.assertLess(cc._SEQ_BASE + 10_000, 2**31 - 1)    # fits a Kotlin Int

    def test_stale_reports_disappear(self):
        cc.record_report('cam2', dict(REPORT))
        with patch.object(cc.time, 'time', return_value=time.time() + cc.REPORT_STALE + 1):
            self.assertNotIn('cam2', cc.get_reports())

    def test_zoom_requests_steps_clamp_and_wide(self):
        with self.assertRaises(ValueError):
            cc.request_zoom('cam3', step=1)  # no report yet
        cc.record_report('cam3', dict(REPORT, zoom=1.0))
        self.assertEqual(cc.request_zoom('cam3', step=1), 1.25)
        self.assertEqual(cc.request_zoom('cam3', step=1), 1.56)   # builds on the request just made
        self.assertEqual(cc.request_zoom('cam3', zoom=50), 10.0)  # clamped to the phone's range
        self.assertEqual(cc.request_zoom('cam3', wide=True), 0.6)
        with self.assertRaises(ValueError):
            cc.request_zoom('cam3', step=2)
        self.assertEqual(cc.record_report('cam3', dict(REPORT)), {'zoom': 0.6, 'zoom_seq': cc._SEQ_BASE + 4})
        cc.record_report('cam4', dict(REPORT, remote_zoom=False))
        with self.assertRaises(ValueError):
            cc.request_zoom('cam4', step=1)

    def test_long_poll_answers_at_once_on_zoom_and_times_out_otherwise(self):
        cc.record_report('cam3', dict(REPORT, zoom=1.0))
        t0 = time.monotonic()
        b = cc._SEQ_BASE
        self.assertEqual(cc.record_report('cam3', dict(REPORT, wait=True, zoom_seq=b), hold=0.3)['zoom_seq'], b)
        self.assertGreaterEqual(time.monotonic() - t0, 0.25)  # nothing new: held until timeout
        result = {}
        thread = threading.Thread(target=lambda: result.update(cc.record_report('cam3', dict(REPORT, wait=True, zoom_seq=b), hold=5)))
        thread.start(); time.sleep(0.2)
        t1 = time.monotonic()
        cc.request_zoom('cam3', zoom=2.0)
        thread.join(2)
        self.assertLess(time.monotonic() - t1, 1.0)  # woke up immediately
        self.assertEqual(result, {'zoom': 2.0, 'zoom_seq': b + 1})
        # a phone that already has the newest request gets nothing new without waiting forever
        self.assertEqual(cc.record_report('cam3', dict(REPORT, wait=True, zoom_seq=b + 1), hold=0.1)['zoom_seq'], b + 1)


class CameraReportApiTests(unittest.TestCase):
    def setUp(self):
        import app as a
        self.a = a
        cc.reset()
        a.app.config['OPERATOR_PASSWORD'] = 'pw'
        self.client = a.app.test_client()

    def test_report_needs_no_login_but_zoom_does(self):
        patcher = patch.object(self.a, 'camera_password', return_value='')
        patcher.start(); self.addCleanup(patcher.stop)
        self.a.app.config['OPERATOR_USER'] = 'operator'
        r = self.client.post('/api/camera-report/cam3', json=dict(REPORT))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()['zoom_seq'], cc._SEQ_BASE)
        self.assertEqual(self.client.post('/api/camera-report/cam9', json=dict(REPORT)).status_code, 404)
        self.assertEqual(self.client.post('/api/camera-report/cam3', json={'battery': 500}).status_code, 400)
        self.assertEqual(self.client.post('/api/cameras/cam3/zoom', json={'step': 1}).status_code, 401)
        auth = {'Authorization': 'Basic ' + __import__('base64').b64encode(b'operator:pw').decode()}
        r = self.client.post('/api/cameras/cam3/zoom', json={'step': 1}, headers=auth)
        self.assertEqual((r.status_code, r.get_json()['zoom']), (200, 0.75))
        status = self.client.get('/api/status', headers=auth).get_json()
        self.assertEqual(status['camera_reports']['cam3']['zoom_target'], 0.75)

    def test_camera_password_is_required_when_set(self):
        with patch.object(self.a, 'camera_password', return_value='secret'):
            self.assertEqual(self.client.post('/api/camera-report/cam3', json=dict(REPORT)).status_code, 401)
            r = self.client.post('/api/camera-report/cam3', json=dict(REPORT), headers={'X-Camera-Password': 'secret'})
            self.assertEqual(r.status_code, 200)


if __name__ == '__main__':
    unittest.main()

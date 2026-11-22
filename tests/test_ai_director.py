import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import director_scoring as ds
import ai_director as ad

W, H = 1280, 720
GOOD = ds.Picture(sharpness=200, brightness=120, clipped=0.0, change=5.0)


def person(x, y, h=80):
    return ds.Detection(x, y, h * 0.4, h, 0.8)


def ball(x, y, size=16, conf=0.6):
    return ds.Detection(x, y, size, size, conf)


class ScoringTests(unittest.TestCase):
    def test_ball_in_the_middle_with_players_beats_empty_picture(self):
        busy, empty = ds.CameraState(), ds.CameraState()
        players = [person(600 + i * 30, 380) for i in range(5)]
        a = ds.rate(busy, 0.0, W, H, players, [ball(640, 400)], GOOD)
        b = ds.rate(empty, 0.0, W, H, [], [], GOOD)
        self.assertGreater(a["score"], 80)
        self.assertEqual(b["score"], 20.0)  # only the picture quality counts
        self.assertTrue(a["ball"]["seen"])
        self.assertEqual(a["action"]["near"], 5)

    def test_ball_at_the_edge_scores_lower_than_central(self):
        s1, s2 = ds.CameraState(), ds.CameraState()
        central = ds.rate(s1, 0, W, H, [], [ball(640, 360)], GOOD)["raw"]
        edge = ds.rate(s2, 0, W, H, [], [ball(20, 360)], GOOD)["raw"]
        self.assertGreater(central, edge + 10)

    def test_weak_and_tiny_detections_are_ignored(self):
        s = ds.CameraState()
        r = ds.rate(s, 0, W, H, [person(600, 300, h=5)], [ball(640, 360, conf=0.1)], GOOD)
        self.assertFalse(r["ball"]["seen"])
        self.assertEqual(r["action"]["players"], 0)

    def test_ball_is_remembered_briefly(self):
        s = ds.CameraState()
        ds.rate(s, 0.0, W, H, [], [ball(640, 360)], GOOD)
        r = ds.rate(s, 1.0, W, H, [], [], GOOD)
        self.assertTrue(r["ball"]["remembered"])
        self.assertGreater(r["ball"]["part"], 0)
        r = ds.rate(s, 2.0, W, H, [], [], GOOD)
        self.assertEqual(r["ball"]["part"], 0)

    def test_group_of_players_counts_without_ball(self):
        s = ds.CameraState()
        r = ds.rate(s, 0, W, H, [person(600 + i * 20, 400) for i in range(4)] + [person(50, 400)], [], GOOD)
        self.assertEqual(r["action"]["near"], 4)
        self.assertGreater(r["action"]["part"], 0.5)

    def test_frozen_picture_scores_zero(self):
        s = ds.CameraState()
        still = ds.Picture(sharpness=200, brightness=120, change=0.1)
        players = [person(640, 380)] * 4
        ds.rate(s, 0.0, W, H, players, [ball(640, 360)], still)
        r = ds.rate(s, 2.5, W, H, players, [ball(640, 360)], still)
        self.assertTrue(r["quality"]["frozen"])
        self.assertEqual(r["raw"], 0.0)

    def test_dark_or_blurred_picture_lowers_quality(self):
        q_dark, _ = ds.quality_part(ds.Picture(sharpness=200, brightness=20), False)
        q_blur, _ = ds.quality_part(ds.Picture(sharpness=10, brightness=120), False)
        self.assertLess(q_dark, 0.8)
        self.assertLess(q_blur, 0.5)

    def test_score_is_smoothed(self):
        s = ds.CameraState()
        ds.rate(s, 0.0, W, H, [], [], GOOD)                       # 20
        r = ds.rate(s, 0.2, W, H, [person(640, 380)] * 5, [ball(640, 360)], GOOD)
        self.assertLess(r["score"], r["raw"])                     # moves towards it gradually
        for t in (1, 2, 3, 4, 6):
            r = ds.rate(s, t, W, H, [person(640, 380)] * 5, [ball(640, 360)], GOOD)
        self.assertAlmostEqual(r["score"], r["raw"], delta=3)


class RecommenderTests(unittest.TestCase):
    def test_hysteresis(self):
        r = ds.Recommender()
        self.assertEqual(r.update(0, {"cam1": 50, "cam2": 40}), "cam1")
        self.assertEqual(r.update(1, {"cam1": 50, "cam2": 55}), "cam1")    # not enough lead
        self.assertEqual(r.update(2, {"cam1": 50, "cam2": 70}), "cam1")    # lead, but not long enough
        self.assertEqual(r.update(3, {"cam1": 50, "cam2": 70}), "cam1")
        self.assertEqual(r.update(4.1, {"cam1": 50, "cam2": 70}), "cam2")  # 2 s lead
        self.assertEqual(r.update(5, {"cam1": 90}), "cam1")                # cam2 gone
        self.assertIsNone(r.update(6, {}))


class ManagerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.patches = [patch.object(ad, 'SETTINGS', base / 'ai-director.json'), patch.object(ad, 'OUT', base / 'out')]
        for p in self.patches:
            p.start()
        ad._settings.update(enabled=False, fps=5)

    def tearDown(self):
        ad.stop()
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def test_setup_problem_blocks_enabling(self):
        with patch.object(ad, 'VENV_PYTHON', Path('/nonexistent/python')):
            self.assertIn('setup-ai-director.sh', ad.setup_problem())
            with self.assertRaises(ValueError):
                ad.configure(enabled=True)
            self.assertEqual(ad.status()['phase'], 'off')

    def test_validation_and_settings_persist(self):
        with self.assertRaises(ValueError):
            ad.configure(fps=7)
        with self.assertRaises(ValueError):
            ad.configure(enabled='yes')
        ad.configure(fps=10)
        self.assertEqual(json.loads(ad.SETTINGS.read_text()), {'enabled': False, 'fps': 10})
        ad._settings['fps'] = 5
        self.assertEqual(ad.load_settings()['fps'], 10)

    def test_status_reads_fresh_worker_state_only(self):
        ad.OUT.mkdir(parents=True)
        state = {'updated': time.time(), 'phase': 'running', 'gpu': 'GPU', 'busy': 0.2, 'recommended': 'cam2',
                 'cams': {'cam2': {'online': True, 'score': 70}}}
        (ad.OUT / 'state.json').write_text(json.dumps(state))
        class Alive:
            error = ''
            def poll(self): return None
        with patch.object(ad, '_proc', Alive()):
            s = ad.status()
            self.assertEqual((s['phase'], s['recommended'], s['cams']['cam2']['score']), ('running', 'cam2', 70))
            state['updated'] -= 60
            (ad.OUT / 'state.json').write_text(json.dumps(state))
            self.assertEqual(ad.status()['phase'], 'starting')   # stale file: not trusted
        self.assertEqual(ad.status()['cams'], {})

    def test_images_only_for_match_cameras(self):
        ad.OUT.mkdir(parents=True)
        (ad.OUT / 'cam1.jpg').write_bytes(b'jpg')
        self.assertIsNotNone(ad.image_path('cam1'))
        self.assertIsNone(ad.image_path('cam5'))
        self.assertIsNone(ad.image_path('../secret'))

    def test_supervisor_restarts_a_crashed_worker(self):
        started = []
        class Dead:
            error = 'CUDA error'
            proc = type('P', (), {'stdin': None})()
            def poll(self): return 1
        def fake_start():
            started.append(1)
            ad._proc = Dead()
        with patch.object(ad, 'setup_problem', return_value=None), patch.object(ad, '_start_worker', side_effect=fake_start), \
                patch.object(ad, "RESTART_MAX_S", 0.2):
            ad._settings['enabled'] = True
            ad.start()
            time.sleep(4.5)
            ad._settings['enabled'] = False
            ad.stop()
        self.assertGreaterEqual(len(started), 2)
        self.assertIn('CUDA error', ad._last_error)

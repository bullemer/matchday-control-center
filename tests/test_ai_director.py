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


class FenceScoringTests(unittest.TestCase):
    PITCH = [(0.1, 0.4), (0.9, 0.4), (1.0, 0.95), (0.0, 0.95)]   # trapezoid, lower part of the picture

    def test_point_in_polygon(self):
        square = [(0, 0), (10, 0), (10, 10), (0, 10)]
        self.assertTrue(ds.inside(square, 5, 5))
        self.assertFalse(ds.inside(square, 15, 5))
        self.assertFalse(ds.inside(square, 5, -1))
        self.assertIsNone(ds.fence_pixels([(0, 0), (1, 1)], W, H))     # fewer than 3 points: no fence

    def test_spectators_outside_the_fence_do_not_count(self):
        players = [person(640, 450 - 40) for _ in range(3)]            # feet at y=450 (on the pitch)
        stands = [person(200 + i * 80, 200 - 40) for i in range(6)]    # feet at y=200 (above the fence)
        no_fence = ds.rate(ds.CameraState(), 0, W, H, players + stands, [], GOOD)
        fenced = ds.rate(ds.CameraState(), 0, W, H, players + stands, [], GOOD, fence=self.PITCH)
        self.assertEqual(no_fence["action"]["players"], 9)
        self.assertEqual(fenced["action"]["players"], 3)
        self.assertEqual(fenced["action"]["off_pitch"], 6)
        self.assertTrue(fenced["fence"])
        self.assertFalse(no_fence["fence"])

    def test_ball_in_the_air_above_the_fence_counts_but_not_far_outside(self):
        high = ball(640, 0.4 * H - 0.1 * H)      # 10 % of the height above the far line: in the air
        stand = ball(640, 0.1 * H)               # in the stands
        self.assertTrue(ds.rate(ds.CameraState(), 0, W, H, [], [high], GOOD, fence=self.PITCH)["ball"]["seen"])
        self.assertFalse(ds.rate(ds.CameraState(), 0, W, H, [], [stand], GOOD, fence=self.PITCH)["ball"]["seen"])


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
        ad._settings.update(enabled=False, fps=5, suggest=False)
        ad._suggester = ds.Suggester()
        ad._suggestion = None
        ad._on_air.update(cam=None, since=0.0)
        for k in ad._stats:
            ad._stats[k] = 0

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
        self.assertEqual(json.loads(ad.SETTINGS.read_text()), {'enabled': False, 'fps': 10, 'suggest': False})
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

    def test_fences_validated_saved_and_only_for_fixed_cameras(self):
        square = [[0.1, 0.4], [0.9, 0.4], [1.0, 0.95], [0.0, 0.95]]
        with patch.object(ad, 'FENCES', ad.SETTINGS.parent / 'fences.json'), \
                patch.object(ad, '_mounts', return_value={'cam1': 'fixed', 'cam2': 'tracking'}):
            for bad in ([[0.1, 0.1], [0.2, 0.2]], [[0.1, 0.1]] * 17, [[0.1, 'x'], [0.2, 0.2], [0.3, 0.3]], [[2, 0], [0, 0], [0, 1]], 'abc'):
                with self.assertRaises(ValueError):
                    ad.set_fence('cam1', bad)
            with self.assertRaises(ValueError):
                ad.set_fence('cam5', square)                 # reporting cameras: no fence
            self.assertEqual(ad.set_fence('cam1', square), square)
            ad.set_fence('cam2', square)
            self.assertEqual(json.loads((ad.OUT / 'fences.json').read_text()), {'cam1': square})  # cam2 moves: not used
            ad._fences.clear()
            self.assertEqual(set(ad.load_fences()), {'cam1', 'cam2'})
            self.assertEqual(ad.set_fence('cam1', []), [])     # remove
            self.assertEqual(json.loads((ad.OUT / 'fences.json').read_text()), {})
            self.assertEqual(ad.status()['fences'], {'cam2': square})
            ad._fences.clear()

    def test_pitch_maps_saved_previewed_and_only_for_fixed_cameras(self):
        from tests.test_pitch_map import points
        import pitch_map as pm
        keys = ['box_l_f', 'box_l_n', 'half_f', 'half_n', 'centre']
        good = points(pm.clean_pitch({}), keys)
        with patch.object(ad, 'PITCH', ad.SETTINGS.parent / 'pitch.json'), \
                patch.object(ad, 'FENCES', ad.SETTINGS.parent / 'fences.json'), \
                patch.object(ad, '_mounts', return_value={'cam1': 'fixed', 'cam2': 'tracking'}):
            ad._pitch.update(pitch=dict(pm.DEFAULT_PITCH), cams={})
            self.assertEqual(ad.preview_pitch_map('cam1', good[:2]), {'ready': False, 'need': 2})
            pv = ad.preview_pitch_map('cam1', good)
            self.assertTrue(pv['ready'] and pv['checked'] and pv['lines'] and pv['view'])
            for bad in (good[:3], good + [dict(good[0])], [dict(good[0], mark='nope')] + good[1:], 'x'):
                with self.assertRaises(ValueError):
                    ad.set_pitch_map('cam1', bad)
            with self.assertRaises(ValueError):
                ad.set_pitch_map('cam5', good)
            ad.set_pitch_map('cam1', good)
            ad.set_pitch_map('cam2', good)
            worker = json.loads((ad.OUT / 'pitch.json').read_text())
            self.assertEqual(set(worker['cams']), {'cam1'})            # cam2 moves: not used
            st = ad.status()['pitch']
            self.assertTrue(st['maps']['cam1']['active'])
            self.assertFalse(st['maps']['cam2']['active'])
            self.assertEqual(len(st['landmarks']), len(pm.landmarks(pm.DEFAULT_PITCH)))
            # A smaller pitch: the clicked markings now mean other metres; maps are refitted.
            ad.set_pitch_size({'length': 90, 'width': 60})
            self.assertEqual(json.loads((ad.OUT / 'pitch.json').read_text())['pitch']['length'], 90)
            with self.assertRaises(ValueError):
                ad.set_pitch_size({'length': 500})
            ad._pitch['cams'] = {}
            self.assertEqual(set(ad.load_pitch()['maps']), {'cam1', 'cam2'})
            ad.set_pitch_map('cam1', [])
            self.assertEqual(set(json.loads((ad.OUT / 'pitch.json').read_text())['cams']), set())
            ad._pitch.update(pitch=dict(pm.DEFAULT_PITCH), cams={})

    def test_suggestions_from_worker_scores_and_on_air_camera(self):
        ad.OUT.mkdir(parents=True)
        def state(c1, c3):
            (ad.OUT / 'state.json').write_text(json.dumps({'updated': time.time(), 'phase': 'running', 'cams': {
                'cam1': {'online': True, 'score': c1}, 'cam3': {'online': True, 'score': c3}, 'cam5': {'online': True, 'score': 99}}}))
        stream = {'active_camera': 'cam1', 'replay': {'active': False}, 'layout': {'mode': 'single'}}
        ad._settings.update(enabled=True, suggest=True)
        with patch.object(ad.media_engine, 'get_stream_status', side_effect=lambda: stream):
            state(40, 74)
            for t in range(0, 12):
                ad._update_suggestion(now=1000 + t)
            sug = ad.suggestion()
            self.assertEqual((sug['cam'], sug['on_air']), ('cam3', 'cam1'))   # cam5 (reporter) never suggested
            stream['replay']['active'] = True                                 # replay: no suggestions
            ad._update_suggestion(now=1013)
            self.assertIsNone(ad.suggestion())
            stream['replay']['active'] = False
            for t in range(14, 30):
                ad._update_suggestion(now=1000 + t)
            self.assertEqual(ad.suggestion()['cam'], 'cam3')
            self.assertTrue(ad.ignore_suggestion())
            self.assertIsNone(ad.suggestion())
            ad._settings['suggest'] = False
            self.assertIsNone(ad._update_suggestion(now=1031))
        self.assertEqual((ad._stats['ignored'], ad._stats['lead lost']), (1, 1))
        log = (ad.OUT / 'suggestions.csv').read_text().splitlines()
        self.assertEqual(log[0], 'time,suggested,score,on_air,on_air_score,shown_s,outcome')
        self.assertTrue(log[-1].endswith(',ignored'))

    def test_snapshot_only_for_match_cameras(self):
        with self.assertRaises(ValueError):
            ad.snapshot('cam5')
        with patch.object(ad.subprocess, 'run', return_value=type('R', (), {'returncode': 1})()):
            with self.assertRaises(ValueError):
                ad.snapshot('cam1')

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


class SuggesterTests(unittest.TestCase):
    def run_for(self, s, start, seconds, scores, on_air, since, step=0.5, blocked=False):
        out = (None, [])
        events = []
        t = start
        while t <= start + seconds + 1e-9:
            out = s.update(t, scores, on_air, since, blocked)
            events += out[1]
            t += step
        return out[0], events

    def test_suggests_after_lead_hold_and_accepts_on_cut(self):
        s = ds.Suggester()
        scores = {"cam1": 40, "cam3": 74}
        cur, _ = self.run_for(s, 10, 1.5, scores, "cam1", since=0)
        self.assertIsNone(cur)                                   # lead not held for 2 s yet
        cur, _ = self.run_for(s, 12, 0.5, scores, "cam1", since=0)
        self.assertEqual((cur["cam"], cur["score"], cur["on_air_score"]), ("cam3", 74, 40))
        cur, events = s.update(13, scores, "cam3", 13)          # operator cut to cam3
        self.assertIsNone(cur)
        self.assertEqual(events[0][1], "accepted")

    def test_no_suggestion_right_after_a_cut_small_lead_low_score_or_blocked(self):
        self.assertIsNone(self.run_for(ds.Suggester(), 10, 5, {"cam1": 40, "cam3": 74}, "cam1", since=8)[0])  # on air 2-7 s
        self.assertIsNone(self.run_for(ds.Suggester(), 10, 5, {"cam1": 60, "cam3": 66}, "cam1", since=0)[0])  # lead 6
        self.assertIsNone(self.run_for(ds.Suggester(), 10, 5, {"cam1": 10, "cam3": 30}, "cam1", since=0)[0])  # below 35
        self.assertIsNone(self.run_for(ds.Suggester(), 10, 5, {"cam1": 40, "cam3": 74}, "cam1", since=0, blocked=True)[0])
        # nothing on air: the best camera is suggested without waiting
        self.assertEqual(self.run_for(ds.Suggester(), 10, 3, {"cam2": 50}, None, since=0)[0]["cam"], "cam2")

    def test_lost_lead_timeout_and_snooze(self):
        s = ds.Suggester()
        self.run_for(s, 10, 2.5, {"cam1": 40, "cam3": 74}, "cam1", since=0)
        cur, events = s.update(13, {"cam1": 70, "cam3": 72}, "cam1", 0)
        self.assertIsNone(cur)
        self.assertEqual(events[0][1], "lead lost")
        cur, events = self.run_for(s, 20, 20, {"cam1": 40, "cam3": 74}, "cam1", since=0)
        self.assertIn("timed out", [e[1] for e in events])
        self.assertIsNone(cur)                                   # resting after the timeout
        self.assertGreater(s.snoozed["cam3"], 40)

    def test_ignore_snoozes_that_camera(self):
        s = ds.Suggester()
        self.run_for(s, 10, 2.5, {"cam1": 40, "cam3": 74, "cam4": 60}, "cam1", since=0)
        done, outcome = s.ignore(13)
        self.assertEqual((done["cam"], outcome), ("cam3", "ignored"))
        cur, _ = self.run_for(s, 13, 3, {"cam1": 40, "cam3": 74, "cam4": 60}, "cam1", since=0)
        self.assertEqual(cur["cam"], "cam4")     # cam3 rests: the next camera that is clearly better
        s.ignore(16.5)
        cur, _ = self.run_for(s, 17, 5, {"cam1": 40, "cam3": 74, "cam4": 60}, "cam1", since=0)
        self.assertIsNone(cur)                   # both rest
        cur, _ = self.run_for(s, 44, 3, {"cam1": 40, "cam3": 74, "cam4": 60}, "cam1", since=0)
        self.assertEqual(cur["cam"], "cam3")     # cam3 rested 30 s

import math
import unittest

import director_scoring as ds
import pitch_map as pm

# A camera on the near touchline, raised, looking across the pitch: pitch metres -> image px.
CAMERA = [5.5, 0.3, 640.0, 0.1, 1.6, 400.0, 0.0003, -0.0105, 1.0]


def seen(x, y):
    return pm.apply(CAMERA, x, y)


def points(pitch, keys, jitter=0.0):
    marks = pm.landmarks(pitch)
    out = []
    for i, k in enumerate(keys):
        u, v = seen(*marks[k][1:])
        out.append({"x": (u + (jitter if i == 0 else 0)) / 1280, "y": v / 720, "mark": k})
    return out


class PitchMapTests(unittest.TestCase):
    def setUp(self):
        self.pitch = pm.clean_pitch({})
        self.keys = ["box_l_f", "box_l_n", "half_f", "half_n", "centre", "box_r_n", "circle_l"]

    def test_fit_recovers_the_camera(self):
        m, info = pm.PitchMap.from_points(points(self.pitch, self.keys), self.pitch)
        self.assertTrue(info["checked"])
        self.assertLess(info["error_m"], 0.01)
        for x, y in ((0, 0), (-30, 10), (25, -20)):
            u, v = seen(x, y)
            px, py = m.to_pitch(u, v)
            self.assertAlmostEqual(px, x, places=3)
            self.assertAlmostEqual(py, y, places=3)
            iu, iv = m.to_image(x, y)
            self.assertAlmostEqual(iu, u, places=2)
            self.assertAlmostEqual(iv, v, places=2)

    def test_four_points_fit_but_are_not_checked(self):
        _, info = pm.PitchMap.from_points(points(self.pitch, self.keys[:4]), self.pitch)
        self.assertFalse(info["checked"])

    def test_a_wrong_click_shows_as_error(self):
        _, info = pm.PitchMap.from_points(points(self.pitch, self.keys, jitter=60), self.pitch)
        self.assertGreater(info["error_m"], 0.3)

    def test_collinear_points_are_refused(self):
        bad = points(self.pitch, ["half_f", "centre", "half_n", "circle_f"])
        with self.assertRaises(ValueError):
            pm.PitchMap.from_points(bad, self.pitch)

    def test_on_pitch_lines_view_and_scale(self):
        m, _ = pm.PitchMap.from_points(points(self.pitch, self.keys), self.pitch)
        self.assertTrue(m.on_pitch(*seen(10, 5)))
        self.assertFalse(m.on_pitch(*seen(10, 40)))           # 6 m beyond the near touchline
        self.assertTrue(m.lines())
        view = m.view()
        self.assertGreaterEqual(len(view), 3)
        self.assertTrue(all(abs(x) <= 55.5 and abs(y) <= 37 for x, y in view))
        near, far = m.scale_at(0, 30), m.scale_at(0, -30)
        self.assertGreater(near, far)                        # near side looks bigger

    def test_pitch_validation(self):
        self.assertEqual(pm.clean_pitch({"length": 60, "width": 40, "box_width": 30, "box_depth": 12})["length"], 60)
        for bad in ({"length": 5}, {"width": "x"}, {"width": 30, "box_width": 40.32}):
            with self.assertRaises(ValueError):
                pm.clean_pitch(bad)

    def test_pitch_ball_memory(self):
        b = pm.PitchBall()
        self.assertIsNone(b.update(0.0, []))
        b.update(1.0, [("cam1", 3, 4, 0.4), ("cam2", 3.5, 4, 0.8)])
        self.assertEqual(b.current(1.5)["cam"], "cam2")
        self.assertIsNone(b.current(3.0))


class PitchScoringTests(unittest.TestCase):
    def test_ball_hint_gives_ball_credit_and_area_filters_people(self):
        pitch = pm.clean_pitch({})
        m, _ = pm.PitchMap.from_points(points(pitch, ["box_l_f", "box_l_n", "half_f", "half_n", "centre"]), pitch)
        area = lambda x, y: m.on_pitch(x, y, margin=2.0)
        u, v = seen(0, 0)
        hint = (ds.Detection(u, v - 4, 8, 8, 0.7), 0.2)
        pic = ds.Picture(sharpness=200, brightness=120)
        player_on = ds.Detection(u + 30, v - 40, 30, 80, 0.9)
        su, sv = seen(5, 45)                                   # spectator far behind the near touchline
        spectator = ds.Detection(su, sv - 40, 30, 80, 0.9)
        without = ds.rate(ds.CameraState(), 1.0, 1280, 720, [player_on, spectator], [], pic, area=area)
        with_hint = ds.rate(ds.CameraState(), 1.0, 1280, 720, [player_on, spectator], [], pic, area=area, ball_hint=hint)
        self.assertEqual(without["action"]["off_pitch"], 1)
        self.assertTrue(with_hint["ball"]["from_pitch"])
        self.assertGreater(with_hint["score"], without["score"])
        self.assertTrue(with_hint["mapped"])


if __name__ == "__main__":
    unittest.main()

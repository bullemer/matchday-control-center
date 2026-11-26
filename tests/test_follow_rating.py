import csv
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import ai_director as ad
import camera_control as cc
import follow_rating as fr


def rated(score=60, ball=0.5, play=0.75, frozen=False, online=True):
    return {"online": online, "score": score, "ball": {"part": ball}, "action": {"part": play},
            "quality": {"frozen": frozen}}


class FollowRatingTests(unittest.TestCase):
    def setUp(self):
        fr.reset()
        cc.reset()
        self.addCleanup(fr.reset)
        self.addCleanup(cc.reset)

    def test_too_few_or_old_ratings_say_nothing(self):
        for t in range(fr.MIN_SAMPLES - 1):
            fr.add("cam1", rated(), True, now=1000 + t)
        self.assertIsNone(fr.summary("cam1", now=1010))
        fr.add("cam1", rated(), True, now=1009)
        self.assertEqual(fr.summary("cam1", now=1010)["score"], 60)
        self.assertIsNone(fr.summary("cam1", now=1030))      # the camera went away
        self.assertIsNone(fr.summary("cam2", now=1010))

    def test_summary_of_the_last_minute(self):
        for t in range(40):                                   # old minute: bad picture
            fr.add("cam1", rated(score=10, ball=0, play=0), False, now=1000 + t)
        for t in range(60):                                   # half with the ball, all following
            fr.add("cam1", rated(score=80 if t % 2 else 40, ball=0.9 if t % 2 else 0, play=0.5), True, now=1100 + t)
        s = fr.summary("cam1", now=1160)
        self.assertEqual((s["score"], s["ball_pct"], s["play_pct"], s["following_pct"], s["frozen_pct"], s["seconds"]),
                         (60.0, 50, 50, 100, 0, 59))
        self.assertEqual(list(fr.summaries(now=1160)), ["cam1"])

    def test_offline_and_repeated_ratings_are_skipped(self):
        fr.add("cam1", rated(online=False), True, now=1000)
        fr.add("cam1", {"online": True, "score": None}, True, now=1001)
        for t in range(20):
            fr.add("cam1", rated(), True, now=1002 + t * 0.1)  # same state file read again
        self.assertEqual(len(fr._samples["cam1"]), 4)

    def test_one_file_row_a_minute_with_the_tuning(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(fr, "LOG_FILE", Path(folder) / "sub" / "rating.csv"):
            self.assertFalse(fr.log_row("cam1", {"follow_gain": 40}, now=1000))   # nothing rated yet
            for t in range(20):
                fr.add("cam1", rated(), True, now=1000 + t)
            self.assertTrue(fr.log_row("cam1", {"follow_gain": 40}, now=1020))
            self.assertFalse(fr.log_row("cam1", {"follow_gain": 40}, now=1050))
            for t in range(20):
                fr.add("cam1", rated(score=70), True, now=1070 + t)
            self.assertTrue(fr.log_row("cam1", {"follow_gain": 55}, now=1090))
            with fr.LOG_FILE.open() as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual([(r["camera"], r["score"], r["tuning"]) for r in rows],
                             [("cam1", "60.0", '{"follow_gain": 40}'), ("cam1", "70.0", '{"follow_gain": 55}')])

    def test_director_feeds_only_touchline_cameras(self):
        phone = {"app": "touchline 0.1.0", "mode": "gimbal", "control_session": "s", "stream": "live",
                 "gimbal": "ready", "tracking": True, "tuning_active": {"follow_gain": 40}}
        cc.record_report("cam1", phone)
        cc.record_report("cam2", dict(phone, app="chameleon-stream 0.9.2"))
        state = {"cams": {"cam1": rated(), "cam2": rated(), "cam3": rated()}}
        with patch.object(ad, "_state_age", return_value=1), patch.object(ad, "_read_state", return_value=state):
            for t in range(12):
                ad._rate_followers(now=2000 + t)
        self.assertEqual(list(fr.summaries(now=2012)), ["cam1"])
        self.assertEqual(fr.summary("cam1", now=2012)["following_pct"], 100)
        with patch.object(ad, "_state_age", return_value=ad.STALE_S + 1), patch.object(ad, "_read_state", return_value=state):
            ad._rate_followers(now=2013)                       # worker stopped: nothing added
        self.assertEqual(len(fr._samples["cam1"]), 12)


class FollowRatingRouteTests(unittest.TestCase):
    def test_the_phone_gets_its_rating_back(self):
        import app as a
        fr.reset(); cc.reset()
        self.addCleanup(fr.reset); self.addCleanup(cc.reset)
        client = a.app.test_client()
        with patch.object(a, "camera_password", return_value=""):
            body = {"app": "touchline 0.1.0"}
            self.assertNotIn("rating", client.post("/api/camera-report/cam3", json=body).get_json())
            import time
            now = time.time()
            for t in range(15):
                fr.add("cam3", rated(score=55), True, now=now - 15 + t)
            answer = client.post("/api/camera-report/cam3", json=body).get_json()
            self.assertEqual((answer["rating"]["score"], answer["rating"]["ball_pct"]), (55.0, 100))


if __name__ == "__main__":
    unittest.main()

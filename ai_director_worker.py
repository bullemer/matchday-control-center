"""AI Director worker: watches the match cameras and rates them (separate process).

Runs in its own Python environment (ai-venv: torch + ultralytics) so the GPU work can
never disturb the broadcast app. Started and stopped by ai_director.py.

Per camera an ffmpeg reader keeps only the newest frame (decoded on the NVIDIA card when
possible). The main loop analyses all cameras with a fresh frame together:
  - players: stock YOLO26s, class "person"
  - ball: box2boxcam-ball-v1.0 (YOLO26s fine-tuned for small balls on amateur pitches)
and director_scoring.rate() turns that into a score. Output (atomic writes):
  <out>/state.json    scores and details for every camera
  <out>/<cam>.jpg     annotated thumbnail, about once per second
"""
import argparse
import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import director_scoring as ds  # noqa: E402

W, H = 1280, 720            # analysis frame size (16:9, letterboxed if needed)
THUMB_W = 640
THUMB_EVERY_S = 1.0


class Reader(threading.Thread):
    """ffmpeg -> raw BGR frames at the analysis rate; keeps only the newest one."""

    def __init__(self, cam, url, fps, hwaccel):
        super().__init__(daemon=True)
        self.cam, self.url, self.fps, self.hwaccel = cam, url, fps, hwaccel
        self.frame, self.frame_t, self.online = None, 0.0, False
        self.error = ""
        self.lock = threading.Lock()
        self.proc = None
        self.stopped = False

    def command(self, hw):
        cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error"]
        if hw:
            cmd += ["-hwaccel", "cuda"]
        cmd += ["-rtsp_transport", "tcp", "-timeout", "5000000", "-i", self.url, "-an",
                "-vf", f"fps={self.fps},scale={W}:{H}:force_original_aspect_ratio=decrease,"
                       f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2",
                "-f", "rawvideo", "-pix_fmt", "bgr24", "pipe:1"]
        return cmd

    def run(self):
        hw = self.hwaccel
        size = W * H * 3
        while not self.stopped:
            started = time.monotonic()
            self.proc = subprocess.Popen(self.command(hw), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            got = 0
            while not self.stopped:
                buf = self.proc.stdout.read(size)
                if len(buf) < size:
                    break
                got += 1
                with self.lock:
                    self.frame = np.frombuffer(buf, np.uint8).reshape(H, W, 3)
                    self.frame_t = time.time()
                    self.online = True
            err = self.proc.stderr.read().decode(errors="replace").strip()[-300:] if self.proc.stderr else ""
            self.proc.kill()
            self.proc.wait()
            with self.lock:
                self.online = False
                self.error = err
            if self.stopped:
                break
            if got == 0 and hw and time.monotonic() - started < 10:
                hw = False  # NVDEC refused this stream: software decoding from now on
                continue
            time.sleep(2.0)  # camera offline: try again

    def latest(self):
        with self.lock:
            return self.frame, self.frame_t, self.online

    def stop(self):
        self.stopped = True
        if self.proc and self.proc.poll() is None:
            self.proc.kill()


def picture_metrics(frame, previous):
    small = cv2.resize(frame, (320, 180), interpolation=cv2.INTER_AREA)
    grey = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
    sharp = float(cv2.Laplacian(grey, cv2.CV_64F).var())
    bright = float(grey.mean())
    clipped = float(((grey <= 3) | (grey >= 252)).mean())
    change = float(np.abs(grey.astype(np.int16) - previous.astype(np.int16)).mean()) if previous is not None else 10.0
    return ds.Picture(sharpness=sharp, brightness=bright, clipped=clipped, change=change), grey


def detections(result, keep=None):
    out = []
    if result.boxes is None:
        return out
    for (x, y, w, h), conf, cls in zip(result.boxes.xywh.tolist(), result.boxes.conf.tolist(), result.boxes.cls.tolist()):
        if keep is None or int(cls) in keep:
            out.append(ds.Detection(x, y, w, h, conf))
    return out


def thumbnail(frame, persons, balls, info):
    img = frame.copy()
    for p in ds.players(persons, H):
        cv2.rectangle(img, (int(p.x - p.w / 2), int(p.y - p.h / 2)), (int(p.x + p.w / 2), int(p.y + p.h / 2)), (238, 104, 167), 2)
    for b in balls:
        if b.conf >= ds.BALL_CONF:
            r = int(max(b.w, b.h) / 2) + 10
            cv2.circle(img, (int(b.x), int(b.y)), r, (0, 255, 255), 3)
    cv2.putText(img, f"{info['score']:.0f}", (20, 70), cv2.FONT_HERSHEY_SIMPLEX, 2.2, (0, 0, 0), 9)
    cv2.putText(img, f"{info['score']:.0f}", (20, 70), cv2.FONT_HERSHEY_SIMPLEX, 2.2, (255, 255, 255), 3)
    thumb = cv2.resize(img, (THUMB_W, int(THUMB_W * H / W)), interpolation=cv2.INTER_AREA)
    ok, jpg = cv2.imencode(".jpg", thumb, [cv2.IMWRITE_JPEG_QUALITY, 70])
    return jpg.tobytes() if ok else None


def write_atomic(path, data):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cams", default="cam1,cam2,cam3,cam4")
    ap.add_argument("--rtsp", default="rtsp://127.0.0.1:8554")
    ap.add_argument("--fps", type=float, default=5.0)
    ap.add_argument("--out", required=True)
    ap.add_argument("--models", required=True)
    ap.add_argument("--no-hwaccel", action="store_true")
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    cams = [c for c in args.cams.split(",") if c]
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())

    def status(**extra):
        write_atomic(out / "state.json", json.dumps({"updated": time.time(), "pid": os.getpid(), **extra}).encode())

    status(phase="loading", cams={})
    import torch
    from ultralytics import YOLO
    device = 0 if torch.cuda.is_available() else "cpu"
    gpu = torch.cuda.get_device_name(0) if device == 0 else "CPU (no CUDA: slow)"
    person_model = YOLO(str(Path(args.models) / "yolo26s.pt"))
    ball_model = YOLO(str(Path(args.models) / "box2boxcam-ball-v1.0.pt"))
    precision = dict(quantize=16) if device == 0 else {}

    readers = {c: Reader(c, f"{args.rtsp}/{c}", args.fps, not args.no_hwaccel) for c in cams}
    for r in readers.values():
        r.start()
    states = {c: ds.CameraState() for c in cams}
    last_seen = {c: 0.0 for c in cams}
    greys = {c: None for c in cams}
    results = {}
    thumbs_t = {c: 0.0 for c in cams}
    recommender = ds.Recommender()
    period = 1.0 / args.fps
    loop_times = []

    while not stop.is_set():
        t0 = time.time()
        batch = []
        for c, r in readers.items():
            frame, ft, online = r.latest()
            if not online:
                results[c] = {"online": False, "error": r.error[-160:]}
                states[c] = ds.CameraState()
                greys[c] = None
                continue
            if frame is not None and ft > last_seen[c]:
                last_seen[c] = ft
                batch.append((c, frame))
        if batch:
            frames = [f for _, f in batch]
            people = person_model.predict(frames, imgsz=960, classes=[0], conf=0.3, device=device, verbose=False, **precision)
            balls = ball_model.predict(frames, imgsz=1280, conf=0.15, device=device, verbose=False, **precision)
            now = time.time()
            for (c, frame), pr, br in zip(batch, people, balls):
                persons, ball_dets = detections(pr, keep={0}), detections(br)
                pic, greys[c] = picture_metrics(frame, greys[c])
                info = ds.rate(states[c], now, W, H, persons, ball_dets, pic)
                results[c] = {"online": True, **info}
                if now - thumbs_t[c] >= THUMB_EVERY_S:
                    jpg = thumbnail(frame, persons, ball_dets, info)
                    if jpg:
                        write_atomic(out / f"{c}.jpg", jpg)
                        thumbs_t[c] = now
                results[c]["image_t"] = thumbs_t[c]
        scores = {c: r["score"] for c, r in results.items() if r.get("online")}
        recommended = recommender.update(time.time(), scores)
        loop_times.append(time.time() - t0)
        loop_times = loop_times[-50:]
        status(phase="running", gpu=gpu, fps=args.fps, recommended=recommended,
               busy=round(sum(loop_times) / len(loop_times) / period, 2), cams=results)
        stop.wait(max(0.0, period - (time.time() - t0)))

    for r in readers.values():
        r.stop()
    status(phase="stopped", cams={})


if __name__ == "__main__":
    main()

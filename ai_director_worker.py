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
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import cv2
import numpy as np

cv2.setNumThreads(2)   # the machine also decodes and encodes video: no OpenCV thread storm

sys.path.insert(0, str(Path(__file__).resolve().parent))
import director_scoring as ds  # noqa: E402
import pitch_map as pm  # noqa: E402

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
            # Decode AND scale on the GPU: a 4K phone stream then costs ~35 % of a core, not ~130 %.
            cmd += ["-hwaccel", "cuda", "-hwaccel_output_format", "cuda"]
            scale = (f"scale_cuda={W}:{H}:force_original_aspect_ratio=decrease:format=nv12,"
                     f"hwdownload,format=nv12")
        else:
            scale = f"scale={W}:{H}:force_original_aspect_ratio=decrease"
        cmd += ["-rtsp_transport", "tcp", "-timeout", "5000000", "-i", self.url, "-an",
                "-vf", f"fps={self.fps},{scale},pad={W}:{H}:(ow-iw)/2:(oh-ih)/2",
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


def thumbnail(frame, persons, balls, info, fence=None, pmap=None, hint=None):
    """Players violet, ball yellow; with a field fence: fence cyan, ignored people/balls grey.
    With a pitch map: the pitch lines in green, the ball position from another camera as a
    dashed orange ring."""
    img = frame.copy()
    polygon = None if pmap else ds.fence_pixels(fence, W, H)
    area = pitch_area(pmap) if pmap else ds.area_of(polygon)
    if polygon:
        cv2.polylines(img, [np.array(polygon, np.int32)], True, (255, 255, 0), 2)
    if pmap:
        for line in pmap.lines(W, H):
            cv2.polylines(img, [np.array([(x * W, y * H) for x, y in line], np.int32)], False, (80, 220, 120), 2)
    if hint is not None:
        hb = hint[0]
        r = int(max(hb.w, hb.h) / 2) + 14
        for a in range(0, 360, 40):
            cv2.ellipse(img, (int(hb.x), int(hb.y)), (r, r), 0, a, a + 20, (0, 140, 255), 3)
    for p in ds.players(persons, H):
        colour, width = ((238, 104, 167), 2) if ds.on_pitch(area, p) else ((120, 120, 120), 1)
        cv2.rectangle(img, (int(p.x - p.w / 2), int(p.y - p.h / 2)), (int(p.x + p.w / 2), int(p.y + p.h / 2)), colour, width)
    for b in balls:
        if b.conf >= ds.BALL_CONF:
            r = int(max(b.w, b.h) / 2) + 10
            colour = (0, 255, 255) if ds.ball_on_pitch(area, b, H) else (120, 120, 120)
            cv2.circle(img, (int(b.x), int(b.y)), r, colour, 3)
    cv2.putText(img, f"{info['score']:.0f}", (20, 70), cv2.FONT_HERSHEY_SIMPLEX, 2.2, (0, 0, 0), 9)
    cv2.putText(img, f"{info['score']:.0f}", (20, 70), cv2.FONT_HERSHEY_SIMPLEX, 2.2, (255, 255, 255), 3)
    thumb = cv2.resize(img, (THUMB_W, int(THUMB_W * H / W)), interpolation=cv2.INTER_AREA)
    ok, jpg = cv2.imencode(".jpg", thumb, [cv2.IMWRITE_JPEG_QUALITY, 70])
    return jpg.tobytes() if ok else None


class Fences:
    """Field fences per camera from a JSON file {cam: [[x, y], ...]} (relative), reloaded when
    it changes. broadcast-control writes only fixed cameras into it."""

    def __init__(self, path):
        self.path = Path(path) if path else None
        self.mtime = None
        self.data = {}

    def get(self, cam):
        if self.path is not None:
            try:
                mtime = self.path.stat().st_mtime
            except OSError:
                mtime = None
            if mtime != self.mtime:
                self.mtime = mtime
                try:
                    raw = json.loads(self.path.read_text()) if mtime is not None else {}
                    self.data = {c: [(float(x), float(y)) for x, y in pts] for c, pts in raw.items() if len(pts) >= 3}
                except (OSError, ValueError, TypeError):
                    self.data = {}
        return self.data.get(cam)


def pitch_area(pmap):
    """A mapped camera: people and balls count within 2 m of the pitch."""
    return lambda x, y: pmap.on_pitch(x, y, margin=2.0)


class PitchMaps:
    """Pitch maps per camera from <out>/pitch.json {"pitch": {...}, "cams": {cam: points}},
    written by broadcast-control for fixed cameras only; refitted when the file changes."""

    def __init__(self, path):
        self.path = Path(path) if path else None
        self.mtime = None
        self.maps = {}
        self.pitch = None

    def get(self, cam):
        self._reload()
        return self.maps.get(cam)

    def _reload(self):
        if self.path is None:
            return
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            mtime = None
        if mtime == self.mtime:
            return
        self.mtime, self.maps, self.pitch = mtime, {}, None
        if mtime is None:
            return
        try:
            raw = json.loads(self.path.read_text())
            self.pitch = pm.clean_pitch(raw.get("pitch") or {})
            for cam, points in (raw.get("cams") or {}).items():
                try:
                    self.maps[cam] = pm.PitchMap.from_points(points, self.pitch)[0]
                except (ValueError, KeyError, TypeError) as exc:
                    print(f"pitch map {cam}: {exc}", file=sys.stderr)
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            print(f"pitch maps unreadable: {exc}", file=sys.stderr)


def ball_hint(pmap, ball):
    """Where the pitch ball is in this camera's picture: (Detection, age) or None."""
    if pmap is None or ball is None:
        return None
    q = pmap.to_image(ball["x"], ball["y"])
    scale = pmap.scale_at(ball["x"], ball["y"])
    if q is None or scale is None or not (0 <= q[0] <= W and 0 <= q[1] <= H):
        return None
    r = max(2.0, 0.11 * scale)               # a football is 22 cm across
    return ds.Detection(q[0], q[1] - r, 2 * r, 2 * r, ball["conf"]), ball["age"]


MAX_PLAYERS, MAX_BALLS = 50, 5        # detections kept per frame (Ultralytics default 300: costly post-processing)
PICTURE_EVERY_S = 0.2                  # sharpness / exposure / freeze measured 5x per second is plenty
PERSON_SIZE, BALL_SIZE = (544, 960), (736, 1280)   # input h, w for 16:9 frames (TensorRT engines: exactly these)


def load_model(YOLO, models, name, size, device):
    """The TensorRT engine (models/<name>.engine, built by scripts/setup-ai-director.sh
    --tensorrt) when it exists and works on this GPU, else the PyTorch model.
    MCC_AI_TENSORRT=0 forces PyTorch."""
    engine = models / f"{name}.engine"
    if device == 0 and engine.exists() and os.environ.get("MCC_AI_TENSORRT", "1") != "0":
        try:
            model = YOLO(str(engine), task="detect")
            model.predict([np.zeros((H, W, 3), np.uint8)], imgsz=size, device=device, verbose=False)
            return model, "TensorRT"
        except Exception as exc:  # built for another GPU / TensorRT version: fall back
            print(f"{engine.name} not usable ({exc}); using PyTorch", file=sys.stderr)
    return YOLO(str(models / f"{name}.pt")), "PyTorch"


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
    ap.add_argument("--fences", default="", help="JSON file with field fences per camera")
    ap.add_argument("--pitch", default="", help="JSON file with pitch maps per camera")
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
    person_model, person_engine = load_model(YOLO, Path(args.models), "yolo26s", PERSON_SIZE, device)
    ball_model, ball_engine = load_model(YOLO, Path(args.models), "box2boxcam-ball-v1.0", BALL_SIZE, device)
    engine = person_engine if person_engine == ball_engine else f"{person_engine} + {ball_engine}"
    p_kw = dict(quantize=16) if person_engine == "PyTorch" and device == 0 else {}
    b_kw = dict(quantize=16) if ball_engine == "PyTorch" and device == 0 else {}

    readers = {c: Reader(c, f"{args.rtsp}/{c}", args.fps, not args.no_hwaccel) for c in cams}
    for r in readers.values():
        r.start()
    states = {c: ds.CameraState() for c in cams}
    last_seen = {c: 0.0 for c in cams}
    greys = {c: None for c in cams}
    results = {}
    thumbs_t = {c: 0.0 for c in cams}
    recommender = ds.Recommender()
    fences = Fences(args.fences)
    pitch_maps = PitchMaps(args.pitch)
    pitch_ball = pm.PitchBall()
    pitch_players = {}
    period = 1.0 / args.fps
    person_period = period * (2 if args.fps > 10 else 1)
    person_cache, person_t = {}, {}
    pics, pic_t = {}, {}
    pool = ThreadPoolExecutor(1, thread_name_prefix="players")
    loop_times = []

    while not stop.is_set():
        t0 = time.time()
        batch = []
        for c, r in readers.items():
            frame, ft, online = r.latest()
            if not online:
                results[c] = {"online": False, "error": r.error[-160:]}
                person_cache.pop(c, None)
                states[c] = ds.CameraState()
                greys[c] = None
                continue
            if frame is not None and ft > last_seen[c]:
                last_seen[c] = ft
                batch.append((c, frame))
        if batch:
            frames = [f for _, f in batch]
            # Above 10 frames/s players are detected at half the rate (they move slower than the
            # ball; the ball model is the expensive one): the other frames reuse the last players.
            need = [i for i, (c, _) in enumerate(batch) if t0 - person_t.get(c, 0.0) >= person_period - 0.5 * period]
            run_people = (lambda: person_model.predict([frames[i] for i in need], imgsz=PERSON_SIZE, classes=[0], conf=0.3,
                                                       max_det=MAX_PLAYERS, device=device, verbose=False, **p_kw)) if need else (lambda: [])
            people_job = pool.submit(run_people)     # in parallel with the ball model
            balls = ball_model.predict(frames, imgsz=BALL_SIZE, conf=0.15, max_det=MAX_BALLS, device=device, verbose=False, **b_kw)
            for i, pr in zip(need, people_job.result()):
                c = batch[i][0]
                person_cache[c], person_t[c] = detections(pr, keep={0}), t0
            now = time.time()
            rounds = []
            sightings = []
            for (c, frame), br in zip(batch, balls):
                persons, ball_dets = person_cache.get(c, []), detections(br)
                pmap = pitch_maps.get(c)
                rounds.append((c, frame, persons, ball_dets, pmap))
                if pmap is None:
                    pitch_players.pop(c, None)
                    continue
                # Feet and ball (its lowest point) on the pitch, in metres.
                feet = [pmap.to_pitch(p.x, p.y + p.h / 2) for p in ds.players(persons, H)
                        if pmap.on_pitch(p.x, p.y + p.h / 2, margin=2.0)]
                pitch_players[c] = [[round(x, 1), round(y, 1)] for x, y in (f for f in feet if f)]
                good = [b for b in ball_dets if b.conf >= ds.BALL_CONF and pmap.on_pitch(b.x, b.y + b.h / 2, margin=3.0)]
                if good:
                    b = max(good, key=lambda d: d.conf)
                    pos = pmap.to_pitch(b.x, b.y + b.h / 2)
                    if pos:
                        sightings.append((c, pos[0], pos[1], b.conf))
            ball_now = pitch_ball.update(now, sightings)
            for c, frame, persons, ball_dets, pmap in rounds:
                if now - pic_t.get(c, 0.0) >= PICTURE_EVERY_S or c not in pics:
                    pics[c], greys[c] = picture_metrics(frame, greys[c])
                    pic_t[c] = now
                pic = pics[c]
                fence = None if pmap else fences.get(c)
                hint = ball_hint(pmap, ball_now) if ball_now and ball_now["cam"] != c else None
                info = ds.rate(states[c], now, W, H, persons, ball_dets, pic, fence,
                               area=pitch_area(pmap) if pmap else None, ball_hint=hint)
                results[c] = {"online": True, **info}
                if now - thumbs_t[c] >= THUMB_EVERY_S:
                    jpg = thumbnail(frame, persons, ball_dets, info, fence, pmap, hint if info["ball"]["from_pitch"] else None)
                    if jpg:
                        write_atomic(out / f"{c}.jpg", jpg)
                        thumbs_t[c] = now
                results[c]["image_t"] = thumbs_t[c]
        scores = {c: r["score"] for c, r in results.items() if r.get("online")}
        recommended = recommender.update(time.time(), scores)
        loop_times.append(time.time() - t0)
        loop_times = loop_times[-50:]
        status(phase="running", gpu=gpu, engine=engine, fps=args.fps, recommended=recommended,
               busy=round(sum(loop_times) / len(loop_times) / period, 2), cams=results,
               pitch={"ball": pitch_ball.current(time.time()),
                      "players": {c: v for c, v in pitch_players.items() if results.get(c, {}).get("online")}})
        stop.wait(max(0.0, period - (time.time() - t0)))

    for r in readers.values():
        r.stop()
    status(phase="stopped", cams={})


if __name__ == "__main__":
    main()

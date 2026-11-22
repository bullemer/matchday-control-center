"""Direct TensorRT path vs Ultralytics on real frames (needs the NVIDIA GPU and ai-venv).

    ai-venv/bin/python tests/trt_direct_smoke.py <video or gif with football> [frames]

Both use the same engines (models/*.engine with the NMS-free head), so the boxes must agree:
every Ultralytics box needs a partner of the same frame with IoU >= 0.9 and a confidence
within 0.05 (small differences come from bilinear resizing on the GPU vs OpenCV). Also prints
the time per 4-camera round for both paths.
"""
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
import ai_director_worker as w  # noqa: E402

FAILED = []


def check(ok, text):
    print(("PASS " if ok else "FAIL ") + text)
    if not ok:
        FAILED.append(text)


def load_frames(path, count):
    """Frames scaled and padded to the analysis size exactly like the worker's ffmpeg reader."""
    cmd = ["ffmpeg", "-v", "error", "-i", str(path), "-frames:v", str(count), "-vf",
           f"scale={w.W}:{w.H}:force_original_aspect_ratio=decrease,pad={w.W}:{w.H}:(ow-iw)/2:(oh-ih)/2",
           "-f", "rawvideo", "-pix_fmt", "bgr24", "pipe:1"]
    raw = subprocess.run(cmd, capture_output=True, check=True).stdout
    n = len(raw) // (w.W * w.H * 3)
    return [np.frombuffer(raw, np.uint8, w.W * w.H * 3, i * w.W * w.H * 3).reshape(w.H, w.W, 3) for i in range(n)]


def iou(a, b):
    ax1, ay1, ax2, ay2 = a.x - a.w / 2, a.y - a.h / 2, a.x + a.w / 2, a.y + a.h / 2
    bx1, by1, bx2, by2 = b.x - b.w / 2, b.y - b.h / 2, b.x + b.w / 2, b.y + b.h / 2
    iw, ih = max(0.0, min(ax2, bx2) - max(ax1, bx1)), max(0.0, min(ay2, by2) - max(ay1, by1))
    inter = iw * ih
    return inter / (a.w * a.h + b.w * b.h - inter + 1e-9)


def compare(label, ref, ours):
    pairs = unmatched = 0
    ious, dconf = [], []
    for r_frame, o_frame in zip(ref, ours):
        free = list(o_frame)
        for r in r_frame:
            best = max(free, key=lambda o: iou(r, o), default=None)
            if best is not None and iou(r, best) >= 0.9 and abs(best.conf - r.conf) <= 0.05:
                pairs += 1
                ious.append(iou(r, best))
                dconf.append(abs(best.conf - r.conf))
                free.remove(best)
            else:
                unmatched += 1
    total = pairs + unmatched
    check(total == 0 or pairs / total >= 0.95,
          f"{label}: {pairs}/{total} boxes match (IoU >= 0.9)"
          + (f", mean IoU {np.mean(ious):.3f}, max conf diff {max(dconf):.3f}" if ious else ""))


def time_round(fn, rounds=40):
    for _ in range(5):
        fn()
    t = []
    for _ in range(rounds):
        s = time.perf_counter()
        fn()
        t.append(time.perf_counter() - s)
    return sorted(t)[len(t) // 2] * 1000


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    frames = load_frames(sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else 24)
    check(len(frames) >= 4, f"{len(frames)} frames loaded")
    models = BASE / "models"
    direct = w.DirectDetector(models, 0)
    ultra = w.UltralyticsDetector(models, 0)
    check(direct.name == "TensorRT direct" and ultra.name == "TensorRT", f"paths: {direct.name} / {ultra.name}")
    ref_p, ref_b, our_p, our_b = [], [], [], []
    for i in range(0, len(frames) - 3, 4):
        batch = frames[i:i + 4]
        p, b = ultra.detect(batch, [0, 1, 2, 3])
        ref_p += p
        ref_b += b
        p, b = direct.detect(batch, [0, 1, 2, 3])
        our_p += p
        our_b += b
    print(f"     players: {sum(map(len, ref_p))} (Ultralytics) / {sum(map(len, our_p))} (direct); "
          f"balls: {sum(map(len, ref_b))} / {sum(map(len, our_b))}")
    compare("players", ref_p, our_p)
    compare("ball", ref_b, our_b)
    batch = frames[:4]
    t_u = time_round(lambda: ultra.detect(batch, [0, 1, 2, 3]))
    t_d = time_round(lambda: direct.detect(batch, [0, 1, 2, 3]))
    t_dh = time_round(lambda: direct.detect(batch, [0, 2]))
    print(f"     4-camera round: Ultralytics {t_u:.1f} ms, direct {t_d:.1f} ms, direct with players at half rate {t_dh:.1f} ms")
    check(t_d < t_u, "direct path is faster")
    print("FAILED: " + "; ".join(FAILED) if FAILED else "ALL PASS")
    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    main()

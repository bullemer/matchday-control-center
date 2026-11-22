"""AI Director: rate how good a match camera's picture is right now (0-100).

Pure Python (no torch/numpy) so it can be unit-tested and tuned without a GPU. The
detection itself runs in ai_director_worker.py; this module only turns detections and
picture measurements into a score.

Ideas (concepts only, no code reused):
- box2boxcam (camera guidance): follow the ball, and when it is hidden the group of players.
- XbotGo Falcon firmware (strings/configs only): the camera follows the *crowd* of players
  (centre, density, fast/slow), the ball is only a hint; a user-drawn field fence drops
  spectators; decisions need N confirming frames and hysteresis.
- Football-Analysis-System: ball visible / size, players near the ball (possession area).

Score parts (each 0..1):
  ball     - ball visible now (or a moment ago), well inside the frame, big enough to see
  action   - players around the ball (or, without ball, the densest group of players)
  quality  - sharp, well exposed, not frozen
score = 100 * (W_BALL * ball + W_ACTION * action + W_QUALITY * quality), then smoothed.
"""
import math
from dataclasses import dataclass, field

# The players carry the most weight (like XbotGo): the ball detector finds fewer than half of
# the small, far balls (box2boxcam v1.0: recall 45 %), so the ball is a strong but unreliable hint.
W_BALL = 0.35
W_ACTION = 0.45
W_QUALITY = 0.20

BALL_CONF = 0.25          # ball detections below this are ignored
BALL_MEMORY_S = 1.5       # a ball seen this long ago still counts (fading)
PLAYER_MIN_H = 0.015      # people smaller than 1.5 % of the frame height are ignored (far away / noise)
ACTION_RADIUS = 0.22      # "near the ball" = within 22 % of the frame width
ACTION_FULL = 4           # this many players near the ball = full action
SMOOTH_TAU_S = 1.5        # exponential smoothing of the score
FROZEN_DIFF = 0.6         # mean pixel change below this ...
FROZEN_S = 2.0            # ... for this long = frozen picture


@dataclass
class Detection:
    x: float              # centre, pixels
    y: float
    w: float
    h: float
    conf: float


@dataclass
class Picture:
    """Measurements of the frame itself (made by the worker with OpenCV)."""
    sharpness: float = 0.0     # variance of the Laplacian on a downscaled grey image
    brightness: float = 128.0  # mean grey value 0..255
    clipped: float = 0.0       # share of pixels that are pure black or pure white
    change: float = 10.0       # mean absolute difference to the previous analysed frame


@dataclass
class CameraState:
    """Memory of one camera between frames."""
    last_ball: Detection | None = None
    last_ball_t: float = -1e9
    frozen_since: float | None = None
    smooth: float | None = None
    smooth_t: float = 0.0


def clamp(v, lo=0.0, hi=1.0):
    return max(lo, min(hi, v))


def ball_part(ball, age_s, width, height):
    """Ball visible, well inside the frame and big enough. age_s: seconds since it was seen."""
    if ball is None or age_s > BALL_MEMORY_S:
        return 0.0, {}
    presence = 1.0 if age_s <= 0.25 else clamp(1 - (age_s - 0.25) / (BALL_MEMORY_S - 0.25))
    dx = abs(ball.x - width / 2) / (width / 2)
    dy = abs(ball.y - height / 2) / (height / 2)
    central = clamp(1 - (0.7 * dx + 0.3 * dy))
    # Near the edge the ball (and the play) is about to leave this camera's picture.
    margin = min(ball.x, width - ball.x) / width
    edge = 0.5 if margin < 0.04 else 1.0
    size = clamp(max(ball.w, ball.h) / (0.02 * height))   # 2 % of the frame height = clearly visible
    value = presence * edge * (0.2 + 0.5 * central + 0.3 * size)
    return clamp(value), {"presence": round(presence, 2), "central": round(central, 2), "size": round(size, 2)}


def players(dets, height):
    return [d for d in dets if d.h >= PLAYER_MIN_H * height]


def action_part(people, ball, width, height):
    """Players around the ball; without a ball the densest group of players (and how central it is)."""
    if not people:
        return 0.0, {"near": 0, "group_x": None}
    radius = ACTION_RADIUS * width
    if ball is not None:
        near = sum(1 for p in people if math.hypot(p.x - ball.x, (p.y + p.h / 2) - ball.y) <= radius)
        return clamp(near / ACTION_FULL), {"near": near, "group_x": round(ball.x / width, 3)}
    # No ball: the biggest group within the radius, rated by size and how central it is.
    best, best_x = 0, None
    for p in people:
        n = sum(1 for q in people if math.hypot(q.x - p.x, q.y - p.y) <= radius)
        if n > best:
            best, best_x = n, sum(q.x for q in people if math.hypot(q.x - p.x, q.y - p.y) <= radius) / n
    central = clamp(1 - abs(best_x - width / 2) / (width / 2))
    return clamp(best / ACTION_FULL) * (0.5 + 0.5 * central) * 0.8, {"near": best, "group_x": round(best_x / width, 3)}


def quality_part(pic, frozen):
    if frozen:
        return 0.0, {"sharp": 0.0, "exposure": 0.0, "frozen": True}
    sharp = clamp(pic.sharpness / 120.0)
    # Good exposure: mean grey roughly 60..190, few clipped pixels.
    if pic.brightness < 60:
        exposure = clamp(pic.brightness / 60)
    elif pic.brightness > 190:
        exposure = clamp((255 - pic.brightness) / 65)
    else:
        exposure = 1.0
    exposure *= clamp(1 - pic.clipped * 2)
    return clamp(0.6 * sharp + 0.4 * exposure), {"sharp": round(sharp, 2), "exposure": round(exposure, 2), "frozen": False}


def rate(state, now, width, height, persons, balls, pic):
    """One analysed frame -> score details. Updates state (ball memory, freeze, smoothing)."""
    ball = max((b for b in balls if b.conf >= BALL_CONF), key=lambda b: b.conf, default=None)
    if ball is not None:
        state.last_ball, state.last_ball_t = ball, now
    age = now - state.last_ball_t
    remembered = state.last_ball if age <= BALL_MEMORY_S else None

    if pic.change < FROZEN_DIFF:
        state.frozen_since = state.frozen_since if state.frozen_since is not None else now
    else:
        state.frozen_since = None
    frozen = state.frozen_since is not None and now - state.frozen_since >= FROZEN_S

    people = players(persons, height)
    b, b_info = ball_part(remembered, age, width, height)
    a, a_info = action_part(people, remembered, width, height)
    q, q_info = quality_part(pic, frozen)
    raw = 100 * (W_BALL * b + W_ACTION * a + W_QUALITY * q)
    if frozen:
        raw = 0.0  # a frozen picture must never be recommended

    if state.smooth is None:
        state.smooth = raw
    else:
        alpha = 1 - math.exp(-max(0.0, now - state.smooth_t) / SMOOTH_TAU_S)
        state.smooth += alpha * (raw - state.smooth)
    state.smooth_t = now
    return {
        "score": round(state.smooth, 1), "raw": round(raw, 1),
        "ball": {"seen": ball is not None, "remembered": remembered is not None and ball is None,
                 "conf": round(ball.conf, 2) if ball else None, "part": round(b, 2), **b_info},
        "action": {"part": round(a, 2), "players": len(people), **a_info},
        "quality": {"part": round(q, 2), **q_info},
    }


@dataclass
class Recommender:
    """Best camera with hysteresis (display only for now): a new camera must lead by
    MARGIN points for HOLD_S seconds before the recommendation changes."""
    MARGIN: float = 8.0
    HOLD_S: float = 2.0
    current: str | None = None
    challenger: str | None = None
    since: float = 0.0
    history: list = field(default_factory=list)

    def update(self, now, scores):
        if not scores:
            self.current = self.challenger = None
            return None
        best = max(scores, key=scores.get)
        if self.current not in scores:
            self.current, self.challenger = best, None
            return self.current
        if best != self.current and scores[best] >= scores[self.current] + self.MARGIN:
            if self.challenger != best:
                self.challenger, self.since = best, now
            elif now - self.since >= self.HOLD_S:
                self.current, self.challenger = best, None
        else:
            self.challenger = None
        return self.current

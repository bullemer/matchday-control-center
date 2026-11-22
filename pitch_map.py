"""Pitch map: image <-> pitch coordinates for a fixed camera (a homography).

The operator clicks at least four known pitch markings (corner flags, penalty box corners,
halfway line, centre spot ...) on a still of the camera. From those the image plane of the
grass is mapped to metres on the pitch. Then the AI Director knows *where on the pitch*
players and ball are, sees off-pitch people without a hand-drawn fence, and can ask every
mapped camera whether it has the ball's position in view (even if its own detector missed it).

Pure Python (no numpy) so the dashboard process can use it and it is unit-testable.

Pitch coordinates (metres): origin at the centre spot; x along the pitch from the LEFT goal
(-length/2) to the RIGHT goal (+length/2) as seen from the cameras' side; y across from the
FAR touchline (-width/2) to the NEAR touchline (+width/2, the side the cameras stand on).
"""
import math

# Markings of an 11-a-side pitch (IFAB Law 1); every value can be changed per pitch.
DEFAULT_PITCH = {
    "length": 105.0, "width": 68.0,
    "box_depth": 16.5, "box_width": 40.32,       # penalty area
    "six_depth": 5.5, "six_width": 18.32,        # goal area
    "spot": 11.0, "circle": 9.15, "goal_width": 7.32,
}
PITCH_LIMITS = {
    "length": (20.0, 130.0), "width": (12.0, 100.0), "box_depth": (3.0, 25.0), "box_width": (5.0, 60.0),
    "six_depth": (1.0, 10.0), "six_width": (3.0, 30.0), "spot": (3.0, 15.0), "circle": (2.0, 12.0),
    "goal_width": (1.0, 8.0),
}
MIN_POINTS = 4
MAX_POINTS = 24
FRAME_W, FRAME_H = 1280, 720      # the worker's analysis frame (points are stored relative 0..1)


def clean_pitch(data):
    """Validated pitch size and markings (missing values = 11-a-side defaults)."""
    out = dict(DEFAULT_PITCH)
    for key, (lo, hi) in PITCH_LIMITS.items():
        if not isinstance(data, dict) or key not in data:
            continue
        v = data[key]
        if type(v) not in (int, float) or not math.isfinite(v) or not lo <= v <= hi:
            raise ValueError(f"{key.replace('_', ' ')} must be {lo:g}-{hi:g} m")
        out[key] = float(v)
    if out["box_width"] >= out["width"] or out["box_depth"] * 2 >= out["length"]:
        raise ValueError("The penalty box does not fit on this pitch")
    if out["six_width"] >= out["box_width"] or out["six_depth"] >= out["box_depth"]:
        raise ValueError("The goal area must be smaller than the penalty box")
    return out


def landmarks(p):
    """Named pitch points: key -> (label, x, y) in metres."""
    L, W = p["length"] / 2, p["width"] / 2
    bw, sw, gw = p["box_width"] / 2, p["six_width"] / 2, p["goal_width"] / 2
    marks = {
        "corner_lf": ("Corner flag left, far", -L, -W),
        "corner_ln": ("Corner flag left, near", -L, W),
        "corner_rf": ("Corner flag right, far", L, -W),
        "corner_rn": ("Corner flag right, near", L, W),
        "half_f": ("Halfway line at the far touchline", 0.0, -W),
        "half_n": ("Halfway line at the near touchline", 0.0, W),
        "centre": ("Centre spot", 0.0, 0.0),
        "circle_l": ("Centre circle, left point", -p["circle"], 0.0),
        "circle_r": ("Centre circle, right point", p["circle"], 0.0),
        "circle_f": ("Centre circle, far point", 0.0, -p["circle"]),
        "circle_n": ("Centre circle, near point", 0.0, p["circle"]),
    }
    for side, sx, name in (("l", -1, "left"), ("r", 1, "right")):
        gx, bx, sx6 = sx * L, sx * (L - p["box_depth"]), sx * (L - p["six_depth"])
        marks.update({
            f"box_{side}_fg": (f"Penalty box {name}: far line meets goal line", gx, -bw),
            f"box_{side}_ng": (f"Penalty box {name}: near line meets goal line", gx, bw),
            f"box_{side}_f": (f"Penalty box {name}: far front corner", bx, -bw),
            f"box_{side}_n": (f"Penalty box {name}: near front corner", bx, bw),
            f"six_{side}_fg": (f"Goal area {name}: far line meets goal line", gx, -sw),
            f"six_{side}_ng": (f"Goal area {name}: near line meets goal line", gx, sw),
            f"six_{side}_f": (f"Goal area {name}: far front corner", sx6, -sw),
            f"six_{side}_n": (f"Goal area {name}: near front corner", sx6, sw),
            f"spot_{side}": (f"Penalty spot {name}", sx * (L - p["spot"]), 0.0),
            f"post_{side}_f": (f"Goal {name}: far post (foot)", gx, -gw),
            f"post_{side}_n": (f"Goal {name}: near post (foot)", gx, gw),
            f"box_{side}_arc": (f"Penalty arc {name}: tip", sx * (L - p["spot"] - p["circle"]), 0.0),
        })
    return marks


# --- 3x3 matrices as flat lists [a, b, c, d, e, f, g, h, i] ------------------------------

def apply(H, x, y):
    """Map a point; None when it lies on/behind the horizon (w <= 0 after sign fixing)."""
    w = H[6] * x + H[7] * y + H[8]
    if w <= 1e-12:
        return None
    return (H[0] * x + H[1] * y + H[2]) / w, (H[3] * x + H[4] * y + H[5]) / w


def multiply(A, B):
    return [sum(A[r * 3 + k] * B[k * 3 + c] for k in range(3)) for r in range(3) for c in range(3)]


def invert(H):
    a, b, c, d, e, f, g, h, i = H
    det = a * (e * i - f * h) - b * (d * i - f * g) + c * (d * h - e * g)
    if abs(det) < 1e-15:
        raise ValueError("The mapping cannot be inverted")
    inv = [(e * i - f * h), -(b * i - c * h), (b * f - c * e),
           -(d * i - f * g), (a * i - c * g), -(a * f - c * d),
           (d * h - e * g), -(a * h - b * g), (a * e - b * d)]
    return [v / det for v in inv]


def _normaliser(pts):
    """Hartley normalisation: centroid to 0, mean distance sqrt(2)."""
    cx = sum(p[0] for p in pts) / len(pts)
    cy = sum(p[1] for p in pts) / len(pts)
    mean = sum(math.hypot(p[0] - cx, p[1] - cy) for p in pts) / len(pts) or 1.0
    s = math.sqrt(2) / mean
    return [s, 0, -s * cx, 0, s, -s * cy, 0, 0, 1]


def _solve(M, v):
    """Gaussian elimination with partial pivoting; None when (nearly) singular."""
    n = len(v)
    A = [row[:] + [v[i]] for i, row in enumerate(M)]
    scale = max(abs(x) for row in M for x in row) or 1.0
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(A[r][col]))
        if abs(A[piv][col]) < 1e-10 * scale:
            return None
        A[col], A[piv] = A[piv], A[col]
        for r in range(n):
            if r != col:
                f = A[r][col] / A[col][col]
                if f:
                    A[r] = [x - f * y for x, y in zip(A[r], A[col])]
    return [A[i][n] / A[i][i] for i in range(n)]


def fit(src, dst):
    """Homography H with dst ~ H * src (least squares for more than four points).
    The sign is fixed so that w > 0 for the given points (in front of the camera)."""
    if len(src) != len(dst) or len(src) < MIN_POINTS:
        raise ValueError(f"At least {MIN_POINTS} points are needed")
    Ts, Td = _normaliser(src), _normaliser(dst)
    ns = [apply_affine(Ts, *p) for p in src]
    nd = [apply_affine(Td, *p) for p in dst]
    # Unknowns h0..h7 (h8 = 1): normal equations of the 2n x 8 system.
    AtA = [[0.0] * 8 for _ in range(8)]
    Atb = [0.0] * 8
    for (x, y), (u, v) in zip(ns, nd):
        for row, rhs in (([x, y, 1, 0, 0, 0, -u * x, -u * y], u), ([0, 0, 0, x, y, 1, -v * x, -v * y], v)):
            for i in range(8):
                Atb[i] += row[i] * rhs
                for j in range(8):
                    AtA[i][j] += row[i] * row[j]
    h = _solve(AtA, Atb)
    if h is None:
        raise ValueError("These points do not define the pitch (three or more on one line?)")
    Hn = h + [1.0]
    H = multiply(invert(Td), multiply(Hn, Ts))
    ws = [H[6] * x + H[7] * y + H[8] for x, y in src]
    if all(w < 0 for w in ws):
        H = [-v for v in H]
    elif not all(w > 0 for w in ws):
        raise ValueError("The points contradict each other (check which marking each one is)")
    norm = math.sqrt(sum(v * v for v in H))
    return [v / norm for v in H]


def apply_affine(T, x, y):
    return T[0] * x + T[1] * y + T[2], T[3] * x + T[4] * y + T[5]


class PitchMap:
    """A fitted mapping of one camera: image pixels (analysis frame) <-> pitch metres."""

    def __init__(self, H, pitch):
        self.H = list(H)                    # image -> pitch
        self.Hinv = _fix_sign(invert(self.H), self.H)   # pitch -> image
        self.pitch = pitch

    @classmethod
    def from_points(cls, points, pitch, width=FRAME_W, height=FRAME_H):
        """points: [{"x": 0..1, "y": 0..1, "mark": key}, ...] -> (PitchMap, info)."""
        marks = landmarks(pitch)
        src, dst, keys = [], [], []
        for pt in points:
            if pt["mark"] not in marks:
                raise ValueError(f"Unknown marking {pt['mark']}")
            src.append((pt["x"] * width, pt["y"] * height))
            dst.append(marks[pt["mark"]][1:])
            keys.append(pt["mark"])
        m = cls(fit(src, dst), pitch)
        errors = []
        for (x, y), (px, py) in zip(src, dst):
            got = m.to_pitch(x, y)
            errors.append(math.hypot(got[0] - px, got[1] - py) if got else 99.0)
        rms = math.sqrt(sum(e * e for e in errors) / len(errors))
        worst = max(range(len(errors)), key=lambda i: errors[i])
        return m, {"error_m": round(rms, 2), "checked": len(points) > MIN_POINTS,
                   "worst": keys[worst] if len(points) > MIN_POINTS else None,
                   "worst_m": round(errors[worst], 2)}

    def to_pitch(self, x, y):
        return apply(self.H, x, y)

    def to_image(self, x, y):
        return apply(self.Hinv, x, y)

    def scale_at(self, x, y):
        """Image pixels per metre at a pitch point (how big things look there); None if not in view."""
        a, b = self.to_image(x - 0.5, y), self.to_image(x + 0.5, y)
        c, d = self.to_image(x, y - 0.5), self.to_image(x, y + 0.5)
        if None in (a, b, c, d):
            return None
        return max(math.hypot(b[0] - a[0], b[1] - a[1]), math.hypot(d[0] - c[0], d[1] - c[1]))

    def on_pitch(self, x, y, margin=2.0):
        """Image point -> is it on the pitch (plus margin metres)?"""
        p = self.to_pitch(x, y)
        if p is None:
            return False
        return abs(p[0]) <= self.pitch["length"] / 2 + margin and abs(p[1]) <= self.pitch["width"] / 2 + margin

    def lines(self, width=FRAME_W, height=FRAME_H):
        """The pitch markings drawn into the image (relative 0..1 polylines) to check the map."""
        out = []
        for poly in pitch_lines(self.pitch):
            dense = _densify(poly, 1.0)
            cur = []
            for x, y in dense:
                q = self.to_image(x, y)
                if q is None or not (-0.5 * width <= q[0] <= 1.5 * width and -0.5 * height <= q[1] <= 1.5 * height):
                    if len(cur) >= 2:
                        out.append(cur)
                    cur = []
                    continue
                cur.append([round(q[0] / width, 4), round(q[1] / height, 4)])
            if len(cur) >= 2:
                out.append(cur)
        return out

    def view(self, width=FRAME_W, height=FRAME_H, samples=24):
        """The part of the pitch this camera sees: polygon in pitch metres (clipped to the pitch)."""
        border = ([(width * i / samples, height) for i in range(samples + 1)] +
                  [(width, height * (1 - i / samples)) for i in range(1, samples + 1)] +
                  [(width * (1 - i / samples), 0) for i in range(1, samples + 1)] +
                  [(0, height * i / samples) for i in range(1, samples)])
        L, W = self.pitch["length"] / 2 + 3, self.pitch["width"] / 2 + 3
        pts = []
        for x, y in border:
            p = self.to_pitch(x, y)
            if p is not None and abs(p[0]) < 4 * L and abs(p[1]) < 4 * W:
                pts.append(p)
        poly = clip(pts, -L, -W, L, W)
        return [[round(x, 1), round(y, 1)] for x, y in poly]


def _fix_sign(inv, H):
    """Hinv maps pitch -> image; choose its sign so that w > 0 for points the camera sees."""
    # The image centre (below the horizon in any sensible framing) maps to a pitch point;
    # mapping that point back must give w > 0.
    for x, y in ((FRAME_W / 2, FRAME_H * 0.9), (FRAME_W / 2, FRAME_H / 2), (FRAME_W / 2, FRAME_H)):
        p = apply(H, x, y)
        if p is not None:
            w = inv[6] * p[0] + inv[7] * p[1] + inv[8]
            return inv if w > 0 else [-v for v in inv]
    return inv


def pitch_lines(p):
    """The markings as polylines in metres."""
    L, W = p["length"] / 2, p["width"] / 2
    bw, sw = p["box_width"] / 2, p["six_width"] / 2
    lines = [[(-L, -W), (L, -W), (L, W), (-L, W), (-L, -W)], [(0, -W), (0, W)]]
    circle = [(p["circle"] * math.cos(a * math.pi / 24), p["circle"] * math.sin(a * math.pi / 24)) for a in range(49)]
    lines.append(circle)
    for sx in (-1, 1):
        lines.append([(sx * L, -bw), (sx * (L - p["box_depth"]), -bw), (sx * (L - p["box_depth"]), bw), (sx * L, bw)])
        lines.append([(sx * L, -sw), (sx * (L - p["six_depth"]), -sw), (sx * (L - p["six_depth"]), sw), (sx * L, sw)])
        spot = sx * (L - p["spot"])
        lines.append([(spot - 0.3, 0), (spot + 0.3, 0)])
        # Penalty arc: the part of the circle around the spot outside the box.
        if p["box_depth"] - p["spot"] < p["circle"]:
            half = math.acos((p["box_depth"] - p["spot"]) / p["circle"])
            lines.append([(sx * (L - p["spot"] - p["circle"] * math.cos(t)), p["circle"] * math.sin(t))
                          for t in (-half + 2 * half * i / 16 for i in range(17))])
    return lines


def _densify(poly, step):
    out = []
    for (x1, y1), (x2, y2) in zip(poly, poly[1:]):
        n = max(1, int(math.hypot(x2 - x1, y2 - y1) / step))
        out.extend((x1 + (x2 - x1) * i / n, y1 + (y2 - y1) * i / n) for i in range(n))
    out.append(poly[-1])
    return out


def clip(poly, x0, y0, x1, y1):
    """Sutherland-Hodgman: polygon clipped to a rectangle."""
    def cut(points, inside, cross):
        out = []
        for i, cur in enumerate(points):
            prev = points[i - 1]
            if inside(cur):
                if not inside(prev):
                    out.append(cross(prev, cur))
                out.append(cur)
            elif inside(prev):
                out.append(cross(prev, cur))
        return out

    def at_x(xv):
        return lambda a, b: (xv, a[1] + (b[1] - a[1]) * (xv - a[0]) / (b[0] - a[0]))

    def at_y(yv):
        return lambda a, b: (a[0] + (b[0] - a[0]) * (yv - a[1]) / (b[1] - a[1]), yv)

    for inside, cross in ((lambda p: p[0] >= x0, at_x(x0)), (lambda p: p[0] <= x1, at_x(x1)),
                          (lambda p: p[1] >= y0, at_y(y0)), (lambda p: p[1] <= y1, at_y(y1))):
        if not poly:
            break
        poly = cut(poly, inside, cross)
    return poly


class PitchBall:
    """One ball position on the pitch from all mapped cameras: the most confident fresh
    sighting wins; it is remembered for MEMORY_S seconds."""
    MEMORY_S = 1.5

    def __init__(self):
        self.pos = None      # (x, y) metres
        self.t = -1e9
        self.conf = 0.0
        self.cam = None

    def update(self, now, sightings):
        """sightings: [(cam, x, y, conf)] from this round."""
        if sightings:
            cam, x, y, conf = max(sightings, key=lambda s: s[3])
            self.pos, self.t, self.conf, self.cam = (x, y), now, conf, cam
        return self.current(now)

    def current(self, now):
        if self.pos is None or now - self.t > self.MEMORY_S:
            return None
        return {"x": round(self.pos[0], 1), "y": round(self.pos[1], 1), "age": round(now - self.t, 2),
                "cam": self.cam, "conf": round(self.conf, 2)}

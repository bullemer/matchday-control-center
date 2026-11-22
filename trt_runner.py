"""Direct TensorRT inference for the AI Director: no Ultralytics in the frame loop.

Ultralytics' predictor does its letterbox, normalisation and box conversion in Python per
image; at 4 cameras x 20 frames/s that Python work cost more than the GPU inference itself.
Here a whole batch goes to the GPU once, colour conversion / scaling / padding run as torch
operations, the engine is called directly, and only the finished boxes (at most 300 per
image, about 7 kB) come back to the CPU.

Needs an engine with the NMS-free YOLO26 head (exported with nms=False: metadata
"end2end": true; output [batch, 300, 6] = x1, y1, x2, y2, confidence, class in input pixels).
Older engines raise ValueError so the worker can fall back to Ultralytics.

Runs in ai-venv (torch + tensorrt); imported by ai_director_worker.py only.
"""
import json
import struct
import warnings

import numpy as np
import tensorrt as trt
import torch
import torch.nn.functional as F

PAD_VALUE = 114 / 255          # Ultralytics letterbox grey


def read_engine(path):
    """Ultralytics engine file = 4-byte little-endian length + JSON metadata + TensorRT plan."""
    with open(path, "rb") as f:
        n = struct.unpack("<i", f.read(4))[0]
        meta = json.loads(f.read(n).decode())
        return meta, f.read()


def letterbox_geometry(src_h, src_w, dst_h, dst_w):
    """Scale and padding exactly like Ultralytics' LetterBox (centred, rounding +-0.1)."""
    r = min(dst_h / src_h, dst_w / src_w)
    new_w, new_h = int(round(src_w * r)), int(round(src_h * r))
    dw, dh = (dst_w - new_w) / 2, (dst_h - new_h) / 2
    top, bottom = int(round(dh - 0.1)), int(round(dh + 0.1))
    left, right = int(round(dw - 0.1)), int(round(dw + 0.1))
    return r, new_h, new_w, top, bottom, left, right


class EngineRunner:
    """One TensorRT engine with its own CUDA stream. submit() only enqueues GPU work and
    returns at once; collect() waits for it and returns the boxes."""

    def __init__(self, path, size, device=0):
        self.meta, plan = read_engine(path)
        if not self.meta.get("end2end"):
            raise ValueError(f"{path}: engine without the NMS-free head (rebuild with setup-ai-director.sh --tensorrt)")
        self.h, self.w = size
        self.device = torch.device("cuda", device)
        self.engine = trt.Runtime(trt.Logger(trt.Logger.WARNING)).deserialize_cuda_engine(plan)
        if self.engine is None:
            raise ValueError(f"{path}: TensorRT could not load the engine (other GPU / TensorRT version?)")
        self.ctx = self.engine.create_execution_context()
        if self.ctx is None:
            raise ValueError(f"{path}: no execution context (not enough GPU memory?)")
        names = [self.engine.get_tensor_name(i) for i in range(self.engine.num_io_tensors)]
        self.inp = next(n for n in names if self.engine.get_tensor_mode(n) == trt.TensorIOMode.INPUT)
        self.out = next(n for n in names if self.engine.get_tensor_mode(n) == trt.TensorIOMode.OUTPUT)
        self.max_batch = self.engine.get_tensor_profile_shape(self.inp, 0)[2][0]
        self.in_dtype = torch.float16 if self.engine.get_tensor_dtype(self.inp) == trt.DataType.HALF else torch.float32
        self.out_dtype = torch.float16 if self.engine.get_tensor_dtype(self.out) == trt.DataType.HALF else torch.float32
        self.stream = torch.cuda.Stream(self.device)

    def submit(self, frames):
        """frames: uint8 CUDA tensor [B, H, W, 3] (BGR, all the same size). Returns a job for collect()."""
        if frames.shape[0] == 0:
            return None
        if frames.shape[0] > self.max_batch:
            raise ValueError(f"at most {self.max_batch} frames per batch")
        self.stream.wait_stream(torch.cuda.current_stream(self.device))   # the upload is done first
        with torch.cuda.stream(self.stream):
            src_h, src_w = frames.shape[1], frames.shape[2]
            r, nh, nw, top, bottom, left, right = letterbox_geometry(src_h, src_w, self.h, self.w)
            x = frames.permute(0, 3, 1, 2).flip(1).float().div_(255.0)          # BGR HWC -> RGB CHW, 0..1
            if (nh, nw) != (src_h, src_w):
                x = F.interpolate(x, size=(nh, nw), mode="bilinear", align_corners=False)
            x = F.pad(x, (left, right, top, bottom), value=PAD_VALUE).to(self.in_dtype).contiguous()
            self.ctx.set_input_shape(self.inp, tuple(x.shape))
            out = torch.empty(tuple(self.ctx.get_tensor_shape(self.out)), dtype=self.out_dtype, device=self.device)
            self.ctx.set_tensor_address(self.inp, x.data_ptr())
            self.ctx.set_tensor_address(self.out, out.data_ptr())
            if not self.ctx.execute_async_v3(self.stream.cuda_stream):
                raise RuntimeError("TensorRT execution failed")
        # x stays referenced in the job: TensorRT reads it until the stream has finished.
        return {"x": x, "out": out, "geometry": (r, top, left), "src": (src_h, src_w)}

    def collect(self, job, conf, classes=None, max_det=300):
        """Wait for a submitted batch -> per frame a list of (x_centre, y_centre, w, h, conf) in
        source-frame pixels, best first."""
        if job is None:
            return []
        self.stream.synchronize()
        raw = job["out"].float().cpu().numpy()                     # [B, 300, 6]
        r, top, left = job["geometry"]
        src_h, src_w = job["src"]
        result = []
        for det in raw:
            keep = det[:, 4] >= conf
            if classes is not None:
                keep &= np.isin(det[:, 5].astype(int), list(classes))
            d = det[keep]
            d = d[np.argsort(-d[:, 4])][:max_det]
            x1 = np.clip((d[:, 0] - left) / r, 0, src_w)
            y1 = np.clip((d[:, 1] - top) / r, 0, src_h)
            x2 = np.clip((d[:, 2] - left) / r, 0, src_w)
            y2 = np.clip((d[:, 3] - top) / r, 0, src_h)
            result.append([((a + c) / 2, (b + e) / 2, c - a, e - b, float(s))
                           for a, b, c, e, s in zip(x1.tolist(), y1.tolist(), x2.tolist(), y2.tolist(), d[:, 4].tolist())])
        return result


def upload(frames, device=0, buffer=None):
    """List of HxWx3 uint8 frames -> one CUDA tensor [B, H, W, 3] (reusing buffer when it fits)."""
    n, (h, w, c) = len(frames), frames[0].shape
    if buffer is None or buffer.shape[1:] != (h, w, c) or buffer.shape[0] < n:
        buffer = torch.empty((max(n, 4), h, w, c), dtype=torch.uint8, device=torch.device("cuda", device))
    with warnings.catch_warnings():
        # Reader frames are read-only views of the ffmpeg pipe buffer; they are only read here.
        warnings.filterwarnings("ignore", message="The given NumPy array is not writable")
        for i, f in enumerate(frames):
            buffer[i].copy_(torch.from_numpy(np.ascontiguousarray(f)))
    return buffer[:n], buffer

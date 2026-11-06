"""USB (UVC) webcam as an MJPEG source, straight through V4L2 ioctls (no
ffmpeg/OpenCV). MJPEG cameras' frames are passed on untouched; YUYV-only
cameras are JPEG-encoded here (python3-pil + python3-numpy). Capture runs
only while somebody is watching."""

import asyncio
import fcntl
import glob
import io
import mmap
import os
import select
import struct
import threading
import time

from . import config

# ---------- V4L2 ABI (64-bit) ----------


def _iowr(nr, size, rw=3):
    return (rw << 30) | (size << 16) | (ord("V") << 8) | nr


BUF_TYPE_CAPTURE = 1
MEMORY_MMAP = 1
CAP_VIDEO_CAPTURE = 0x1
CAP_DEVICE_CAPS = 0x80000000
MJPEG = struct.unpack("<I", b"MJPG")[0]
YUYV = struct.unpack("<I", b"YUYV")[0]

_CAP_SZ, _FMTDESC_SZ, _FMT_SZ, _REQBUF_SZ, _BUF_SZ, _PARM_SZ = 104, 64, 208, 20, 88, 204
VIDIOC_QUERYCAP = _iowr(0, _CAP_SZ, 2)
VIDIOC_ENUM_FMT = _iowr(2, _FMTDESC_SZ)
VIDIOC_S_FMT = _iowr(5, _FMT_SZ)
VIDIOC_REQBUFS = _iowr(8, _REQBUF_SZ)
VIDIOC_QUERYBUF = _iowr(9, _BUF_SZ)
VIDIOC_QBUF = _iowr(15, _BUF_SZ)
VIDIOC_DQBUF = _iowr(17, _BUF_SZ)
VIDIOC_STREAMON = _iowr(18, 4, 1)
VIDIOC_STREAMOFF = _iowr(19, 4, 1)
VIDIOC_S_PARM = _iowr(22, _PARM_SZ)

# struct v4l2_buffer: index, type, bytesused, flags, field @0..20; memory @60,
# m.offset @64, length @72
_BUF_IDX, _BUF_USED, _BUF_MEM, _BUF_OFF, _BUF_LEN = 0, 8, 60, 64, 72


def _ioctl(fd, req, buf):
    b = bytearray(buf)
    fcntl.ioctl(fd, req, b)
    return b


def _querycap(fd):
    b = _ioctl(fd, VIDIOC_QUERYCAP, bytes(_CAP_SZ))
    card = b[16:48].split(b"\0")[0].decode(errors="replace")
    caps, dev_caps = struct.unpack_from("<II", b, 84)
    return card, dev_caps if caps & CAP_DEVICE_CAPS else caps


def _best_format(fd):
    """MJPEG if the camera has it, else YUYV, else None."""
    have = set()
    for i in range(32):
        try:
            b = _ioctl(fd, VIDIOC_ENUM_FMT, struct.pack("<II", i, BUF_TYPE_CAPTURE).ljust(_FMTDESC_SZ, b"\0"))
        except OSError:
            break
        have.add(struct.unpack_from("<I", b, 44)[0])
    return next((f for f in (MJPEG, YUYV) if f in have), None)


def _yuyv_to_jpeg(buf, w, h):
    import numpy as np                  # only needed for YUYV cameras
    from PIL import Image
    a = np.frombuffer(buf, np.uint8, w * h * 2).reshape(h, w // 2, 4)    # Y0 U Y1 V
    ycc = np.empty((h, w, 3), np.uint8)
    ycc[:, :, 0] = a[:, :, 0::2].reshape(h, w)
    ycc[:, 0::2, 1] = ycc[:, 1::2, 1] = a[:, :, 1]
    ycc[:, 0::2, 2] = ycc[:, 1::2, 2] = a[:, :, 3]
    out = io.BytesIO()
    Image.frombytes("YCbCr", (w, h), ycc.tobytes()).save(out, "JPEG", quality=config.CAMERA_JPEG_QUALITY)
    return out.getvalue()


def find_device():
    """First USB camera with MJPEG or YUYV -> (path, name, fourcc), or Nones."""
    paths = [config.CAMERA_DEVICE] if config.CAMERA_DEVICE else sorted(glob.glob("/dev/v4l/by-id/usb-*-video-index0"))
    for p in paths:
        try:
            fd = os.open(p, os.O_RDWR | os.O_NONBLOCK)
        except OSError:
            continue
        try:
            name, caps = _querycap(fd)
            fmt = _best_format(fd) if caps & CAP_VIDEO_CAPTURE else None
            if fmt:
                return p, name, fmt
        except OSError:
            pass
        finally:
            os.close(fd)
    return None, None, None

# ---------- capture ----------


class Camera:
    def __init__(self):
        self.frame = b""               # latest JPEG
        self.seq = 0
        self.width = self.height = self.fps = 0
        self.dev = self.name = self.fmt = None
        self.error = None
        self.viewers = 0
        self._rates = []               # fps each viewer asked for
        self._last_view = 0.0
        self._thread = None
        self._loop = None
        self._event = asyncio.Event()

    # called on the event loop
    def status(self):
        if not self.running():
            self.dev, self.name, self.fmt = find_device()
        return {"present": bool(self.dev), "dev": self.dev, "name": self.name, "running": self.running(),
                "format": struct.pack("<I", self.fmt).decode() if self.fmt else None,
                "width": self.width, "height": self.height, "fps": self.fps,
                "viewers": self.viewers, "error": self.error}

    def running(self):
        return bool(self._thread and self._thread.is_alive())

    def start(self):
        """Starts capture if it isn't running (also after an unplug or an idle stop)."""
        self._last_view = time.monotonic()
        if not self.running():
            self._loop = asyncio.get_running_loop()
            self.error = None
            self._thread = threading.Thread(target=self._run, name="camera", daemon=True)
            self._thread.start()

    def acquire(self, fps=30):
        self.viewers += 1
        self._rates.append(fps)
        self.start()

    def release(self, fps=30):
        self.viewers -= 1
        self._rates.remove(fps)
        self._last_view = time.monotonic()

    async def next_frame(self, seq, timeout=5.0):
        """Waits for a frame newer than seq -> (jpeg, seq); jpeg is b"" on
        timeout or when capture stopped (the caller may start() it again)."""
        end = self._loop.time() + timeout
        while self.seq == seq:
            if not self.running() or self._loop.time() >= end:
                return b"", seq
            ev = self._event
            try:
                await asyncio.wait_for(ev.wait(), end - self._loop.time())
            except asyncio.TimeoutError:
                pass
        return self.frame, self.seq

    def _publish(self, jpeg):
        self.frame, self.seq = jpeg, self.seq + 1
        ev, self._event = self._event, asyncio.Event()
        ev.set()

    # camera thread
    def _idle(self):
        return self.viewers <= 0 and time.monotonic() - self._last_view > config.CAMERA_IDLE_S

    def _run(self):
        self.dev, self.name, self.fmt = find_device()
        if not self.dev:
            self.error = "no USB camera found"
            return
        try:
            fd = os.open(self.dev, os.O_RDWR | os.O_NONBLOCK)
        except OSError as e:
            self.error = str(e)
            return
        maps = []
        try:
            w, h = config.CAMERA_SIZE
            pix = struct.pack("<IIII", w, h, self.fmt, 1)              # field = NONE
            b = _ioctl(fd, VIDIOC_S_FMT, (struct.pack("<I4x", BUF_TYPE_CAPTURE) + pix).ljust(_FMT_SZ, b"\0"))
            self.width, self.height = struct.unpack_from("<II", b, 8)   # the driver picks the nearest size
            parm = struct.pack("<IIIII", BUF_TYPE_CAPTURE, 0, 0, 1, config.CAMERA_FPS).ljust(_PARM_SZ, b"\0")
            try:
                b = _ioctl(fd, VIDIOC_S_PARM, parm)
                num, den = struct.unpack_from("<II", b, 12)
                self.fps = round(den / num) if num else 0
            except OSError:
                self.fps = 0
            b = _ioctl(fd, VIDIOC_REQBUFS, struct.pack("<III", 4, BUF_TYPE_CAPTURE, MEMORY_MMAP).ljust(_REQBUF_SZ, b"\0"))
            for i in range(struct.unpack_from("<I", b, 0)[0]):
                q = bytearray(_BUF_SZ)
                struct.pack_into("<II", q, 0, i, BUF_TYPE_CAPTURE)
                struct.pack_into("<I", q, _BUF_MEM, MEMORY_MMAP)
                q = _ioctl(fd, VIDIOC_QUERYBUF, q)
                off, = struct.unpack_from("<I", q, _BUF_OFF)
                ln, = struct.unpack_from("<I", q, _BUF_LEN)
                maps.append(mmap.mmap(fd, ln, mmap.MAP_SHARED, mmap.PROT_READ, offset=off))
                _ioctl(fd, VIDIOC_QBUF, q)
            _ioctl(fd, VIDIOC_STREAMON, struct.pack("<I", BUF_TYPE_CAPTURE))
            print(f"camera: {self.name} {self.width}x{self.height} @ {self.fps} fps on {self.dev}", flush=True)
            last = 0.0
            while not self._idle():
                if not select.select([fd], [], [], 2.0)[0]:
                    continue
                q = bytearray(_BUF_SZ)
                struct.pack_into("<II", q, 0, 0, BUF_TYPE_CAPTURE)
                struct.pack_into("<I", q, _BUF_MEM, MEMORY_MMAP)
                try:
                    q = _ioctl(fd, VIDIOC_DQBUF, q)
                except BlockingIOError:
                    continue
                i, = struct.unpack_from("<I", q, _BUF_IDX)
                used, = struct.unpack_from("<I", q, _BUF_USED)
                data = maps[i][:used]
                _ioctl(fd, VIDIOC_QBUF, q)
                if self.fmt == YUYV:
                    # encoding costs CPU: only as often as the fastest viewer wants
                    now = time.monotonic()
                    if used < self.width * self.height * 2 or now - last < 0.9 / max(self._rates or [30]):
                        continue
                    last = now
                    jpeg = _yuyv_to_jpeg(data, self.width, self.height)
                else:
                    jpeg = data
                if jpeg[:2] == b"\xff\xd8":     # skip the odd corrupt/empty frame
                    self._loop.call_soon_threadsafe(self._publish, jpeg)
            print("camera: idle, stopped", flush=True)
        except (OSError, ImportError) as e:
            self.error = str(e)
            print(f"camera: {e}", flush=True)
        finally:
            try:
                _ioctl(fd, VIDIOC_STREAMOFF, struct.pack("<I", BUF_TYPE_CAPTURE))
            except OSError:
                pass
            for m in maps:
                m.close()
            os.close(fd)


cam = Camera()

"""Offline unit tests for the frame grabber (issue #74, Phase 1 T3).

Run from the repo root:  ``pytest tests/brain/ -q``

No tank, no Pi, no LocalAI, no real network. The grabber is exercised with a
**fake source** (deterministic, no sleeps in the assert path), with
``httpx.MockTransport`` for the pure-HTTP LL-HLS fallback, and with a real
local TCP socket (immediately closed) for the unreachable-URL case. ``cv2`` is
optional: the "no cv2" path is proven in a subprocess that blocks the import.

Covers the issue #74 acceptance:
  * read_latest() returns the last frame produced by a fake source
  * read_latest() -> None before the first frame and after stale_after_s
  * a source returning None keeps serving the last good frame until stale
  * start() idempotent (no duplicate threads); stop() joins cleanly
  * LLHLSSource with an unreachable URL returns None (no exception)
  * LLHLSSource returns None when cv2 is unavailable (module still imports)
  * age_s() reflects last-good timestamp; None before first frame
  * FrameGrab constructed from config.load_config() defaults (cam_url, grab_*)
  * stop() then start() again works (restart cycle)
  * a mid-stream None burst doesn't blank the loop until stale
  * HTTPHLSSource (the Q2 option-(a) fallback) decodes the latest segment
"""

from __future__ import annotations

import socket
import subprocess
import sys
import time

import numpy as np
import pytest

from brain.config import BrainConfig, load_config
from brain.frame_grab import (
    FrameGrab,
    HTTPHLSSource,
    LLHLSSource,
    FrameSource,
    build_frame_grab,
    create_source,
)


def make_frame(tag: int = 0) -> "np.ndarray":
    """A small distinct 8x8 BGR frame (a real ndarray, not a mock)."""
    arr = np.zeros((8, 8, 3), dtype=np.uint8)
    arr[:, :, 0] = 255
    arr[:, :, 1] = (tag * 37) % 256
    arr[:, :, 2] = (tag * 91) % 256
    return arr


class FakeSource:
    """A controllable FrameSource for deterministic tests.

    ``once=True`` models a stream that delivers its frame on the *first* read
    and then stops producing (returns ``None``) -- used to exercise the
    staleness guard without the source refreshing the frame forever.
    """

    def __init__(self, once: bool = False) -> None:
        self._frame = None
        self._once = once
        self._delivered = False
        self._reads = 0

    def set_frame(self, frame) -> None:
        self._frame = frame

    def read(self):
        self._reads += 1
        if self._once and self._delivered:
            return None
        if self._frame is not None:
            self._delivered = True
        return self._frame


def wait_for_frame(grab, timeout_s: float = 2.0):
    """Poll ``read_latest()`` until a frame is available or ``timeout_s`` elapses.

    The grabber thread needs one tick to store the first frame; a bounded poll
    (not an arbitrary sleep) makes the assertion deterministic.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        frame = grab.read_latest()
        if frame is not None:
            return frame
        time.sleep(0.005)
    return None


# ---------------------------------------------------------------------------
# FrameGrab -- core contract (fake source)
# ---------------------------------------------------------------------------

def test_read_latest_returns_last_frame_from_fake_source():
    src = FakeSource()
    src.set_frame(make_frame(1))
    grab = FrameGrab(src, interval_s=0.01, stale_after_s=5.0)
    grab.start()
    try:
        frame = wait_for_frame(grab)
        assert frame is not None
        assert frame.shape == (8, 8, 3)
        # it is the exact frame the fake source produced
        assert (frame == make_frame(1)).all()
    finally:
        grab.stop()


def test_read_latest_none_before_first_frame():
    src = FakeSource()  # returns None
    grab = FrameGrab(src, interval_s=0.01, stale_after_s=5.0)
    grab.start()
    try:
        assert grab.read_latest() is None
        assert grab.age_s() is None
    finally:
        grab.stop()


def test_read_latest_none_after_stale():
    src = FakeSource(once=True)  # delivers one frame, then stops (returns None)
    src.set_frame(make_frame(2))
    grab = FrameGrab(src, interval_s=0.01, stale_after_s=0.05)
    grab.start()
    try:
        assert wait_for_frame(grab) is not None  # fresh
        time.sleep(0.12)  # > stale_after_s, and the source is no longer producing
        assert grab.read_latest() is None  # stale
    finally:
        grab.stop()


def test_none_source_keeps_last_good_frame_until_stale():
    src = FakeSource()
    src.set_frame(make_frame(3))
    grab = FrameGrab(src, interval_s=0.01, stale_after_s=0.2)
    grab.start()
    try:
        assert wait_for_frame(grab) is not None
        # now the stream "drops": source returns None for several reads
        src.set_frame(None)
        time.sleep(0.05)
        assert grab.read_latest() is not None  # still serving last good frame
        time.sleep(0.2)  # now past stale_after_s
        assert grab.read_latest() is None
    finally:
        grab.stop()


def test_start_is_idempotent_no_duplicate_threads():
    import threading

    src = FakeSource()
    grab = FrameGrab(src, interval_s=0.01, stale_after_s=5.0)
    grab.start()
    grab.start()
    grab.start()
    try:
        # exactly one frame-grab worker thread (plus the main test thread)
        names = [t.name for t in threading.enumerate() if t.name == "frame-grab"]
        assert len(names) == 1
    finally:
        grab.stop()


def test_stop_then_start_restart_cycle():
    src = FakeSource()
    src.set_frame(make_frame(4))
    grab = FrameGrab(src, interval_s=0.01, stale_after_s=0.05)
    grab.start()
    try:
        assert wait_for_frame(grab) is not None
    finally:
        grab.stop()
    # after stop + a short wait the stored frame is stale
    time.sleep(0.08)
    assert grab.read_latest() is None
    # restart: a fresh frame becomes available again
    src.set_frame(make_frame(5))
    grab.start()
    try:
        assert wait_for_frame(grab) is not None
        assert (grab.read_latest() == make_frame(5)).all()
    finally:
        grab.stop()


def test_mid_stream_none_burst_does_not_blank_until_stale():
    src = FakeSource()
    src.set_frame(make_frame(6))
    grab = FrameGrab(src, interval_s=0.01, stale_after_s=0.3)
    grab.start()
    try:
        assert wait_for_frame(grab) is not None
        src.set_frame(None)  # burst of None
        for _ in range(5):
            time.sleep(0.02)
            assert grab.read_latest() is not None  # never blanked
        time.sleep(0.3)
        assert grab.read_latest() is None
    finally:
        grab.stop()


def test_age_s_reflects_last_good_frame():
    src = FakeSource()
    src.set_frame(make_frame(7))
    grab = FrameGrab(src, interval_s=0.01, stale_after_s=5.0)
    grab.start()
    try:
        assert wait_for_frame(grab) is not None
        age = grab.age_s()
        assert age is not None and age >= 0.0
    finally:
        grab.stop()


def test_build_frame_grab_from_config_defaults():
    cfg = BrainConfig()
    assert cfg.grab_interval_s == 0.2
    assert cfg.grab_stale_after_s == 3.0
    grab = build_frame_grab(cfg)
    assert isinstance(grab, FrameGrab)
    assert grab._interval_s == 0.2
    assert grab._stale_after_s == 3.0
    # the default cam_url is LL-HLS -> an LLHLSSource (cv2) is chosen
    assert isinstance(grab._source, LLHLSSource)


def test_load_config_resolves_grab_keys():
    cfg = load_config()  # the committed conf/brain.yaml
    assert cfg.grab_interval_s == 0.2
    assert cfg.grab_stale_after_s == 3.0
    assert cfg.grab_http_poll_s == 0.5
    assert cfg.grab_http_timeout_s == 2.0


def test_load_config_env_override_for_grab():
    cfg = load_config(env={"TANKIE_GRAB_INTERVAL_S": "0.5",
                           "TANKIE_GRAB_STALE_S": "9.0"})
    assert cfg.grab_interval_s == 0.5
    assert cfg.grab_stale_after_s == 9.0


# ---------------------------------------------------------------------------
# LLHLSSource -- graceful failure (no cv2 / unreachable URL)
# ---------------------------------------------------------------------------

def test_llhls_unreachable_url_returns_none_no_exception():
    # A real local TCP port that accepts then immediately closes -> the
    # capture cannot establish a stream. cv2 must surface this as None.
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    srv.close()  # port is now free; nothing listens -> connection refused

    src = LLHLSSource(f"rtsp://127.0.0.1:{port}/cam", timeout_s=1.0)
    try:
        assert src.read() is None  # no exception, just None
        assert src.age_s() is None
    finally:
        src.stop()


def test_llhls_no_cv2_module_still_imports_and_returns_none():
    # Prove, in a clean subprocess, that the module imports and a source
    # returns None even when cv2 cannot be imported.
    code = (
        "import sys, types\n"
        "sys.modules['cv2'] = None  # force ImportError on 'import cv2'\n"
        "sys.path.insert(0, 'raspberry_pi')\n"
        "import brain.frame_grab as fg\n"
        "import time\n"
        "s = fg.LLHLSSource('http://127.0.0.1:1/cam')\n"
        "assert s.read() is None\n"
        "time.sleep(0.2)  # let the decode thread start and hit the ImportError\n"
        "assert s.read() is None\n"
        "print('NO_CV2_OK')\n"
    )
    res = subprocess.run(
        [sys.executable, "-c", code],
        cwd=_repo_root(),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert res.returncode == 0, res.stderr
    assert "NO_CV2_OK" in res.stdout


def _repo_root() -> str:
    from pathlib import Path
    return str(Path(__file__).resolve().parents[2])


# ---------------------------------------------------------------------------
# HTTPHLSSource -- the Q2 option-(a) pure-HTTP fallback (MockTransport)
# ---------------------------------------------------------------------------

def _hls_bytes(tag: int) -> bytes:
    """Encode a real frame to JPEG bytes (decodable by cv2.imdecode)."""
    import cv2

    ok, buf = cv2.imencode(".jpg", make_frame(tag))
    assert ok
    return buf.tobytes()


def _hls_handler(tag: int = 1):
    """A realistic mediamtx LL-HLS layout: the ``cam_url`` *is* the media
    playlist (no master), and the ``.ts`` segments sit next to it."""
    import httpx

    seg = _hls_bytes(tag)
    media = (
        "#EXTM3U\n"
        "#EXT-X-VERSION:3\n"
        "#EXT-X-TARGETDURATION:1\n"
        "#EXTINF:1.0,\n"
        "seg0.ts\n"
        "#EXTINF:1.0,\n"
        "seg1.ts\n"
    )

    def handler(request: httpx.Request):
        path = request.url.path
        if path.endswith(".m3u8"):
            return httpx.Response(200, text=media)
        if path.endswith(".ts"):
            return httpx.Response(200, content=seg)
        return httpx.Response(404)

    return handler


def test_http_hls_decodes_latest_segment():
    import httpx

    src = HTTPHLSSource(
        "http://hls.test/cam_low/index.m3u8",
        poll_s=0.01,
        timeout_s=1.0,
        client=httpx.Client(transport=httpx.MockTransport(_hls_handler(11))),
    )
    try:
        # poll until the first frame is decoded (bounded wait)
        frame = None
        for _ in range(200):
            frame = src.read()
            if frame is not None:
                break
            time.sleep(0.01)
        assert frame is not None
        assert frame.shape == (8, 8, 3)
        # JPEG is lossy: allow +-2 per channel (the red channel is the marker)
        ref = make_frame(11).astype("int16")
        assert (abs(frame.astype("int16") - ref).max() <= 2)
        assert src.age_s() is not None
    finally:
        src.stop()


def test_http_hls_unreachable_returns_none():
    import httpx

    def handler(request):
        return httpx.Response(503)

    src = HTTPHLSSource(
        "http://hls.test/cam_low/index.m3u8",
        poll_s=0.01,
        timeout_s=1.0,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    try:
        for _ in range(50):
            assert src.read() is None
            time.sleep(0.01)
        assert src.age_s() is None
    finally:
        src.stop()


def test_http_hls_undecodable_segment_returns_none():
    import httpx

    def handler(request):
        if request.url.path.endswith(".ts"):
            return httpx.Response(200, content=b"not-a-real-video-segment")
        return httpx.Response(200, text="#EXTM3U\n#EXTINF:1.0,\nseg0.ts\n")

    src = HTTPHLSSource(
        "http://hls.test/cam/index.m3u8",
        poll_s=0.01,
        timeout_s=1.0,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    try:
        for _ in range(50):
            assert src.read() is None
            time.sleep(0.01)
    finally:
        src.stop()


# ---------------------------------------------------------------------------
# create_source factory
# ---------------------------------------------------------------------------

def test_create_source_auto_and_explicit():
    assert isinstance(create_source("http://x/cam.m3u8"), LLHLSSource)
    assert isinstance(create_source("rtsp://x/cam"), HTTPHLSSource)
    assert isinstance(create_source("http://x/cam.m3u8", prefer="http"), HTTPHLSSource)
    assert isinstance(create_source("rtsp://x/cam", prefer="cv2"), LLHLSSource)


def test_frame_source_protocol_is_satisfied():
    assert isinstance(FakeSource(), FrameSource)
    assert isinstance(LLHLSSource("http://x/cam"), FrameSource)
    assert isinstance(HTTPHLSSource("http://x/cam"), FrameSource)

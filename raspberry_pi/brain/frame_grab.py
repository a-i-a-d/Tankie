"""Non-blocking "latest frame" grabber for the Tankie brain (issue #74, Phase 1 T3).

The reflex loop (Phase 2) needs the *newest* camera frame on every cycle and
must **never block** waiting for one. This module provides:

* ``FrameSource``  -- the protocol a frame producer implements (``read()``).
* ``LLHLSSource``  -- primary: a persistent ``cv2.VideoCapture`` on the LL-HLS
  stream (D5), driven by a daemon decode thread that always keeps the newest
  decoded frame.
* ``HTTPHLSSource``-- fallback (Q2, option (a)): a pure-HTTP LL-HLS poller
  (``httpx`` playlist walk -> latest ``.ts`` segment -> ``cv2.imdecode``).
  A different *transport* (plain HTTP vs FFmpeg HLS demux), not a "no-cv2"
  escape hatch -- it still needs ``cv2`` to decode.
* ``FrameGrab``    -- the consumer-facing wrapper: a daemon thread that reads
  the source every ``interval_s`` and keeps ``(frame, monotonic())``;
  ``read_latest()`` returns the stored frame **only if** it is younger than
  ``stale_after_s``, else ``None`` (the reflex loop treats ``None`` as
  "no perception this cycle").

Design rules (from the #74 plan):
  * ``cv2`` and ``httpx`` are imported **lazily inside the worker thread** so
    that importing this module (and the reflex loop) works even when ``cv2``
    is not installed; any ``ImportError`` / decode failure / network failure
    surfaces as ``None`` from ``read()`` -- never an exception to the caller.
  * Both sources keep the **last good frame** so a single dropped segment or
    a momentary network blip does not blank the loop; the staleness guard in
    ``FrameGrab.read_latest()`` is what eventually reports "dead".
  * ``LLHLSSource`` holds a **persistent** reader (mediamtx is
    ``sourceOnDemand`` -- re-opening per call would re-buffer LL-HLS and fight
    the 10 s camera timeout).
  * ``start()`` / ``stop()`` are idempotent; ``read_latest()`` worst-case cost
    is a lock + a timestamp compare (no I/O, no decode).

No LocalAI calls, no bridge traffic (out of scope for T3).
"""

from __future__ import annotations

import re
import threading
import time
from typing import Optional, Protocol, runtime_checkable

# ---------------------------------------------------------------------------
# FrameSource protocol
# ---------------------------------------------------------------------------
@runtime_checkable
class FrameSource(Protocol):
    """A frame producer. ``read()`` returns the newest frame or ``None``."""

    def read(self) -> "Optional[object]":  # np.ndarray | None
        ...


# ---------------------------------------------------------------------------
# LL-HLS via OpenCV (primary, D5)
# ---------------------------------------------------------------------------
class LLHLSSource:
    """Persistent ``cv2.VideoCapture`` on an LL-HLS (or RTSP) URL.

    A daemon decode thread continuously calls ``cap.read()`` and keeps the
    newest frame. ``read()`` returns that frame (or ``None`` before the first
    good frame / after a failure) without blocking the caller.

    ``cv2`` is imported lazily inside the decode thread; if it is missing the
    thread exits immediately and ``read()`` keeps returning ``None``.
    """

    def __init__(self, url: str, timeout_s: float = 2.0) -> None:
        self._url = url
        self._timeout_s = float(timeout_s)
        self._lock = threading.Lock()
        self._frame = None
        self._ts: Optional[float] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # -- lifecycle --------------------------------------------------------
    def start(self) -> None:
        """Start the decode thread (idempotent)."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._decode_loop, name="llhls-decode", daemon=True
            )
            self._thread.start()

    def stop(self) -> None:
        """Stop the decode thread and release the capture (idempotent)."""
        self._stop.set()
        with self._lock:
            t = self._thread
            self._thread = None
        if t is not None:
            t.join(timeout=2.0)

    def close(self) -> None:
        """Alias for :meth:`stop` (context-manager friendly)."""
        self.stop()

    # -- FrameSource ------------------------------------------------------
    def read(self):
        """Return the newest decoded frame, or ``None`` (never blocks > lock)."""
        self.start()
        with self._lock:
            return self._frame

    def age_s(self) -> Optional[float]:
        """Seconds since the last good frame, or ``None`` if none yet."""
        with self._lock:
            if self._ts is None:
                return None
            return max(0.0, time.monotonic() - self._ts)

    # -- internals --------------------------------------------------------
    def _decode_loop(self) -> None:
        try:
            import cv2  # lazy: module import must work without cv2
        except Exception:
            return  # cv2 unavailable -> read() keeps returning None
        cap = None
        try:
            cap = cv2.VideoCapture(self._url)
            while not self._stop.is_set():
                ok, frame = cap.read()
                if ok and frame is not None:
                    with self._lock:
                        self._frame = frame
                        self._ts = time.monotonic()
                else:
                    # momentary failure: keep last good frame; small backoff
                    self._stop.wait(0.05)
        except Exception:
            pass
        finally:
            if cap is not None:
                try:
                    cap.release()
                except Exception:
                    pass


# ---------------------------------------------------------------------------
# LL-HLS via plain HTTP (fallback, Q2 option (a))
# ---------------------------------------------------------------------------
class HTTPHLSSource:
    """Pure-HTTP LL-HLS poller (``httpx`` + ``cv2.imdecode``).

    A daemon poller thread fetches the master playlist (first variant), then
    the media playlist, takes the **last** ``EXTINF`` segment, downloads it,
    and decodes it with ``cv2.imdecode``. Only a *new* segment (different URL
    than the last decoded) is fetched, so the poller is cheap.

    ``httpx`` and ``cv2`` are imported lazily; any failure (missing module,
    network error, bad playlist, undecodable segment) -> ``read()`` returns
    ``None``. The last good frame is kept until the poller produces a newer
    one (the ``FrameGrab`` staleness guard is what reports "dead").
    """

    def __init__(
        self,
        url: str,
        poll_s: float = 0.5,
        timeout_s: float = 2.0,
        *,
        client=None,
    ) -> None:
        self._url = url
        self._poll_s = float(poll_s)
        self._timeout_s = float(timeout_s)
        self._client = client  # injectable httpx.Client (tests)
        self._lock = threading.Lock()
        self._frame = None
        self._ts: Optional[float] = None
        self._last_segment: Optional[str] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # -- lifecycle --------------------------------------------------------
    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._poller, name="http-hls-poll", daemon=True
            )
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            t = self._thread
            self._thread = None
        if t is not None:
            t.join(timeout=2.0)

    def close(self) -> None:
        self.stop()

    # -- FrameSource ------------------------------------------------------
    def read(self):
        self.start()
        with self._lock:
            return self._frame

    def age_s(self) -> Optional[float]:
        with self._lock:
            if self._ts is None:
                return None
            return max(0.0, time.monotonic() - self._ts)

    # -- internals --------------------------------------------------------
    def _client_or_none(self):
        if self._client is not None:
            return self._client
        try:
            import httpx  # lazy
        except Exception:
            return None
        return httpx.Client(timeout=self._timeout_s)

    def _poller(self) -> None:
        client = self._client_or_none()
        if client is None:
            return
        try:
            while not self._stop.is_set():
                try:
                    seg = self._latest_segment(client)
                    if seg is not None and seg != self._last_segment:
                        frame = self._decode(client, seg)
                        if frame is not None:
                            with self._lock:
                                self._frame = frame
                                self._ts = time.monotonic()
                            self._last_segment = seg
                except Exception:
                    pass  # network / playlist error: keep last good frame
                self._stop.wait(self._poll_s)
        finally:
            if self._client is None:
                try:
                    client.close()
                except Exception:
                    pass

    def _latest_segment(self, client) -> Optional[str]:
        """Resolve the master -> media playlist -> last segment URL."""
        from urllib.parse import urljoin

        master = client.get(self._url, timeout=self._timeout_s)
        if master.status_code >= 400:
            return None
        master_text = master.text
        m = re.search(r"#EXTSTREAM-INF[^#]*\n([^\n\s]+)", master_text)
        if m:
            base = urljoin(self._url, m.group(1))
        else:
            base = self._url  # already a media playlist

        media = client.get(base, timeout=self._timeout_s)
        if media.status_code >= 400:
            return None
        segments = [
            line.strip()
            for line in media.text.splitlines()
            if line.strip() and not line.startswith("#")
        ]
        if not segments:
            return None
        return urljoin(base, segments[-1])

    def _decode(self, client, segment_url: str):
        """Fetch the segment and decode it to a BGR ``np.ndarray`` (or None)."""
        resp = client.get(segment_url, timeout=self._timeout_s)
        if resp.status_code >= 400:
            return None
        data = resp.content
        if not data:
            return None
        try:
            import cv2  # lazy
            import numpy as np  # lazy
        except Exception:
            return None
        try:
            buf = np.frombuffer(data, dtype=np.uint8)
            arr = cv2.imdecode(buf, cv2.IMREAD_COLOR)
        except Exception:
            try:
                arr = cv2.imdecode(data, cv2.IMREAD_COLOR)  # older cv2 accepts bytes
            except Exception:
                return None
        return arr


# ---------------------------------------------------------------------------
# FrameGrab -- the consumer-facing wrapper
# ---------------------------------------------------------------------------
class FrameGrab:
    """Read ``source`` on a daemon thread; serve the latest *fresh* frame.

    ``read_latest()`` returns the stored frame only if it is younger than
    ``stale_after_s`` (monotonic), else ``None``. A source that returns
    ``None`` keeps the last good frame until it goes stale -- a single dropped
    segment does not blank the reflex loop.

    ``start()`` / ``stop()`` are idempotent. ``read_latest()`` worst-case cost
    is a lock + a timestamp compare (no I/O, no decode).
    """

    def __init__(
        self,
        source: FrameSource,
        interval_s: float = 0.2,
        stale_after_s: float = 3.0,
    ) -> None:
        self._source = source
        self._interval_s = max(0.01, float(interval_s))
        self._stale_after_s = max(0.0, float(stale_after_s))
        self._lock = threading.Lock()
        self._frame = None
        self._ts: Optional[float] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # -- lifecycle --------------------------------------------------------
    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._loop, name="frame-grab", daemon=True
            )
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            t = self._thread
            self._thread = None
        if t is not None:
            t.join(timeout=2.0)

    # -- consumer API -----------------------------------------------------
    def read_latest(self):
        """Newest frame if fresh (``< stale_after_s`` old), else ``None``."""
        with self._lock:
            if self._frame is None or self._ts is None:
                return None
            if (time.monotonic() - self._ts) > self._stale_after_s:
                return None
            return self._frame

    def age_s(self) -> Optional[float]:
        """Seconds since the last good frame, or ``None`` if none yet."""
        with self._lock:
            if self._ts is None:
                return None
            return max(0.0, time.monotonic() - self._ts)

    # -- internals --------------------------------------------------------
    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                frame = self._source.read()
            except Exception:
                frame = None
            if frame is not None:
                with self._lock:
                    self._frame = frame
                    self._ts = time.monotonic()
            self._stop.wait(self._interval_s)


# ---------------------------------------------------------------------------
# factory
# ---------------------------------------------------------------------------
def build_frame_grab(config, *, prefer: str = "auto", client=None) -> "FrameGrab":
    """Build a :class:`FrameGrab` from a T1 ``BrainConfig`` (issue #74 / T3).

    Wires the configured ``cam_url`` to a :class:`FrameSource` (via
    :func:`create_source`) and applies ``grab_interval_s`` /
    ``grab_stale_after_s`` (and the HTTP poll/timeout for the fallback). This
    is the single place the reflex loop (Phase 2) constructs the grabber, so
    all knobs come from the config source (D10/D19) -- never literals.
    """
    source = create_source(
        config.cam_url,
        prefer=prefer,
        poll_s=getattr(config, "grab_http_poll_s", 0.5),
        timeout_s=getattr(config, "grab_http_timeout_s", 2.0),
        client=client,
    )
    return FrameGrab(
        source,
        interval_s=getattr(config, "grab_interval_s", 0.2),
        stale_after_s=getattr(config, "grab_stale_after_s", 3.0),
    )


def create_source(
    url: str,
    *,
    prefer: str = "auto",
    poll_s: float = 0.5,
    timeout_s: float = 2.0,
    client=None,
) -> FrameSource:
    """Pick a :class:`FrameSource` for ``url``.

    ``prefer`` is ``"auto"`` (LL-HLS -> ``LLHLSSource``, RTSP ->
    ``HTTPHLSSource``), ``"cv2"`` (force ``LLHLSSource``), or ``"http"``
    (force ``HTTPHLSSource``). The choice is a config decision (D5 / Q2), not
    a code branch in the reflex loop.
    """
    scheme = url.split("://", 1)[0].lower() if "://" in url else ""
    if prefer == "http":
        return HTTPHLSSource(url, poll_s=poll_s, timeout_s=timeout_s, client=client)
    if prefer == "cv2":
        return LLHLSSource(url, timeout_s=timeout_s)
    # auto
    if scheme == "rtsp":
        return HTTPHLSSource(url, poll_s=poll_s, timeout_s=timeout_s, client=client)
    return LLHLSSource(url, timeout_s=timeout_s)

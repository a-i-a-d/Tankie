"""Camera streaming adapter (mediamtx) — HTTP/HLS/RTSP/API probes.

Uses only the standard library (``urllib``) so it needs no extra dependency.
Probes the mediamtx endpoints declared in ``raspberry_pi/conf/mediamtx.yml``:

    WebRTC  http://<host>:8889/<path>/      (HTML player page)
    LL-HLS  http://<host>:8888/<path>/index.m3u8
    RTSP    rtsp://<host>:8554/<path>       (checked via ffprobe if present)
    API     http://127.0.0.1:9997/v3/paths/list   (loopback only)

Config keys:
    host      — the Pi's LAN IP/hostname (default ``tankie.local``)
    webrtc_port (8889) / hls_port (8888) / rtsp_port (8554) / api_port (9997)
    path      — the mediamtx path (default ``cam``)
    timeout_s — per-request timeout, default 3.0
"""

from __future__ import annotations

import json
import shutil
import subprocess
import urllib.request
import urllib.error
from typing import Any, List, Mapping, Tuple

from .base import Adapter

DEFAULT_HOST = "tankie.local"
DEFAULT_TIMEOUT_S = 3.0


class CameraAdapter(Adapter):
    name = "camera"

    def __init__(self, cfg: Mapping[str, Any] | None = None):
        super().__init__(cfg)
        self.host = self.cfg.get("host", DEFAULT_HOST)
        self.webrtc_port = int(self.cfg.get("webrtc_port", 8889))
        self.hls_port = int(self.cfg.get("hls_port", 8888))
        self.rtsp_port = int(self.cfg.get("rtsp_port", 8554))
        self.api_port = int(self.cfg.get("api_port", 9997))
        self.path = self.cfg.get("path", "cam")
        self.timeout_s = float(self.cfg.get("timeout_s", DEFAULT_TIMEOUT_S))

    def _http_get(self, url: str) -> Tuple[int, str, str]:
        """Return (status, body, error). status 0 = connection failure."""
        try:
            with urllib.request.urlopen(url, timeout=self.timeout_s) as r:
                body = r.read(65536).decode("utf-8", errors="replace")
                return r.status, body, ""
        except urllib.error.HTTPError as e:
            return e.code, "", ""
        except Exception as exc:  # noqa: BLE001 - report, don't crash
            return 0, "", str(exc)

    def _ffprobe_rtsp(self) -> Tuple[bool, str]:
        ffprobe = shutil.which("ffprobe")
        if not ffprobe:
            return False, "ffprobe not installed (RTSP probe skipped)"
        url = f"rtsp://{self.host}:{self.rtsp_port}/{self.path}"
        try:
            out = subprocess.run(
                [ffprobe, "-v", "error", "-show_streams", "-of", "json", url],
                capture_output=True, text=True, timeout=self.timeout_s + 2,
            )
            if out.returncode != 0:
                return False, out.stderr.strip()[:300] or "ffprobe failed"
            try:
                streams = json.loads(out.stdout).get("streams", [])
            except ValueError:
                return True, "ffprobe OK (unparsed)"
            if not streams:
                return False, "no streams"
            s = streams[0]
            codec = s.get("codec_name", "?")
            w = s.get("width", "?")
            h = s.get("height", "?")
            return True, f"{codec} {w}x{h}"
        except subprocess.TimeoutExpired:
            return False, "ffprobe timed out"
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)

    # -- Adapter interface --------------------------------------------------
    def run(self, cmd: Mapping[str, Any]) -> dict:
        """``cmd`` may carry ``probe`` = webrtc|hls|rtsp|api to pick one,
        otherwise probe them all (cheap, read-only)."""
        which = (cmd.get("probe") or "all").lower()
        result: dict = {"reply": {"ok": True, "probes": {}}}
        probes = result["reply"]["probes"]

        if which in ("all", "webrtc"):
            url = f"http://{self.host}:{self.webrtc_port}/{self.path}/"
            status, body, err = self._http_get(url)
            probes["webrtc"] = {"url": url, "status": status,
                                "has_player": "html" in body.lower() or status == 200,
                                "error": err}
        if which in ("all", "hls"):
            url = f"http://{self.host}:{self.hls_port}/{self.path}/index.m3u8"
            status, body, err = self._http_get(url)
            probes["hls"] = {"url": url, "status": status,
                             "is_playlist": "#EXTM3U" in body.upper(),
                             "error": err}
        if which in ("all", "rtsp"):
            ok, detail = self._ffprobe_rtsp()
            probes["rtsp"] = {"ok": ok, "detail": detail}
        if which in ("all", "api"):
            url = f"http://127.0.0.1:{self.api_port}/v3/paths/list"
            status, body, err = self._http_get(url)
            paths = []
            if status == 200:
                try:
                    paths = json.loads(body).get("paths", [])
                except ValueError:
                    paths = []
            probes["api"] = {"url": url, "status": status, "paths": paths, "error": err}

        # Overall ok = every requested probe reported a 200 / ok.
        overall = True
        for name, p in probes.items():
            if name == "rtsp":
                overall = overall and bool(p.get("ok"))
            else:
                overall = overall and (p.get("status") == 200)
        result["reply"]["ok"] = overall
        return result

    def preflight(self) -> List[Tuple[str, bool, str]]:
        url = f"http://{self.host}:{self.webrtc_port}/{self.path}/"
        status, _, err = self._http_get(url)
        return [("mediamtx WebRTC reachable", status == 200,
                 f"{url} -> {status}" + (f" ({err})" if err else ""))]

    def describe(self) -> str:
        return ("mediamtx camera streaming — WebRTC / LL-HLS / RTSP / API "
                "probes (read-only)")

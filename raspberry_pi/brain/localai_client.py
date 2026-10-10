"""Thin, config-driven LocalAI client for the Tankie brain (issue #73, Phase 1 T2).

One uniform contract for every method:

    * success -> ``{"ok": True, ...}``
    * failure -> ``{"ok": False, "error": <str>}``

and **no method ever raises**. The reflex/deliberation loops (Phase 2) are the
only callers and must be able to treat a dead/timing-out LocalAI server as
*data*, not an exception (safety invariant 4: server down => stop).

Design rules (from the #73 plan):
  * base URL, models, and per-call timeouts all come from T1 ``BrainConfig`` —
    **no IP addresses or model-name literals in this module** (A4 / acceptance).
  * ``chat()`` goes through the ``openai`` SDK (already in requirements);
    everything else goes through ``httpx``.
  * Both the ``httpx`` transport and the ``openai`` client are **injectable**
    so the offline test suite can use ``httpx.MockTransport`` + a fake
    ``openai.OpenAI`` and assert the exact request shapes.
  * A single ``_request`` helper wraps every ``httpx`` call in one
    ``try/except`` covering ``httpx.TimeoutException``, ``httpx.HTTPError``,
    ``json.JSONDecodeError``, and non-2xx -> ``{"ok": False, "error": ...}``.

Endpoints (LocalAI, OpenAI-compatible where noted):
  * ``chat()``        -> ``POST /v1/chat/completions``   (openai SDK)
  * ``detect()``      -> ``POST /v1/detection``          (LocalAI)
  * ``depth()``       -> ``POST /v1/depth``              (STUB, D4 / T15)
  * ``transcribe()``  -> ``POST /v1/audio/transcriptions`` (OpenAI)
  * ``speak()``       -> ``POST /v1/audio/speech``         (OpenAI)
  * ``health()``      -> ``GET  /v1/models``               (liveness probe)
"""

from __future__ import annotations

import json
from typing import Any, Mapping, Optional, Sequence

import httpx

from .config import BrainConfig

# The documented depth stub error (D4: depth is not enabled until T15).
_DEPTH_STUB_ERROR = "depth not enabled (T15)"

# NOTE: no model names live in this module. Every model is read from
# ``BrainConfig`` (``vlm_model`` / ``detection_model`` / ``stt_model`` /
# ``tts_model``), which is the single config source (D10/D19) -- so switching
# models is a config edit, never a code edit.



def _err(message: str) -> dict:
    """Uniform failure shape."""
    return {"ok": False, "error": str(message)}


class LocalAIClient:
    """One typed surface for every LocalAI endpoint the brain uses.

    Parameters
    ----------
    config:
        A T1 ``BrainConfig`` (base URL, models, timeouts). All request
        parameters are read from here -- never from literals.
    transport:
        Optional ``httpx`` transport (inject ``httpx.MockTransport`` in tests).
    openai_client:
        Optional pre-built ``openai.OpenAI`` (inject a fake in tests). When
        omitted, one is constructed from ``config.localai_base_url``.
    """

    def __init__(
        self,
        config: BrainConfig,
        *,
        transport: Optional[httpx.BaseTransport] = None,
        openai_client: Optional[Any] = None,
    ) -> None:
        self._cfg = config
        # httpx client for the non-OpenAI endpoints. base_url already carries
        # the ``/v1`` prefix (e.g. ``http://localai.local:8080/v1``); we pass
        # relative refs (``"detection"``, ``"audio/speech"``) which httpx
        # resolves against it.
        self._http = httpx.Client(
            base_url=config.localai_base_url,
            headers={"Authorization": f"Bearer {config.localai_api_key}"},
            timeout=config.timeouts.chat_s,  # per-call timeout overrides below
            transport=transport,
        )
        # openai client for chat(). Injected clients (tests) are used as-is;
        # otherwise a real one is constructed lazily on the first chat() call so
        # that constructing this class has no side effects and never touches the
        # network.
        self._openai = openai_client

    # ------------------------------------------------------------------
    # internal helpers
    # ------------------------------------------------------------------
    def _timeout(self, name: str) -> float:
        """Resolve a per-endpoint timeout from config (seconds)."""
        return float(getattr(self._cfg.timeouts, name, self._cfg.timeouts.chat_s))

    def _request(
        self,
        method: str,
        path: str,
        *,
        timeout: float,
        json_body: Optional[Mapping] = None,
        files: Optional[Mapping] = None,
        data: Optional[Mapping] = None,
        headers: Optional[Mapping] = None,
    ) -> dict:
        """One httpx call wrapped so it *never* raises.

        Returns ``{"ok": True, "http_status": int, "json": <parsed|None>,
        "bytes": <bytes|None>}`` on success (2xx), or ``{"ok": False,
        "error": ...}`` on any failure (timeout / connect / non-2xx / bad
        JSON). Callers decide how to interpret the payload.
        """
        try:
            resp = self._http.request(
                method,
                path,
                json=json_body,
                files=files,
                data=data,
                headers=headers,
                timeout=timeout,
            )
        except httpx.TimeoutException as exc:
            return _err(f"timeout after {timeout}s: {exc}")
        except httpx.HTTPError as exc:
            return _err(f"http error: {exc}")

        if resp.status_code >= 400:
            detail = _extract_error_detail(resp)
            return _err(
                f"HTTP {resp.status_code} from {method} {path}"
                + (f": {detail}" if detail else "")
            )

        content = resp.content
        # Try to parse JSON (most endpoints); keep raw bytes for audio.
        parsed: Optional[Any] = None
        if content:
            try:
                parsed = json.loads(content.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                parsed = None  # binary (e.g. audio) -> leave as bytes
        return {
            "ok": True,
            "http_status": resp.status_code,
            "json": parsed,
            "bytes": content,
        }

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------
    def health(self) -> dict:
        """Cheap liveness probe: ``GET /v1/models``.

        Returns ``{"ok": True, "models": [str, ...]}`` or an error dict.
        """
        res = self._request("GET", "models", timeout=self._timeout("chat_s"))
        if not res["ok"]:
            return res
        data = res.get("json")
        if isinstance(data, dict) and isinstance(data.get("data"), list):
            models = [m.get("id") for m in data["data"] if isinstance(m, dict) and m.get("id")]
            return {"ok": True, "models": models}
        if isinstance(data, list):
            return {"ok": True, "models": [str(m) for m in data]}
        # 2xx but unexpected shape -> treat as data, not a crash
        return {"ok": True, "models": []}

    def chat(
        self,
        messages: Sequence[Mapping],
        images_b64: Optional[Sequence[str]] = None,
        task: str = "",
    ) -> dict:
        """One vision-language-model call: ``POST /v1/chat/completions``.

        ``messages`` is a list of ``{"role": ..., "content": ...}`` dicts.
        ``images_b64`` (optional) is appended to the *last* user message as
        image_url content parts (OpenAI vision shape). ``task`` is a short
        label recorded in the result (for the data log), not sent to the model.

        Returns ``{"ok": True, "raw": str, "model": str, "task": str}`` or an
        error dict. Never raises.
        """
        if self._openai is None:
            self._openai, import_error = _make_openai_client(
                config=self._cfg,
            )
            if self._openai is None:
                return _err(f"openai client unavailable: {import_error}")

        msgs = _build_chat_messages(messages, images_b64)
        try:
            resp = self._openai.chat.completions.create(
                model=self._cfg.vlm_model,
                messages=msgs,
                response_format={"type": "json_object"},
                timeout=self._timeout("chat_s"),
            )
        except Exception as exc:  # SDK raises many types; map them all
            return _err(f"chat failed: {type(exc).__name__}: {exc}")

        try:
            raw = resp.choices[0].message.content or ""
        except (AttributeError, IndexError, TypeError):
            return _err("chat response missing choices[0].message.content")
        return {"ok": True, "raw": raw, "model": self._cfg.vlm_model, "task": task}

    def detect(self, image_b64: str, min_confidence: float = 0.5) -> dict:
        """Object detection: ``POST /v1/detection``.

        Sends ``{model, image, threshold}`` and maps LocalAI's
        ``{x, y, width, height, class_name, confidence, mask?}`` items into the
        brain ``Detection`` shape ``{class_name, x, y, w, h, confidence}``,
        dropping ``mask`` and filtering ``confidence >= min_confidence``.

        Returns ``{"ok": True, "detections": [...]}`` or an error dict (e.g.
        the ``501`` when no detector model is deployed yet). Never raises.
        """
        body = {
            "model": self._cfg.detection_model,
            "image": image_b64,
            "threshold": min_confidence,
        }
        res = self._request("POST", "detection", timeout=self._timeout("detect_s"), json_body=body)
        if not res["ok"]:
            return res
        data = res.get("json")
        if not isinstance(data, dict) or not isinstance(data.get("detections"), list):
            return _err("detection response missing 'detections' list")
        out = []
        for d in data["detections"]:
            if not isinstance(d, dict):
                continue
            conf = float(d.get("confidence", 0.0) or 0.0)
            if conf < min_confidence:
                continue
            out.append(
                {
                    "class_name": str(d.get("class_name", "")),
                    "x": float(d.get("x", 0.0) or 0.0),
                    "y": float(d.get("y", 0.0) or 0.0),
                    "w": float(d.get("width", 0.0) or 0.0),
                    "h": float(d.get("height", 0.0) or 0.0),
                    "confidence": conf,
                }
            )
        return {"ok": True, "detections": out}

    def depth(self, image_b64: str) -> dict:
        """Depth estimate: ``POST /v1/depth``.

        STUB (D4): depth is not enabled until T15. The real call would be
        ``self._request("POST", "depth", json_body={"model": ..., "image":
        image_b64})`` and a parse of the per-pixel depth map -- that is where the
        implementation goes when a depth model (e.g. depth-anything) is deployed.
        """
        return _err(_DEPTH_STUB_ERROR)

    def transcribe(self, audio_bytes: bytes, filename: str = "audio.wav") -> dict:
        """Speech-to-text: ``POST /v1/audio/transcriptions`` (multipart).

        Returns ``{"ok": True, "text": str}`` or an error dict. Never raises.
        """
        files = {"file": (filename, audio_bytes, "application/octet-stream")}
        data = {"model": self._cfg.stt_model}
        res = self._request(
            "POST", "audio/transcriptions", timeout=self._timeout("transcribe_s"),
            files=files, data=data,
        )
        if not res["ok"]:
            return res
        parsed = res.get("json")
        if isinstance(parsed, dict) and "text" in parsed:
            return {"ok": True, "text": str(parsed["text"])}
        return _err("transcription response missing 'text'")

    def speak(self, text: str, voice: Optional[str] = None, format: str = "wav") -> dict:
        """Text-to-speech: ``POST /v1/audio/speech``.

        Returns ``{"ok": True, "audio": bytes, "format": str}`` or an error
        dict. Never raises.
        """
        body: dict = {
            "model": self._cfg.tts_model,
            "input": text,
            "voice": voice or "default",
            "response_format": format,
        }
        res = self._request("POST", "audio/speech", timeout=self._timeout("speak_s"), json_body=body)
        if not res["ok"]:
            return res
        # A real TTS response is raw audio (binary). If the body parsed as JSON
        # it is not audio (e.g. a 200 error payload) -> treat as a failure.
        if res.get("json") is not None:
            return _err("speech response was JSON, not audio bytes")
        audio = res.get("bytes")
        if not audio:
            return _err("speech response had no audio bytes")
        return {"ok": True, "audio": audio, "format": format}

    def close(self) -> None:
        """Close the underlying httpx client."""
        try:
            self._http.close()
        except Exception:
            pass


# ----------------------------------------------------------------------
# module-level helpers
# ----------------------------------------------------------------------
def _make_openai_client(*, config: BrainConfig):
    """Lazily construct a real ``openai.OpenAI`` from config (no network yet).

    Returns ``(client, None)`` on success or ``(None, error_str)`` on failure.
    """
    try:
        from openai import OpenAI  # local import: keep module import light
        client = OpenAI(
            base_url=config.localai_base_url,
            api_key=config.localai_api_key,
            timeout=config.timeouts.chat_s,
        )
        return client, None
    except Exception as exc:
        return None, str(exc)


def _extract_error_detail(resp: httpx.Response) -> str:
    """Best-effort human-readable detail from an error response body."""
    try:
        data = json.loads(resp.content.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return ""
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict):
            return str(err.get("message") or err.get("code") or "")
        if isinstance(err, str):
            return err
        if isinstance(data.get("message"), str):
            return data["message"]
    return ""


def _build_chat_messages(
    messages: Sequence[Mapping],
    images_b64: Optional[Sequence[str]],
) -> list:
    """Convert brain messages (+ optional images) to OpenAI chat shape.

    If ``images_b64`` is provided, they are attached to the last user message
    as ``image_url`` content parts (data URIs). A plain string content becomes
    a list of parts so text + images coexist.
    """
    msgs = [dict(m) for m in messages]
    if images_b64:
        # find the last user message to attach images to
        idx = None
        for i in range(len(msgs) - 1, -1, -1):
            if msgs[i].get("role") == "user":
                idx = i
                break
        if idx is None:
            idx = len(msgs) - 1
        parts: list = []
        existing = msgs[idx].get("content")
        if isinstance(existing, str):
            parts.append({"type": "text", "text": existing})
        elif isinstance(existing, list):
            parts.extend(existing)
        for b64 in images_b64:
            parts.append({"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}})
        msgs[idx] = {**msgs[idx], "content": parts}
    return msgs

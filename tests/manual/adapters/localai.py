"""LocalAI adapter — health + one chat/detect call (issue #73).

Uses only the standard library (``urllib``). Probes the LocalAI OpenAI-compatible
API declared in ``raspberry_pi/conf/brain.yaml`` / the env. No model name or
IP is hard-coded here (A4): it reads them from config.

Config keys:
    base_url  — default ``http://localai.local:8080/v1``
    api_key   — default ``sk-0123456789`` (LocalAI accepts any key)
    vlm_model — a vision model name for the chat probe (optional)
    timeout_s — per-request timeout, default 5.0
"""

from __future__ import annotations

import json
import urllib.request
import urllib.error
from typing import Any, List, Mapping, Tuple

from .base import Adapter

DEFAULT_BASE_URL = "http://localai.local:8080/v1"
DEFAULT_API_KEY = "sk-0123456789"
DEFAULT_TIMEOUT_S = 5.0


class LocalAIAdapter(Adapter):
    name = "localai"

    def __init__(self, cfg: Mapping[str, Any] | None = None):
        super().__init__(cfg)
        self.base_url = self.cfg.get("base_url", DEFAULT_BASE_URL).rstrip("/")
        self.api_key = self.cfg.get("api_key", DEFAULT_API_KEY)
        self.vlm_model = self.cfg.get("vlm_model")
        self.timeout_s = float(self.cfg.get("timeout_s", DEFAULT_TIMEOUT_S))

    def _get(self, path: str) -> Tuple[int, dict, str]:
        url = f"{self.base_url}{path}"
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {self.api_key}"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as r:
                body = r.read().decode("utf-8", errors="replace")
                return r.status, (json.loads(body) if body else {}), ""
        except urllib.error.HTTPError as e:
            return e.code, {}, ""
        except Exception as exc:  # noqa: BLE001
            return 0, {}, str(exc)

    def _post(self, path: str, payload: dict) -> Tuple[int, dict, str]:
        url = f"{self.base_url}{path}"
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode(),
            headers={"Authorization": f"Bearer {self.api_key}",
                     "Content-Type": "application/json"},
            method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as r:
                body = r.read().decode("utf-8", errors="replace")
                return r.status, (json.loads(body) if body else {}), ""
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read().decode("utf-8", "replace")), ""
            except Exception:  # noqa: BLE001
                return e.code, {}, ""
        except Exception as exc:  # noqa: BLE001
            return 0, {}, str(exc)

    # -- Adapter interface --------------------------------------------------
    def run(self, cmd: Mapping[str, Any]) -> dict:
        """``cmd`` may carry ``probe`` = models|health|chat|detect (default all)."""
        which = (cmd.get("probe") or "all").lower()
        result: dict = {"reply": {"ok": True, "probes": {}}}
        probes = result["reply"]["probes"]

        if which in ("all", "models", "health"):
            status, body, err = self._get("/models")
            ids = [m.get("id") for m in body.get("data", [])] if isinstance(body, dict) else []
            probes["models"] = {"status": status, "models": ids, "error": err}
            if status != 200:
                result["reply"]["ok"] = False

        if which in ("all", "chat") and self.vlm_model:
            status, body, err = self._post("/chat/completions", {
                "model": self.vlm_model,
                "messages": [{"role": "user", "content": "Reply with exactly: OK"}],
                "max_tokens": 8,
            })
            content = ""
            try:
                content = body["choices"][0]["message"].get("content") or ""
            except (KeyError, IndexError, TypeError):
                pass
            probes["chat"] = {"status": status, "content": content[:200], "error": err}
            if status != 200:
                result["reply"]["ok"] = False

        if which in ("all", "detect") and self.vlm_model:
            # A detection call needs an image; skip if none supplied.
            image = cmd.get("image_b64")
            if image:
                status, body, err = self._post("/chat/completions", {
                    "model": self.vlm_model,
                    "messages": [{"role": "user", "content": [
                        {"type": "text", "text": "List objects in this image as JSON."},
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image}"}},
                    ]}],
                    "max_tokens": 200,
                })
                probes["detect"] = {"status": status, "error": err}
                if status != 200:
                    result["reply"]["ok"] = False
            else:
                probes["detect"] = {"status": 0, "skipped": "no image_b64 supplied"}

        return result

    def preflight(self) -> List[Tuple[str, bool, str]]:
        status, body, err = self._get("/models")
        n = len(body.get("data", [])) if isinstance(body, dict) else 0
        return [("LocalAI /v1/models reachable", status == 200,
                 f"{self.base_url}/models -> {status} ({n} models)"
                 + (f" ({err})" if err else ""))]

    def describe(self) -> str:
        return ("LocalAI OpenAI-compatible API — health + models + one chat "
                "call (read-mostly, one cheap completion)")

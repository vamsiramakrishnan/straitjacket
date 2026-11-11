"""Model providers with thread-local HTTP keep-alive pooling for AccioBench.

Supports two pluggable backends with untouched model defaults by default
(neither ``thinkingConfig`` nor ``maxOutputTokens`` is sent unless explicitly
configured on :class:`BenchConfig`):

- ``gemini``: Google Gemini Developer API (``generativelanguage.googleapis.com/v1beta``)
  authenticated via ``GEMINI_API_KEY`` / ``--api-key``.
- ``vertex``: Google Cloud Vertex AI (``aiplatform.googleapis.com/v1``)
  authenticated via Application Default Credentials with thread-safe token
  caching and proactive pre-expiry refresh.
"""

from __future__ import annotations

from collections.abc import Callable
import http.client
import json
import os
import ssl
import subprocess
import threading
import time
from typing import Any, Protocol, runtime_checkable
import urllib.parse

from .config import BenchConfig

__all__ = [
    "AdcTokenCache",
    "GeminiProvider",
    "ModelProvider",
    "VertexProvider",
    "create_provider",
]

_DEFAULT_HTTP_TIMEOUT_S = 600.0
_MAX_GENERATE_ATTEMPTS = 6
_RETRY_BACKOFF_S = 5.0
_RETRYABLE_STATUSES = frozenset({408, 429, 500, 502, 503, 504})
_ERR_SNIPPET_LIMIT = 320

_ADC_PROACTIVE_REFRESH_S = 2400.0
_ADC_HARD_MAX_AGE_S = 3300.0
_ADC_FETCH_RETRIES = 4
_ADC_FETCH_TIMEOUT_S = 30.0
_ADC_FETCH_BACKOFF_S = 0.8

_GEMINI_API_HOST = "generativelanguage.googleapis.com"
_VERTEX_GLOBAL_HOST = "aiplatform.googleapis.com"


@runtime_checkable
class ModelProvider(Protocol):
    """Protocol implemented by pluggable LLM generation backends."""

    def generate(
        self,
        model_id: str,
        contents: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        session_id: str | None = None,
        system_instruction: str | None = None,
        tool_config: dict[str, Any] | None = None,
    ) -> dict[str, Any]: ...

    def judge_access_token(self) -> str: ...

    def close(self) -> None: ...


class AdcTokenCache:
    """Thread-safe GCP access-token cache with proactive pre-expiry refresh."""

    def __init__(
        self,
        *,
        proactive_refresh_s: float = _ADC_PROACTIVE_REFRESH_S,
        hard_max_age_s: float = _ADC_HARD_MAX_AGE_S,
        fetcher: Callable[[], str] | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self._proactive_refresh_s = float(proactive_refresh_s)
        self._hard_max_age_s = float(hard_max_age_s)
        self._fetcher = fetcher
        self._clock = clock
        self._sleeper = sleeper
        self._lock = threading.Lock()
        self._token: str = ""
        self._fetched_at: float = 0.0
        self._ignore_env_token: bool = False

    def _default_fetch(self) -> str:
        if not self._ignore_env_token:
            env_tok = os.environ.get("VERTEX_ACCESS_TOKEN", "").strip()
            if env_tok:
                return env_tok

        commands = (
            ["gcloud", "auth", "application-default", "print-access-token"],
            ["gcloud", "auth", "print-access-token"],
        )
        last_exc: Exception | None = None
        for cmd in commands:
            try:
                tok = (
                    subprocess.check_output(
                        cmd,
                        stderr=subprocess.PIPE,
                        timeout=_ADC_FETCH_TIMEOUT_S,
                    )
                    .decode("utf-8")
                    .strip()
                )
                if tok:
                    return tok
            except Exception as exc:
                last_exc = exc
        if last_exc is not None:
            raise last_exc
        raise RuntimeError("gcloud returned an empty access token")

    def get_token(self) -> str:
        """Return a valid bearer token, refreshing proactively before expiry."""
        with self._lock:
            now = self._clock()
            if self._token and (now - self._fetched_at) < self._proactive_refresh_s:
                return self._token

            fetch_fn = self._fetcher or self._default_fetch
            last_exc: Exception | None = None
            for attempt in range(1, _ADC_FETCH_RETRIES + 1):
                try:
                    tok = fetch_fn().strip()
                    if tok:
                        self._token = tok
                        self._fetched_at = self._clock()
                        return tok
                except Exception as exc:
                    last_exc = exc
                    if attempt < _ADC_FETCH_RETRIES:
                        self._sleeper(_ADC_FETCH_BACKOFF_S * attempt)

            # Fall back to cached token if still safely within its 1-hour lifetime.
            if self._token and (self._clock() - self._fetched_at) < self._hard_max_age_s:
                return self._token
            if last_exc is not None:
                raise RuntimeError(f"Failed to obtain GCP ADC access token: {last_exc}") from last_exc
            raise RuntimeError("Failed to obtain GCP ADC access token")

    def invalidate(self) -> None:
        """Clear cached token (e.g. after an HTTP 401 response)."""
        with self._lock:
            self._token = ""
            self._fetched_at = 0.0
            self._ignore_env_token = True


class _KeepAliveConnectionPool:
    """Thread-local ``HTTPSConnection`` keep-alive pool with cross-thread close."""

    def __init__(
        self,
        *,
        timeout: float = _DEFAULT_HTTP_TIMEOUT_S,
        conn_factory: Callable[[str, float], http.client.HTTPSConnection] | None = None,
    ) -> None:
        self._timeout = float(timeout)
        self._conn_factory = conn_factory
        self._local = threading.local()
        self._lock = threading.Lock()
        self._all_conns: set[http.client.HTTPSConnection] = set()

    def _thread_map(self) -> dict[str, http.client.HTTPSConnection]:
        conns = getattr(self._local, "conns", None)
        if conns is None:
            conns = {}
            self._local.conns = conns
        return conns

    def _open_conn(self, host: str) -> http.client.HTTPSConnection:
        if self._conn_factory is not None:
            conn = self._conn_factory(host, self._timeout)
        else:
            conn = http.client.HTTPSConnection(host, timeout=self._timeout)
        with self._lock:
            self._all_conns.add(conn)
        return conn

    def _discard_conn(self, host: str, conn: http.client.HTTPSConnection) -> None:
        tmap = self._thread_map()
        if tmap.get(host) is conn:
            tmap.pop(host, None)
        with self._lock:
            self._all_conns.discard(conn)
        try:
            conn.close()
        except Exception:
            pass

    def _acquire(self, host: str) -> tuple[http.client.HTTPSConnection, bool]:
        tmap = self._thread_map()
        existing = tmap.get(host)
        if existing is not None:
            reused = getattr(existing, "sock", None) is not None
            return existing, reused
        conn = self._open_conn(host)
        tmap[host] = conn
        return conn, False

    def post_json(
        self,
        host: str,
        path: str,
        body: bytes,
        headers: dict[str, str],
    ) -> tuple[int, str]:
        """Send an HTTP/1.1 keep-alive POST and return ``(status_code, response_text)``."""
        req_headers = {
            "Content-Type": "application/json",
            "Connection": "keep-alive",
            **headers,
        }
        conn, was_reused = self._acquire(host)
        try:
            conn.request("POST", path, body=body, headers=req_headers)
            resp = conn.getresponse()
            raw = resp.read()
            status = int(resp.status)
            if getattr(resp, "will_close", False):
                self._discard_conn(host, conn)
            return status, raw.decode("utf-8", errors="replace")
        except (http.client.HTTPException, ConnectionError, ssl.SSLError, OSError):
            self._discard_conn(host, conn)
            if not was_reused:
                raise

        # Stale keep-alive socket closed by peer while idle — reconnect once immediately.
        fresh_conn, _ = self._acquire(host)
        try:
            fresh_conn.request("POST", path, body=body, headers=req_headers)
            resp = fresh_conn.getresponse()
            raw = resp.read()
            status = int(resp.status)
            if getattr(resp, "will_close", False):
                self._discard_conn(host, fresh_conn)
            return status, raw.decode("utf-8", errors="replace")
        except Exception:
            self._discard_conn(host, fresh_conn)
            raise

    def close(self) -> None:
        """Close all pooled keep-alive sockets across all worker threads."""
        with self._lock:
            conns = list(self._all_conns)
            self._all_conns.clear()
        tmap = getattr(self._local, "conns", None)
        if isinstance(tmap, dict):
            tmap.clear()
        for conn in conns:
            try:
                conn.close()
            except Exception:
                pass


def _build_generation_config(
    thinking_level: str | None,
    max_output_tokens: int | None,
) -> dict[str, Any] | None:
    """Build optional ``generationConfig``; returns ``None`` when untouched defaults apply."""
    gen_cfg: dict[str, Any] = {}
    if thinking_level is not None and str(thinking_level).strip():
        lvl = str(thinking_level).strip()
        if lvl.isdigit():
            gen_cfg["thinkingConfig"] = {
                "thinkingBudget": int(lvl),
                "includeThoughts": int(lvl) > 0,
            }
        elif lvl.upper() in ("NONE", "OFF", "DISABLE", "DISABLED"):
            gen_cfg["thinkingConfig"] = {"thinkingBudget": 0}
        else:
            gen_cfg["thinkingConfig"] = {
                "thinkingLevel": lvl.upper(),
                "includeThoughts": True,
            }
    if max_output_tokens is not None and int(max_output_tokens) > 0:
        gen_cfg["maxOutputTokens"] = int(max_output_tokens)
    return gen_cfg or None


def _build_payload(
    contents: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    *,
    session_id: str | None,
    include_session_id: bool,
    system_instruction: str | None,
    tool_config: dict[str, Any] | None,
    generation_config: dict[str, Any] | None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"contents": contents}
    if tools:
        payload["tools"] = tools
    if system_instruction:
        payload["systemInstruction"] = {"parts": [{"text": system_instruction}]}
    if tool_config:
        payload["toolConfig"] = tool_config
    if session_id and include_session_id:
        payload["session_id"] = session_id
    if generation_config:
        payload["generationConfig"] = generation_config
    return payload


class GeminiProvider:
    """Google Gemini Developer API (``generativelanguage.googleapis.com/v1beta``) provider."""

    def __init__(
        self,
        api_key: str,
        *,
        thinking_level: str | None = None,
        max_output_tokens: int | None = None,
        judge_provider: str = "gemini",
        timeout: float = _DEFAULT_HTTP_TIMEOUT_S,
        conn_factory: Callable[[str, float], http.client.HTTPSConnection] | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        clean_key = (api_key or "").strip()
        if not clean_key:
            raise ValueError("GeminiProvider requires a non-empty API key.")
        self._api_key = clean_key
        self._generation_config = _build_generation_config(thinking_level, max_output_tokens)
        self._judge_provider = (judge_provider or "gemini").strip().lower()
        self._pool = _KeepAliveConnectionPool(timeout=timeout, conn_factory=conn_factory)
        self._sleeper = sleeper
        self._adc_fallback: AdcTokenCache | None = None
        self._adc_init_lock = threading.Lock()

    @staticmethod
    def _model_path(model_id: str) -> str:
        clean = (model_id or "").strip()
        if "/models/" in clean:
            clean = clean.split("/models/", 1)[1]
        clean = clean.removeprefix("models/")
        quoted = urllib.parse.quote(clean, safe="-._")
        return f"/v1beta/models/{quoted}:generateContent"

    def generate(
        self,
        model_id: str,
        contents: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        session_id: str | None = None,
        system_instruction: str | None = None,
        tool_config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        del session_id  # v1beta GenerateContentRequest does not accept top-level session_id
        payload = _build_payload(
            contents,
            tools,
            session_id=None,
            include_session_id=False,
            system_instruction=system_instruction,
            tool_config=tool_config,
            generation_config=self._generation_config,
        )
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        path = self._model_path(model_id)
        headers = {"x-goog-api-key": self._api_key}

        last_err = ""
        for attempt in range(1, _MAX_GENERATE_ATTEMPTS + 1):
            try:
                status, resp_text = self._pool.post_json(_GEMINI_API_HOST, path, body, headers)
            except Exception as exc:
                last_err = str(exc)
                if attempt < _MAX_GENERATE_ATTEMPTS:
                    self._sleeper(_RETRY_BACKOFF_S * attempt)
                continue

            if status == 200:
                return json.loads(resp_text)

            last_err = f"HTTP {status}: {resp_text[:_ERR_SNIPPET_LIMIT]}"
            if status == 400 and "toolConfig" in payload:
                payload.pop("toolConfig", None)
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                continue
            if status in _RETRYABLE_STATUSES:
                if attempt < _MAX_GENERATE_ATTEMPTS:
                    self._sleeper(_RETRY_BACKOFF_S * attempt)
                continue
            raise RuntimeError(last_err)

        raise RuntimeError(f"Gemini API call failed after retries: {last_err}")

    def judge_access_token(self) -> str:
        if self._judge_provider != "vertex":
            return ""
        with self._adc_init_lock:
            if self._adc_fallback is None:
                self._adc_fallback = AdcTokenCache()
            cache = self._adc_fallback
        try:
            return cache.get_token()
        except Exception:
            return ""

    def close(self) -> None:
        self._pool.close()


class VertexProvider:
    """Google Cloud Vertex AI ``generateContent`` provider with keep-alive & ADC cache."""

    def __init__(
        self,
        project_id: str,
        location: str = "global",
        *,
        thinking_level: str | None = None,
        max_output_tokens: int | None = None,
        judge_provider: str = "vertex",
        token_cache: AdcTokenCache | None = None,
        timeout: float = _DEFAULT_HTTP_TIMEOUT_S,
        conn_factory: Callable[[str, float], http.client.HTTPSConnection] | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        clean_proj = (project_id or "").strip()
        if not clean_proj:
            raise ValueError("VertexProvider requires a non-empty GCP project_id.")
        self.project_id = clean_proj
        self.location = (location or "global").strip() or "global"
        self._host = (
            _VERTEX_GLOBAL_HOST
            if self.location == "global"
            else f"{self.location}-{_VERTEX_GLOBAL_HOST}"
        )
        self._generation_config = _build_generation_config(thinking_level, max_output_tokens)
        self._judge_provider = (judge_provider or "vertex").strip().lower()
        self._token_cache = token_cache if token_cache is not None else AdcTokenCache(sleeper=sleeper)
        self._pool = _KeepAliveConnectionPool(timeout=timeout, conn_factory=conn_factory)
        self._sleeper = sleeper
        self._supports_session_id = True

    def _model_path(self, model_id: str) -> str:
        clean = (model_id or "").strip()
        if clean.startswith("projects/"):
            return f"/v1/{clean}:generateContent"
        if clean.startswith("publishers/"):
            return (
                f"/v1/projects/{self.project_id}/locations/{self.location}"
                f"/{clean}:generateContent"
            )
        clean = clean.removeprefix("models/")
        quoted = urllib.parse.quote(clean, safe="-._")
        return (
            f"/v1/projects/{self.project_id}/locations/{self.location}"
            f"/publishers/google/models/{quoted}:generateContent"
        )

    def generate(
        self,
        model_id: str,
        contents: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        session_id: str | None = None,
        system_instruction: str | None = None,
        tool_config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload = _build_payload(
            contents,
            tools,
            session_id=session_id,
            include_session_id=self._supports_session_id,
            system_instruction=system_instruction,
            tool_config=tool_config,
            generation_config=self._generation_config,
        )
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        path = self._model_path(model_id)

        last_err = ""
        for attempt in range(1, _MAX_GENERATE_ATTEMPTS + 1):
            try:
                token = self._token_cache.get_token()
                headers = {"Authorization": f"Bearer {token}"}
                status, resp_text = self._pool.post_json(self._host, path, body, headers)
            except Exception as exc:
                last_err = str(exc)
                if attempt < _MAX_GENERATE_ATTEMPTS:
                    self._sleeper(_RETRY_BACKOFF_S * attempt)
                continue

            if status == 200:
                return json.loads(resp_text)

            last_err = f"HTTP {status}: {resp_text[:_ERR_SNIPPET_LIMIT]}"
            if status == 400 and "session_id" in resp_text and "session_id" in payload:
                self._supports_session_id = False
                payload.pop("session_id", None)
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                continue
            if status == 400 and "toolConfig" in payload:
                payload.pop("toolConfig", None)
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                continue
            if status == 401:
                self._token_cache.invalidate()
                continue
            if status in _RETRYABLE_STATUSES:
                if attempt < _MAX_GENERATE_ATTEMPTS:
                    self._sleeper(_RETRY_BACKOFF_S * attempt)
                continue
            raise RuntimeError(last_err)

        raise RuntimeError(f"Vertex AI call failed after retries: {last_err}")

    def judge_access_token(self) -> str:
        if self._judge_provider != "vertex":
            return ""
        try:
            return self._token_cache.get_token()
        except Exception:
            return ""

    def close(self) -> None:
        self._pool.close()


def create_provider(config: BenchConfig) -> ModelProvider:
    """Instantiate the configured :class:`ModelProvider` (``gemini`` or ``vertex``)."""
    provider_kind = (config.provider or "vertex").strip().lower()
    if provider_kind == "gemini":
        api_key = (
            config.api_key
            or os.environ.get("GEMINI_API_KEY", "")
            or os.environ.get("GOOGLE_API_KEY", "")
        ).strip()
        if not api_key:
            raise ValueError(
                "Gemini provider requires an API key via --api-key or GEMINI_API_KEY."
            )
        return GeminiProvider(
            api_key=api_key,
            thinking_level=config.thinking_level,
            max_output_tokens=config.max_output_tokens,
            judge_provider=config.judge_provider,
        )

    if provider_kind == "vertex":
        project_id = (
            config.project_id
            or os.environ.get("VERTEX_PROJECT_ID", "")
            or os.environ.get("GOOGLE_CLOUD_PROJECT", "")
            or os.environ.get("GCLOUD_PROJECT", "")
        ).strip()
        if not project_id:
            raise ValueError(
                "Vertex provider requires a project ID via --project or "
                "VERTEX_PROJECT_ID / GOOGLE_CLOUD_PROJECT."
            )
        location = (
            config.location or os.environ.get("VERTEX_LOCATION", "") or "global"
        ).strip()
        return VertexProvider(
            project_id=project_id,
            location=location,
            thinking_level=config.thinking_level,
            max_output_tokens=config.max_output_tokens,
            judge_provider=config.judge_provider,
        )

    raise ValueError(
        f"Unsupported provider {config.provider!r}. Expected 'gemini' or 'vertex'."
    )

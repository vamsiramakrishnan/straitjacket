"""HTTP client for a yes/no decision endpoint (the ``model`` / ``shadow``
evidence-map picker). Imported only on that path; the default ``local`` mode
never loads this module or any network module.

Request (the hosted Decisions API shape; a compatible self-hosted server can
be named with ``[picker] endpoint``)::

    POST <endpoint>        header x-goog-api-key: <value of $<api_key_env>>
    {"model": "<model>",
     "input": "An engineer ran the command `<command>` and asks: <intent>",
     "questions": {"m000": {"type": "noul", "instructions": "<question>\\n<excerpt>"}, ...}}

Response: ``{"answers": {"m000": {"noul": P(yes), ...}, ...}, "usage":
{"total_tokens": N, ...}}``. Answers are read by id, never by order.

The key is read from the environment at call time, sent only in the request
header, and never logged, stored, returned or put in an exception message.
A hard deadline bounds the whole call (a daemon thread is joined with the
deadline, so a slow server cannot hold the caller past it).
"""

from __future__ import annotations

import ipaddress
import json
import os
import threading
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from ctx.digest.picker import QUESTION, PickerError

MAX_RESPONSE_BYTES = 1 << 20


def _loopback(host: str) -> bool:
    if host in ("localhost",):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class DecisionClient:
    def __init__(self, policy: Any) -> None:
        self.endpoint = str(policy.endpoint)
        self.model = str(policy.model)
        self.key_env = str(policy.api_key_env)
        self.deadline = max(0.1, int(policy.timeout_ms) / 1000.0)

    def _request(self, body: bytes, key: str) -> dict[str, Any]:
        url = urllib.parse.urlsplit(self.endpoint)
        if url.scheme not in ("https", "http"):
            raise PickerError("endpoint scheme")
        if url.scheme == "http" and not _loopback(url.hostname or ""):
            raise PickerError("plain http allowed only to a loopback endpoint")
        handlers: list[Any] = []
        if _loopback(url.hostname or ""):
            handlers.append(urllib.request.ProxyHandler({}))
        opener = urllib.request.build_opener(*handlers)
        req = urllib.request.Request(
            self.endpoint, data=body, method="POST",
            headers={"content-type": "application/json", "x-goog-api-key": key},
        )
        try:
            with opener.open(req, timeout=self.deadline) as resp:
                raw = resp.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as e:
            raise PickerError(f"http {int(e.code)}") from None
        except urllib.error.URLError as e:
            reason = getattr(e, "reason", None)
            if isinstance(reason, TimeoutError) or "timed out" in str(reason):
                raise PickerError("timeout") from None
            raise PickerError(f"transport {type(reason).__name__}") from None
        if len(raw) > MAX_RESPONSE_BYTES:
            raise PickerError("malformed answer")
        try:
            doc = json.loads(raw.decode("utf-8"))
        except ValueError:
            raise PickerError("malformed answer") from None
        if not isinstance(doc, dict):
            raise PickerError("malformed answer")
        return doc

    def score(self, intent: str, command: str, candidates: list[str]) -> tuple[list[float], int | None]:
        key = os.environ.get(self.key_env) if self.key_env else None
        if not key:
            raise PickerError("missing key")
        ids = [f"m{i:03d}" for i in range(len(candidates))]
        body = json.dumps({
            "model": self.model,
            "input": f"An engineer ran the command `{command}` and asks: {intent}",
            "questions": {qid: {"type": "noul", "instructions": QUESTION + text}
                          for qid, text in zip(ids, candidates)},
        }).encode("utf-8")
        box: dict[str, Any] = {}

        def work() -> None:
            try:
                box["doc"] = self._request(body, key)
            except BaseException as e:  # noqa: BLE001 -- handed to the caller
                box["err"] = e

        t = threading.Thread(target=work, daemon=True)
        t.start()
        t.join(self.deadline)
        if t.is_alive():
            raise PickerError("timeout")
        if "err" in box:
            err = box["err"]
            raise err if isinstance(err, PickerError) else PickerError(f"error: {type(err).__name__}")
        doc = box.get("doc") or {}
        answers = doc.get("answers")
        if not isinstance(answers, dict):
            raise PickerError("malformed answer")
        scores: list[float] = []
        for qid in ids:
            a = answers.get(qid)
            p = a.get("noul") if isinstance(a, dict) else None
            if isinstance(p, bool) or not isinstance(p, (int, float)) or not 0.0 <= float(p) <= 1.0:
                raise PickerError("malformed answer")
            scores.append(float(p))
        usage = doc.get("usage")
        tokens = usage.get("total_tokens") if isinstance(usage, dict) else None
        return scores, tokens if isinstance(tokens, int) and not isinstance(tokens, bool) else None

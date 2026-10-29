"""Deterministic diagnostic receipts for edits owned by ``ctx``.

The host hooks can observe edits, but they cannot prove that diagnostics
reported immediately afterwards describe the bytes that were just written.
This module gives ctx-owned edit transactions a small diagnostic seam:

1. capture a document digest and optional diagnostic version before writing;
2. run dependency-free syntax checks and/or an injected LSP-like provider;
3. classify every result as ``fresh``, ``stale``, or ``unavailable``; and
4. persist an idempotent, content-addressed receipt outside the repository.

It deliberately does not perform the edit and is not wired into host hooks.
Only a caller that owns the write has enough ordering information to use this
contract honestly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
import tempfile
import tomllib
from typing import Any, Callable, Iterable, Literal

from ctx.sessiondir import session_reads_path
from ctx.textutil import EVIDENCE_LINE_CHARS


RECEIPT_SCHEMA = "ctx.post-edit-diagnostics/v1"
MAX_DOCUMENT_BYTES = 2 * 1024 * 1024
MAX_DIAGNOSTICS = 50
MAX_MESSAGE_CHARS = 500

Freshness = Literal["fresh", "stale", "unavailable"]


@dataclass(frozen=True)
class Diagnostic:
    """One provider-neutral diagnostic without a source-code excerpt."""

    message: str
    severity: str = "error"
    line: int | None = None
    column: int | None = None
    code: str | None = None


@dataclass(frozen=True)
class DiagnosticSnapshot:
    """A check provider's diagnostics and the evidence that dates them.

    ``document_digest`` should be the SHA-256 of the exact bytes inspected.
    LSP adapters that cannot provide bytes may instead report a monotonically
    advancing ``version``. A snapshot with neither is available but cannot be
    proven current, and is therefore classified as stale.
    """

    source: str
    diagnostics: tuple[Diagnostic, ...] = field(default_factory=tuple)
    document_digest: str | None = None
    version: int | str | None = None
    available: bool = True
    reason: str | None = None


@dataclass(frozen=True)
class DiagnosticBaseline:
    """Facts captured before an owned edit begins."""

    path: str
    document_digest: str | None
    diagnostic_version: int | str | None = None
    diagnostic_fingerprint: str | None = None


DiagnosticProvider = Callable[[Path, DiagnosticBaseline], DiagnosticSnapshot]


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _owned_path(workspace_root: Path, path: Path | str) -> tuple[Path, str]:
    root = Path(workspace_root).resolve()
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved = candidate.resolve(strict=False)
    try:
        relative = resolved.relative_to(root).as_posix()
    except ValueError as exc:
        raise ValueError("diagnostic target must be inside the workspace") from exc
    return resolved, relative


def _document_digest(path: Path) -> str | None:
    try:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            while chunk := handle.read(128 * 1024):
                digest.update(chunk)
        return digest.hexdigest()
    except (FileNotFoundError, IsADirectoryError):
        return None


def _diagnostic_doc(diagnostic: Diagnostic) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "message": str(diagnostic.message)[:MAX_MESSAGE_CHARS],
        "severity": str(diagnostic.severity).lower()[:24] or "error",
    }
    if diagnostic.line is not None:
        line = int(diagnostic.line)
        doc["line"] = line if line > 0 else 1
    if diagnostic.column is not None:
        column = int(diagnostic.column)
        doc["column"] = column if column > 0 else 1
    if diagnostic.code is not None:
        doc["code"] = str(diagnostic.code)[:80]
    return doc


def _diagnostic_fingerprint(diagnostics: Iterable[Diagnostic]) -> str:
    docs = [_diagnostic_doc(item) for item in diagnostics]
    return _sha256(_canonical(docs))


def capture_baseline(
    workspace_root: Path,
    path: Path | str,
    *,
    diagnostics: DiagnosticSnapshot | None = None,
) -> DiagnosticBaseline:
    """Capture the file identity and optional provider state before an edit."""

    target, relative = _owned_path(workspace_root, path)
    return DiagnosticBaseline(
        path=relative,
        document_digest=_document_digest(target),
        diagnostic_version=diagnostics.version if diagnostics else None,
        diagnostic_fingerprint=(
            _diagnostic_fingerprint(diagnostics.diagnostics)
            if diagnostics and diagnostics.available
            else None
        ),
    )


def builtin_syntax_snapshot(path: Path) -> DiagnosticSnapshot:
    """Check Python, JSON, or TOML syntax synchronously and without side effects.

    A bounded read prevents a generated file from turning a receipt into an
    expensive surprise. Unsupported and oversized files are unavailable rather
    than optimistically clean.
    """

    source_by_suffix = {
        ".py": "python-compile",
        ".json": "json-parser",
        ".toml": "toml-parser",
    }
    source = source_by_suffix.get(path.suffix.lower(), "builtin-syntax")
    if path.suffix.lower() not in source_by_suffix:
        return DiagnosticSnapshot(
            source=source, available=False, reason="unsupported_file_type"
        )
    try:
        size = path.stat().st_size
        if size > MAX_DOCUMENT_BYTES:
            return DiagnosticSnapshot(
                source=source, available=False, reason="document_too_large"
            )
        raw = path.read_bytes()
    except OSError:
        return DiagnosticSnapshot(source=source, available=False, reason="read_failed")

    digest = _sha256(raw)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        return DiagnosticSnapshot(
            source=source,
            document_digest=digest,
            diagnostics=(
                Diagnostic(
                    message="document is not valid UTF-8",
                    line=1,
                    column=exc.start + 1,
                    code="invalid-utf8",
                ),
            ),
        )

    try:
        suffix = path.suffix.lower()
        if suffix == ".py":
            # compile() checks the grammar without creating __pycache__ files.
            compile(text, path.name, "exec", dont_inherit=True)
        elif suffix == ".json":
            json.loads(text)
        else:
            tomllib.loads(text)
    except SyntaxError as exc:
        diagnostic = Diagnostic(
            message=exc.msg or "invalid syntax",
            line=exc.lineno,
            column=exc.offset,
            code="syntax-error",
        )
        return DiagnosticSnapshot(
            source=source, diagnostics=(diagnostic,), document_digest=digest
        )
    except json.JSONDecodeError as exc:
        diagnostic = Diagnostic(
            message=exc.msg,
            line=exc.lineno,
            column=exc.colno,
            code="json-syntax-error",
        )
        return DiagnosticSnapshot(
            source=source, diagnostics=(diagnostic,), document_digest=digest
        )
    except tomllib.TOMLDecodeError as exc:
        diagnostic = Diagnostic(message=str(exc), code="toml-syntax-error")
        return DiagnosticSnapshot(
            source=source, diagnostics=(diagnostic,), document_digest=digest
        )
    return DiagnosticSnapshot(source=source, document_digest=digest)


def _freshness(
    snapshot: DiagnosticSnapshot,
    *,
    current_digest: str | None,
    baseline_version: int | str | None,
) -> tuple[Freshness, str | None]:
    if not snapshot.available:
        return "unavailable", snapshot.reason or "provider_unavailable"
    if snapshot.document_digest is not None:
        if snapshot.document_digest == current_digest:
            return "fresh", None
        return "stale", "document_digest_mismatch"
    if snapshot.version is not None and baseline_version is not None:
        current_version = snapshot.version
        previous_version = baseline_version
        if (
            isinstance(current_version, int)
            and not isinstance(current_version, bool)
            and isinstance(previous_version, int)
            and not isinstance(previous_version, bool)
            and current_version > previous_version
        ):
            return "fresh", None
        if isinstance(current_version, str) and isinstance(previous_version, str):
            try:
                if int(current_version) > int(previous_version):
                    return "fresh", None
            except ValueError:
                # Opaque unequal versions do not prove ordering. Providers with
                # opaque versions must attach the inspected document digest.
                pass
    return "stale", "freshness_unproven"


def _check_doc(
    snapshot: DiagnosticSnapshot,
    *,
    current_digest: str | None,
    baseline_version: int | str | None,
) -> dict[str, Any]:
    status, reason = _freshness(
        snapshot,
        current_digest=current_digest,
        baseline_version=baseline_version,
    )
    all_diagnostics = tuple(snapshot.diagnostics)
    shown = all_diagnostics[:MAX_DIAGNOSTICS]
    doc: dict[str, Any] = {
        "source": str(snapshot.source)[:80],
        "freshness": status,
        "diagnosticCount": len(all_diagnostics),
        # Computed before bounding the rendered list: an error at position 51
        # must not turn a noisy failing check into a clean receipt.
        "hasErrors": any(
            str(item.severity).lower() == "error" for item in all_diagnostics
        ),
        "diagnosticFingerprint": _diagnostic_fingerprint(all_diagnostics),
        "diagnostics": [_diagnostic_doc(item) for item in shown],
        "omittedDiagnostics": max(0, len(all_diagnostics) - len(shown)),
    }
    if reason:
        doc["reason"] = str(reason)[:EVIDENCE_LINE_CHARS]
    if snapshot.version is not None:
        doc["version"] = snapshot.version
    return doc


def _receipt_dir(workspace_root: Path) -> Path:
    return session_reads_path(workspace_root, "post-edit-diagnostics")


def persist_receipt(workspace_root: Path, receipt: dict[str, Any]) -> Path:
    """Atomically persist a receipt under its content-derived identifier."""

    receipt_id = str(receipt.get("receiptId", ""))
    if len(receipt_id) != 64 or any(ch not in "0123456789abcdef" for ch in receipt_id):
        raise ValueError("receipt has no valid content identifier")
    target_dir = _receipt_dir(workspace_root)
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{receipt_id}.json"
    if target.exists():
        return target
    fd, temporary_name = tempfile.mkstemp(prefix=".receipt-", dir=target_dir)
    try:
        with open(fd, "wb", closefd=True) as handle:
            handle.write(_canonical(receipt) + b"\n")
        Path(temporary_name).replace(target)
    finally:
        Path(temporary_name).unlink(missing_ok=True)
    return target


def load_receipt(workspace_root: Path, receipt_id: str) -> dict[str, Any]:
    """Retrieve an addressable receipt and verify its content identity."""

    path = _receipt_dir(workspace_root) / f"{receipt_id}.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    claimed = doc.pop("receiptId", None)
    actual = _sha256(_canonical(doc))
    doc["receiptId"] = claimed
    if claimed != receipt_id or actual != receipt_id:
        raise ValueError("diagnostic receipt failed content verification")
    return doc


def verify_post_edit(
    workspace_root: Path,
    path: Path | str,
    baseline: DiagnosticBaseline,
    *,
    providers: Iterable[DiagnosticProvider] = (),
    run_builtin: bool = True,
    persist: bool = True,
    expected_digest: str | None = None,
) -> dict[str, Any]:
    """Run post-edit checks and return a deterministic, addressable receipt."""

    target, relative = _owned_path(workspace_root, path)
    if relative != baseline.path:
        raise ValueError("baseline belongs to a different diagnostic target")
    current_digest = _document_digest(target)
    snapshots: list[DiagnosticSnapshot] = []
    if run_builtin:
        snapshots.append(builtin_syntax_snapshot(target))
    for provider in providers:
        try:
            snapshot = provider(target, baseline)
            if not isinstance(snapshot, DiagnosticSnapshot):
                raise TypeError("provider returned the wrong snapshot type")
        except Exception as exc:  # Provider failure belongs in the receipt.
            snapshots.append(
                DiagnosticSnapshot(
                    source=getattr(provider, "__name__", "external-provider"),
                    available=False,
                    reason=f"provider_error:{type(exc).__name__}",
                )
            )
        else:
            snapshots.append(snapshot)

    final_digest = _document_digest(target)
    changed_during_checks = final_digest != current_digest
    unexpected_bytes = expected_digest is not None and final_digest != expected_digest
    checks = [
        _check_doc(
            snapshot,
            current_digest=current_digest,
            baseline_version=baseline.diagnostic_version,
        )
        for snapshot in snapshots
    ]
    if changed_during_checks or unexpected_bytes:
        for check in checks:
            if check["freshness"] == "fresh":
                check["freshness"] = "stale"
                check["reason"] = "edited_bytes_changed"
    fresh = [check for check in checks if check["freshness"] == "fresh"]
    if changed_during_checks or unexpected_bytes:
        outcome = "stale"
    elif fresh:
        has_errors = any(check["hasErrors"] for check in fresh)
        outcome = "issues" if has_errors else "clean"
    elif any(check["freshness"] == "stale" for check in checks):
        outcome = "stale"
    else:
        outcome = "unavailable"

    body: dict[str, Any] = {
        "schema": RECEIPT_SCHEMA,
        "path": relative,
        "baselineDocumentDigest": baseline.document_digest,
        "postEditDocumentDigest": current_digest,
        "changeObserved": baseline.document_digest != current_digest,
        "outcome": outcome,
        "checks": checks,
    }
    if baseline.diagnostic_version is not None:
        body["baselineDiagnosticVersion"] = baseline.diagnostic_version
    if baseline.diagnostic_fingerprint is not None:
        body["baselineDiagnosticFingerprint"] = baseline.diagnostic_fingerprint
    receipt_id = _sha256(_canonical(body))
    receipt = {**body, "receiptId": receipt_id}
    if persist:
        persist_receipt(workspace_root, receipt)
    return receipt

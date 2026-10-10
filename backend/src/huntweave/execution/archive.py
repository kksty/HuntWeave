"""The execution side's evidence writer, and the reasons it can refuse to write.

Raw tool output is untrusted and possibly sensitive, and the archive is the one place it becomes a
record. Three things therefore have to be decidable *before* a file is presented as evidence, and
each of them is a separate fact rather than a footnote:

* **Truncation** — an action may print more than the run is allowed to keep. What is kept is kept
  whole and *labelled*; the platform never presents a cut transcript as the complete output.
* **Redaction** — a credential the tool echoed into its own output is replaced by a placeholder and
  the redaction is recorded with the count. The secret itself is not retained: an archive that keeps
  a copy of what it redacted has not redacted anything.
* **Refusal** — output that cannot be written (no space left, an unwritable directory, a file larger
  than the configured artifact quota) is a reason to *stop the affected execution* rather than to
  continue with a gap nobody can see. Every refusal carries a `reason_code` that reaches the console
  and the call's own failure, so a missing archive is never explained away as a success (spec 0002;
  issue #19: "归档失败或存储满时阻断受影响执行，而不是继续确认").
"""

import errno
import hashlib
import re
import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID, uuid5

from huntweave.contracts.execution import ExecutionEvidence
from huntweave.execution.durable import atomic_write

__all__ = [
    "ARTIFACT_QUOTA_BYTES",
    "OUTPUT_QUOTA_BYTES",
    "ArchiveRejected",
    "Archived",
    "EvidenceArchive",
    "REDACTION_PATTERNS",
    "prepared",
]

# `PROJECT.md` section 12's conservative development defaults: one raw tool output is bounded at
# 20 MiB, and the transcript a console reads is bounded at the same figure. A deployment may
# configure both; nothing here is a promotion of those numbers to a capacity claim.
OUTPUT_QUOTA_BYTES = 20 * 1024 * 1024
ARTIFACT_QUOTA_BYTES = 20 * 1024 * 1024

# One namespace for every evidence id this side mints, so two archives never collide and a reader
# can recompute an id from the path it was archived under.
_EVIDENCE_NAMESPACE = UUID("6f9619ff-8b86-d011-b42d-00c04fc964ff")

# A conservative, deterministic set: these are shapes that must never be presented as evidence
# again, and every match is reported so a reader knows what was hidden and how much of it. The list
# deliberately covers *credentials a tool could echo* rather than target content, because the
# archive is display evidence and the platform does not get to decide that a target's business data
# is a secret.
REDACTION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "private_key_block",
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
    ),
    ("authorization_header", re.compile(r"(?i)\bauthorization:\s*(?:bearer|basic)\s+\S+")),
    ("aws_access_key_id", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("openai_style_key", re.compile(r"\bsk-[A-Za-z0-9]{20,}\b")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")),
    ("password_assignment", re.compile(r"(?i)\b(?:password|passwd|pwd)\s*[:=]\s*\S+")),
)


def _placeholder(label: str) -> str:
    return f"[redacted:{label}]"


def _keep_label(label: str, match: re.Match[str]) -> str:
    """Replace the value, keep whatever named it, so the line still reads as itself.

    `password=hunter2` becomes `password=[redacted:password_assignment]`, which is what lets a
    redacted transcript still explain what the tool was doing.
    """
    whole = match.group(0)
    for separator in (":", "="):
        head, found, _ = whole.partition(separator)
        if found:
            return f"{head}{separator} {_placeholder(label)}"
    return _placeholder(label)


def _redact(text: str) -> tuple[str, dict[str, int]]:
    """Apply every pattern once, counting what each one removed."""
    counts: dict[str, int] = {}
    for label, pattern in REDACTION_PATTERNS:
        text, found = pattern.subn(_replacer(label), text)
        if found:
            counts[label] = found
    return text, counts


def _replacer(label: str) -> Callable[[re.Match[str]], str]:
    """A replacement bound to one pattern's name, built where the name is known.

    A lambda handed to `re.subn` cannot be typed — mypy has nothing to infer its parameter from —
    and this is the seam where the pattern's label becomes part of the output. Binding it here keeps
    that step explicit instead of untyped.
    """
    return lambda match: _keep_label(label, match)


def prepared(text: str, *, max_bytes: int) -> tuple[str, bool, dict[str, int]]:
    """Redact, then cut to the quota. The order matters: cut first and a partial secret survives.

    ``max_bytes`` bounds what this run may keep of one artifact. A body that does not fit is cut on
    a UTF-8 boundary and reported as truncated, so the caller can label it instead of pretending
    the tail never existed.
    """
    text, counts = _redact(text)
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text, False, counts
    return encoded[:max_bytes].decode("utf-8", errors="ignore"), True, counts


@dataclass(frozen=True)
class Archived:
    """One archive write, with the honesty flags a reader needs beside the bytes."""

    evidence: ExecutionEvidence
    text: str
    truncated: bool = False
    redacted: bool = False
    redactions: dict[str, int] = field(default_factory=dict)


class ArchiveRejected(Exception):
    """The archive refused to write, and names why.

    Distinct from an unexpected `OSError`: this is the execution side deciding that continuing
    without an archive would present an unverifiable call as a completed one.
    """

    def __init__(self, reason_code: str):
        self.reason_code = reason_code
        super().__init__(reason_code)


class EvidenceArchive:
    """Writes one call's evidence under a root, bounded by a quota and a free-space check."""

    def __init__(self, root: Path, *, artifact_quota_bytes: int):
        self.root = root
        self.artifact_quota_bytes = artifact_quota_bytes
        root.mkdir(parents=True, exist_ok=True)

    def archive(self, relative_path: str, text: str) -> Archived:
        """Write one whole artifact.

        A single artifact over the quota is refused rather than silently cut: the record would
        otherwise claim to hold the whole thing while holding part of it, and the caller has no way
        to tell which. Truncation is for output that arrives in pieces — there the cut is this
        side's decision and is reported on the file itself.
        """
        if len(text.encode("utf-8")) > self.artifact_quota_bytes:
            raise ArchiveRejected("evidence_artifact_too_large")
        safe, truncated, redactions = prepared(text, max_bytes=self.artifact_quota_bytes)
        body = safe.encode("utf-8")
        self._require_space(len(body))
        destination = self._resolve(relative_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        self._write(destination, body)
        return Archived(
            evidence=self._evidence(relative_path, destination, truncated, bool(redactions)),
            text=safe,
            truncated=truncated,
            redacted=bool(redactions),
            redactions=redactions,
        )

    def append(self, relative_path: str, text: str, *, quota_bytes: int) -> Archived | None:
        """Append to a growing transcript, keeping at most ``quota_bytes`` of it.

        Returns ``None`` when the transcript is already full, so the caller records a truncation
        instead of writing: a tool that never stops printing cannot fill the volume.
        """
        destination = self._resolve(relative_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        previous = destination.read_bytes() if destination.exists() else b""
        if len(previous) >= quota_bytes:
            return None
        room = quota_bytes - len(previous)
        raw = text.encode("utf-8")
        cut = len(raw) > room
        body, redacted, redactions = prepared(raw[:room].decode("utf-8", "ignore"), max_bytes=room)
        encoded = body.encode("utf-8")
        self._require_space(len(encoded))
        self._write(destination, previous + encoded)
        return Archived(
            evidence=self._evidence(
                relative_path, destination, cut or redacted, bool(redactions)
            ),
            text=body,
            truncated=cut or redacted,
            redacted=bool(redactions),
            redactions=redactions,
        )

    def stored(self, relative_path: str) -> bytes:
        return self._resolve(relative_path).read_bytes()

    def stored_evidence(self, relative_path: str) -> ExecutionEvidence:
        """The evidence entry for a file already on disk, hashed from what is really there."""
        destination = self._resolve(relative_path)
        return self._evidence(relative_path, destination, False, False)

    # -- internals ----------------------------------------------------------------------------

    def _evidence(
        self, relative_path: str, destination: Path, truncated: bool, redacted: bool
    ) -> ExecutionEvidence:
        payload = destination.read_bytes()
        return ExecutionEvidence(
            id=uuid5(_EVIDENCE_NAMESPACE, relative_path),
            relative_path=relative_path,
            sha256=hashlib.sha256(payload).hexdigest(),
            size_bytes=len(payload),
            available=True,
            truncated=truncated,
            redacted=redacted,
        )

    def _write(self, destination: Path, body: bytes) -> None:
        try:
            atomic_write(destination, body)
        except OSError as error:
            if error.errno in {errno.ENOSPC, errno.EDQUOT}:
                raise ArchiveRejected("evidence_storage_full") from error
            raise ArchiveRejected("evidence_archive_unwritable") from error

    def _resolve(self, relative_path: str) -> Path:
        """Nothing a caller names may escape the archive root."""
        destination = (self.root / relative_path).resolve()
        if not destination.is_relative_to(self.root.resolve()):
            raise ArchiveRejected("evidence_path_escapes_root")
        return destination

    def _require_space(self, incoming: int) -> None:
        """Refuse before writing when the volume cannot hold this file.

        Checked up front rather than only reacted to afterwards, so "storage is full" is a decision
        this side made and can name, not an `OSError` whose meaning depends on the filesystem.
        """
        if shutil.disk_usage(self.root).free < incoming:
            raise ArchiveRejected("evidence_storage_full")

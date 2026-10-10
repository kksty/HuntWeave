"""Durable file writes shared by the execution side's ledgers and evidence archives."""

import os
import threading
from pathlib import Path


def atomic_write(path: Path, data: bytes) -> None:
    """Replace ``path`` with ``data``, leaving the previous content whole on failure.

    A reader must never see half of a new ledger or half of a new evidence file: the file is
    written to a sibling and renamed, and the directory entry is flushed so the rename survives
    a crash. Callers keep the previous content when this raises.

    The scratch name is unique per writer, so two writers that should never have overlapped still
    cannot consume each other's file; the ordering discipline itself belongs to the caller.
    """
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.pending")
    try:
        with temporary.open("wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        if os.name != "nt":
            descriptor = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    except OSError:
        temporary.unlink(missing_ok=True)
        raise

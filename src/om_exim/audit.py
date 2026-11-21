"""Append structured import events without copying entity data or credentials."""

import json
import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO
from uuid import uuid4


class AuditError(ValueError):
    pass


class ImportAudit:
    def __init__(self, stream: BinaryIO):
        self._stream = stream
        self._run_id = uuid4().hex

    def record(self, event: str, **fields: str | int) -> None:
        entry = {
            "schema_version": 1,
            "run_id": self._run_id,
            "timestamp": datetime.now(UTC).isoformat(),
            "event": event,
            **fields,
        }
        try:
            self._stream.write((json.dumps(entry, sort_keys=True) + "\n").encode())
            self._stream.flush()
        except OSError:
            raise AuditError(
                "Unable to write the import audit. Review the checkpoint before resuming."
            ) from None


@contextmanager
def import_audit(
    path: Path | None, *, package: Path, checkpoint: Path, apply: bool, policy: str
) -> Iterator[ImportAudit | None]:
    if path is None:
        yield None
        return
    try:
        resolved = path.resolve()
        package_path = package.resolve()
        checkpoint_paths = {
            checkpoint.resolve(),
            checkpoint.with_name(checkpoint.name + ".lock").resolve(),
        }
    except (OSError, RuntimeError):
        raise AuditError(
            "Unable to resolve the import audit or checkpoint path."
        ) from None
    if resolved.is_relative_to(package_path) or resolved in checkpoint_paths:
        raise AuditError(
            "The import audit must be separate from the package and checkpoint."
        )
    if path.is_symlink():
        raise AuditError("The import audit must be a regular file.")
    try:
        flags = os.O_CREAT | os.O_RDWR | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags, 0o600)
        stream = os.fdopen(fd, "a+b")
    except OSError:
        raise AuditError(
            "Unable to open import.audit_file. Check its parent directory and permissions."
        ) from None

    with stream:
        try:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise AuditError("The import audit must be a regular file.")
            stream.seek(0)
            first = stream.readline(4096)
        except OSError:
            raise AuditError("Unable to read the existing import audit.") from None
        if first:
            try:
                entry = json.loads(first)
            except ValueError:
                entry = None
            if (
                not first.endswith(b"\n")
                or not isinstance(entry, dict)
                or entry.get("event") != "import_started"
                or entry.get("schema_version") != 1
            ):
                raise AuditError(
                    "The existing audit file is not an om-exim import audit."
                )
        audit = ImportAudit(stream)
        audit.record(
            "import_started", mode="apply" if apply else "dry-run", policy=policy
        )
        try:
            yield audit
        except BaseException as error:
            try:
                audit.record("import_failed", error_type=type(error).__name__)
            except AuditError:
                pass
            raise
        else:
            audit.record("import_completed")

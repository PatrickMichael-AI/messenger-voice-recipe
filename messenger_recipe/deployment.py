"""Verified resource creation and assistant updates with caller-owned state.

Adapted from pm_vapi_deploy_assistant.py at b8671f1: exclusive durable intent
before POST, no automatic retries, explicit-ID reconciliation, independent GET
before acceptance. Raw responses remain evidence. Only assistant/output creation
and assistant update are supported; this is not general resource CRUD.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import tempfile
import uuid

from .comparison import differences
from .provider import ProviderError, resource_id


class DeploymentError(RuntimeError):
    pass


def digest(data: dict) -> str:
    return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


def write_json(path: Path, data: dict) -> None:
    """Atomic private writes; both file data and directory entries are synced."""
    path = Path(path).absolute()
    if any(parent.is_symlink() for parent in (path, *path.parents)):
        raise DeploymentError("Private artifacts must not use symlink paths.")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    text = json.dumps(data, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".pending-")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _sync_directory(path.parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _sync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class Deployment:
    """State paths are supplied by the caller; the provider knows no paths."""

    def __init__(self, provider, state: Path):
        self.provider = provider
        self.state = Path(state).absolute()

    @contextmanager
    def _lock(self):
        if any(parent.is_symlink() for parent in (self.state, *self.state.parents)):
            raise DeploymentError("Deployment state must not use symlink paths.")
        self.state.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(self.state / ".deployment.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise DeploymentError("Another deployment is in progress.") from exc
            yield
        finally:
            os.close(descriptor)

    def _paths(self, kind: str) -> tuple[Path, Path]:
        if kind not in {"assistant", "output"}:
            raise DeploymentError("Only assistant and output resources are supported.")
        return self.state / f"{kind}-pending.json", self.state / f"{kind}-accepted.json"

    def _read(self, path: Path) -> dict:
        if path.is_symlink():
            raise DeploymentError("Deployment records must not use symlink paths.")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("Expected object")
            return data
        except (OSError, ValueError) as exc:
            raise DeploymentError(f"Invalid state at {path}; reconcile before mutation.") from exc

    def _evidence(self, kind: str, operation: str, response: dict) -> None:
        write_json(self.state / "evidence" / f"{kind}-{operation}-{uuid.uuid4().hex}.json", response)

    def _verify(self, kind: str, identity: str, payload: dict) -> dict:
        response = getattr(self.provider, f"get_{kind}")(identity)
        self._evidence(kind, "get", response)
        if response.get("id") != identity:
            raise DeploymentError("Independent GET returned a different resource identity.")
        drift = differences(payload, response)
        if drift:
            raise DeploymentError("Independent GET has owned-setting drift: " + ", ".join(drift))
        return response

    def _accept(self, kind: str, response: dict, payload: dict) -> dict:
        pending, accepted = self._paths(kind)
        write_json(accepted, {"id": response["id"], "payloadSha256": digest(payload),
                              "intent": payload, "readback": response})
        if pending.exists():
            pending.unlink()
            _sync_directory(self.state)
        return response

    def create(self, kind: str, payload: dict) -> dict:
        """Create exactly once; all unresolved outcomes block further POSTs."""
        with self._lock():
            pending, accepted = self._paths(kind)
            if pending.exists():
                raise DeploymentError("Unresolved creation: reconcile by explicit resource ID; do not retry.")
            if accepted.exists():
                raise DeploymentError("Resource already has accepted identity; creation refused.")
            record = {"payloadSha256": digest(payload), "status": "submission-pending", "id": None,
                      "attemptedAt": datetime.now(timezone.utc).isoformat()}
            # Lock plus the accepted/pending checks make reservation exclusive.
            write_json(pending, record)
            try:
                response = getattr(self.provider, f"create_{kind}")(payload)
                self._evidence(kind, "post", response)
                identity = resource_id(response.get("id"))
                record.update(id=identity, status="readback-pending")
                write_json(pending, record)
                readback = self._verify(kind, identity, payload)
                return self._accept(kind, readback, payload)
            except Exception as exc:
                record["status"] = "reconciliation-required"
                write_json(pending, record)
                if isinstance(exc, ProviderError) and exc.evidence is not None:
                    self._evidence(kind, "error", exc.evidence)
                raise

    def reconcile(self, kind: str, identity: str, payload: dict) -> dict:
        """GET-only recovery: exact original intent and explicit matching ID."""
        with self._lock():
            pending, _ = self._paths(kind)
            if not pending.exists():
                raise DeploymentError("No unresolved creation to reconcile.")
            record = self._read(pending)
            if record.get("payloadSha256") != digest(payload):
                raise DeploymentError("Reconciliation requires the original creation intent.")
            identity = resource_id(identity)
            if record.get("id") is not None and record["id"] != identity:
                raise DeploymentError("Reconciliation ID conflicts with the creation response.")
            return self._accept(kind, self._verify(kind, identity, payload), payload)

    def update_assistant(self, payload: dict) -> dict:
        """Preserve accepted state on drift or uncertainty; no blind PATCH retry."""
        with self._lock():
            pending, accepted = self._paths("assistant")
            if pending.exists() or not accepted.exists():
                raise DeploymentError("Update requires accepted identity and no unresolved create.")
            previous = self._read(accepted)
            identity = resource_id(previous.get("id"))
            live = self.provider.get_assistant(identity)
            self._evidence("assistant", "before-update", live)
            if live.get("id") != identity:
                raise DeploymentError("Independent GET returned a different resource identity.")
            if not differences(payload, live):
                return self._accept("assistant", live, payload)
            if differences(previous["intent"], live):
                raise DeploymentError("Remote owned settings changed; review before update.")
            # JSON lists are replacements; None and [] are never removed.
            try:
                response = self.provider.update_assistant(identity, payload)
                self._evidence("assistant", "patch", response)
                return self._accept("assistant", self._verify("assistant", identity, payload), payload)
            except ProviderError as exc:
                if exc.evidence is not None:
                    self._evidence("assistant", "error", exc.evidence)
                raise

"""Verified resource creation and assistant updates with caller-owned state.

Adapted from pm_vapi_deploy_assistant.py at b8671f1: exclusive durable intent
before POST, no automatic retries, explicit-ID reconciliation, independent GET
before acceptance. Raw responses remain evidence. Only assistant/output creation
and updates are supported; this is not general resource CRUD.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import uuid

from .comparison import differences
from .provider import ProviderError, resource_id
from .state import InstanceState


class DeploymentError(RuntimeError):
    pass


def digest(data: dict) -> str:
    return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


def _record_names(kind: str) -> tuple[str, str]:
    if kind not in {"assistant", "output"}:
        raise DeploymentError("Only assistant and output resources are supported.")
    return f"{kind}-pending.json", f"{kind}-accepted.json"


def accepted_id(state: InstanceState, kind: str) -> str:
    """Accepted provider identity for read-only callers; needs no provider."""
    pending, accepted = _record_names(kind)
    if state.path(pending).exists():
        raise DeploymentError("Unresolved creation must be reconciled first.")
    if not state.path(accepted).exists():
        raise DeploymentError(f"No accepted {kind} identity; deploy first.")
    return resource_id(state.read_json(accepted).get("id"))


class Deployment:
    """Instance State is supplied by the caller; the provider knows no paths.

    ensure() is the only mutating entry point. It chooses create, explicit-ID
    reconciliation, update or check-only under one lock, so callers never
    branch on pending/accepted records themselves.
    """

    def __init__(self, provider, state: InstanceState):
        self.provider = provider
        self.state = state

    @contextmanager
    def _lock(self):
        self.state.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock = self.state.path(".deployment.lock")
        descriptor = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise DeploymentError("Another deployment is in progress.") from exc
            yield
        finally:
            os.close(descriptor)

    def _exists(self, name: str) -> bool:
        return self.state.path(name).exists()

    def plan(self, kind: str, *, reconcile_id: str | None = None, update: bool = False) -> str:
        """Read-only choice of create, reconcile, update or check; no provider call.

        Callers may plan every resource before the first ensure() so invalid
        recovery intent is refused before any mutation.
        """
        pending, accepted = _record_names(kind)
        if reconcile_id is not None:
            resource_id(reconcile_id)
            if not self._exists(pending):
                raise DeploymentError(f"No unresolved {kind} creation to reconcile.")
        if self._exists(pending):
            if reconcile_id is None:
                raise DeploymentError(f"Unresolved {kind} creation: rerun with --reconcile-{kind}-id "
                                      "after identifying it; do not retry.")
            if self._exists(accepted):
                raise DeploymentError(f"{kind.capitalize()} has both accepted and pending state; "
                                      "review the conflict before proceeding.")
            return "reconcile"
        if self._exists(accepted):
            return "update" if update else "check"
        return "create"

    def ensure(self, kind: str, intent: dict, *, reconcile_id: str | None = None,
               update: bool = False) -> dict:
        """Make one resource match intent and return its accepted readback.

        Without update, an accepted resource is only checked: GET evidence is
        saved, the accepted record is never rewritten and any difference stops.
        """
        with self._lock():
            action = self.plan(kind, reconcile_id=reconcile_id, update=update)
            if action == "create":
                return self._create(kind, intent)
            if action == "reconcile":
                return self._reconcile(kind, reconcile_id, intent)
            if action == "update":
                return self._update(kind, intent)
            return self._check_accepted(kind, intent)

    def check(self, intents: dict[str, dict]) -> dict:
        """GET-only drift report for accepted resources; never updates accepted state.

        Every local precondition is checked for all resources before any GET.
        """
        records = {}
        for kind in intents:
            pending, accepted = _record_names(kind)
            if self._exists(pending):
                raise DeploymentError("Unresolved creation must be reconciled before verification.")
            records[kind] = self.state.read_json(accepted)
            resource_id(records[kind].get("id"))
        for kind, intent in intents.items():
            record = records[kind]
            if record.get("payloadSha256") != digest(intent) or record.get("intent") != intent:
                raise DeploymentError(f"Accepted {kind} intent differs from current sources; review before verification.")
        report = {}
        for kind, intent in intents.items():
            identity = records[kind]["id"]
            response = getattr(self.provider, f"get_{kind}")(identity)
            evidence = self._evidence(kind, "verification", response)
            drift = differences(intent, response)
            if response.get("id") != identity:
                drift.insert(0, "id")
            report[kind] = {"id": identity, "differences": drift, "evidence": evidence}
        return report

    def _evidence(self, kind: str, operation: str, response: dict) -> Path:
        name = f"evidence/{kind}-{operation}-{uuid.uuid4().hex}.json"
        self.state.create(name, response)
        return self.state.path(name)

    @staticmethod
    def _validation_rejection(kind: str, evidence: dict) -> bool:
        """Only a captured, explicit create validation response proves rejection."""
        status = evidence.get("status")
        body = evidence.get("body")
        endpoint = "assistant" if kind == "assistant" else "structured-output"
        if (evidence.get("method") != "POST" or evidence.get("path") != endpoint
                or status not in (400, 422) or not isinstance(body, dict)
                or evidence.get("id") is not None or body.get("id") is not None
                or body.get("statusCode") != status):
            return False
        expected_error = "Bad Request" if status == 400 else "Unprocessable Entity"
        messages = body.get("message")
        return (body.get("error") == expected_error and isinstance(messages, list)
                and bool(messages) and all(isinstance(item, str) and item.strip() for item in messages))

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
        pending, accepted = _record_names(kind)
        self.state.replace(accepted, {"id": response["id"], "payloadSha256": digest(payload),
                                      "intent": payload, "readback": response})
        if self._exists(pending):
            self.state.remove(pending)
        return response

    def _create(self, kind: str, payload: dict) -> dict:
        """Never retry automatically; archive definite rejection, block uncertainty."""
        pending, accepted = _record_names(kind)
        if self._exists(pending):
            raise DeploymentError("Unresolved creation: reconcile by explicit resource ID; do not retry.")
        if self._exists(accepted):
            raise DeploymentError("Resource already has accepted identity; creation refused.")
        record = {"payloadSha256": digest(payload), "status": "submission-pending", "id": None,
                  "attemptedAt": datetime.now(timezone.utc).isoformat()}
        # Lock plus the accepted/pending checks make reservation exclusive.
        self.state.replace(pending, record)
        create_returned = False
        try:
            response = getattr(self.provider, f"create_{kind}")(payload)
            create_returned = True
            self._evidence(kind, "post", response)
            identity = resource_id(response.get("id"))
            record.update(id=identity, status="readback-pending")
            self.state.replace(pending, record)
            readback = self._verify(kind, identity, payload)
            return self._accept(kind, readback, payload)
        except Exception as exc:
            record["status"] = "reconciliation-required"
            self.state.replace(pending, record)
            if isinstance(exc, ProviderError) and exc.evidence is not None:
                evidence_path = self._evidence(kind, "error", exc.evidence)
                if (not create_returned and record["id"] is None
                        and self._validation_rejection(kind, exc.evidence)):
                    rejected = dict(record, status="validation-rejected", intent=payload,
                                    errorEvidencePath=str(evidence_path.relative_to(self.state.directory)))
                    # Enrich the durable record before atomically moving it;
                    # a failed write or rename leaves the pending blocker.
                    self.state.replace(pending, rejected)
                    self.state.move(pending, f"{kind}-rejected-{uuid.uuid4().hex}.json")
                raise ProviderError(f"{exc} Private evidence: {evidence_path}.",
                                    evidence=exc.evidence) from exc
            raise

    def _reconcile(self, kind: str, identity: str, payload: dict) -> dict:
        """GET-only recovery: exact original intent and explicit matching ID."""
        pending, _ = _record_names(kind)
        if not self._exists(pending):
            raise DeploymentError("No unresolved creation to reconcile.")
        record = self.state.read_json(pending)
        if record.get("payloadSha256") != digest(payload):
            raise DeploymentError("Reconciliation requires the original creation intent.")
        identity = resource_id(identity)
        if record.get("id") is not None and record["id"] != identity:
            raise DeploymentError("Reconciliation ID conflicts with the creation response.")
        return self._accept(kind, self._verify(kind, identity, payload), payload)

    def _check_accepted(self, kind: str, payload: dict) -> dict:
        """Check an accepted binding against unchanged intent; never re-accept."""
        _, accepted = _record_names(kind)
        record = self.state.read_json(accepted)
        identity = resource_id(record.get("id"))
        if record.get("payloadSha256") != digest(payload) or record.get("intent") != payload:
            raise DeploymentError(f"Accepted {kind} binding conflicts with the reviewed definition; "
                                  "do not create a duplicate.")
        response = getattr(self.provider, f"get_{kind}")(identity)
        self._evidence(kind, "get", response)
        if response.get("id") != identity or differences(payload, response):
            raise DeploymentError(f"Accepted {kind} GET differs from the reviewed definition; binding refused.")
        return response

    def _update(self, kind: str, payload: dict) -> dict:
        """Preserve accepted state on drift or uncertainty; no blind PATCH retry."""
        pending, accepted = _record_names(kind)
        if self._exists(pending) or not self._exists(accepted):
            raise DeploymentError("Update requires accepted identity and no unresolved create.")
        previous = self.state.read_json(accepted)
        identity = resource_id(previous.get("id"))
        live = getattr(self.provider, f"get_{kind}")(identity)
        self._evidence(kind, "before-update", live)
        if live.get("id") != identity:
            raise DeploymentError("Independent GET returned a different resource identity.")
        if not differences(payload, live):
            return self._accept(kind, live, payload)
        if differences(previous["intent"], live):
            raise DeploymentError("Remote owned settings changed; review before update.")
        if kind == "output":
            self.state.preserve(accepted)
        # JSON lists are replacements; None and [] are never removed.
        try:
            response = getattr(self.provider, f"update_{kind}")(identity, payload)
            self._evidence(kind, "patch", response)
            return self._accept(kind, self._verify(kind, identity, payload), payload)
        except ProviderError as exc:
            if exc.evidence is not None:
                self._evidence(kind, "error", exc.evidence)
            raise

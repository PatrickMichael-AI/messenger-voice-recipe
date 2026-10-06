"""Reviewed local promotion and the single assistant/output deployment sequence.

All paths and decisions belong to the standalone package. Offline preparation
never invents provider IDs; live execution accepts only independently verified
resources and preserves uncertain creations for explicit-ID reconciliation.
"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import uuid

from .comparison import differences
from .configuration import render_assistant
from .deployment import Deployment, DeploymentError, digest, write_json
from .output import render_output
from .provider import resource_id
from .recipe import (
    RecipeError, _hash, _json, _private_write, _scalar, _text,
    instance_state, load_instance, package_path, render_candidate,
)


def _prepared(root: Path, slug: str) -> dict:
    candidate = render_candidate(root, slug)
    instance = load_instance(root, slug)
    definition = render_output(root, slug)
    reviewed = {
        "sourceHashes": {**candidate["sourceHashes"],
                         "outputConfiguration": _hash(package_path(root, "config/structured-output.json")),
                         "outputSchema": _hash(package_path(root, instance["result"]["schemaRef"]))},
        "assistantPayload": candidate["assistantPayload"],
        "outputDefinition": definition,
    }
    return {"candidate": candidate, "outputDefinition": definition,
            "reviewSha256": digest(reviewed),
            "promotionSha256": digest({"sourceHashes": candidate["sourceHashes"],
                                       "assistantPayload": candidate["assistantPayload"]})}


def _prompt_paths(slug: str) -> dict:
    prefix = f".local/{slug}/prompts"
    return {
        "draft": f"{prefix}/backups/{slug}_vapi_system_prompt_refactor.txt",
        "openingDraft": f"{prefix}/backups/{slug}_first_message_candidate.txt",
        "prompt": f"{prefix}/{slug}_vapi_system_prompt.txt",
        "opening": f"{prefix}/{slug}_first_message.txt",
    }


def _checked_drafts(root: Path, slug: str, prepared: dict) -> dict:
    paths = _prompt_paths(slug)
    candidate = prepared["candidate"]
    # render_candidate already enforces format, all twenty template sections,
    # unresolved-placeholder absence and the punctuated phrase-close contract.
    if _text(package_path(root, paths["draft"])) != candidate["systemPrompt"]:
        raise RecipeError("Staged system draft differs from current reviewed sources; stage and review again.")
    if _text(package_path(root, paths["openingDraft"])) != candidate["firstMessage"] + "\n":
        raise RecipeError("Staged opening differs from current reviewed sources; stage and review again.")
    staged = _json(package_path(root, f".local/{slug}/candidate.json"))
    # Review-status labels are private history, not inputs to a new clone.
    for key in ("systemPrompt", "firstMessage", "assistantPayload", "sourceHashes"):
        if staged.get(key) != candidate[key]:
            raise RecipeError("Staged candidate differs from current sources; stage and review again.")
    return paths


def promote(root: Path, slug: str, approval_source: str) -> dict:
    """Operator-confirmed promotion binds exact prompt, opening and runtime sources.

    The command's confirmation is its own explicit local review decision. It
    does not borrow historical Phase 3 approval or authorize a live request.
    """
    _scalar(approval_source, "approval_source")
    prepared = _prepared(root, slug)
    paths = _checked_drafts(root, slug, prepared)
    contents = {"prompt": prepared["candidate"]["systemPrompt"],
                "opening": prepared["candidate"]["firstMessage"] + "\n"}
    # Validate every destination first, then preserve superseded promoted files.
    for key in contents:
        target = package_path(root, paths[key])
        if target.exists():
            _text(target)
    for key, content in contents.items():
        target = package_path(root, paths[key])
        if target.exists() and _text(target) != content:
            previous = _text(target)
            _private_write(root, f".local/{slug}/prompts/backups/{target.name}.{uuid.uuid4().hex}.txt", previous)
            # Approval is written last. An interrupted replacement cannot grant
            # approval for the new sources or a mixed pair of prompt files.
            target.unlink()
        _private_write(root, paths[key], content)
    approval = {"scope": "local-promotion", "slug": slug,
                "assistantName": prepared["candidate"]["assistantPayload"]["name"],
                "promotionSha256": prepared["promotionSha256"], "approvalSource": approval_source}
    approval_path = package_path(root, f".local/{slug}/promotion-review.json")
    if approval_path.exists():
        previous = _json(approval_path)
        if previous != approval:
            write_json(package_path(root, f".local/{slug}/promotion-review.{uuid.uuid4().hex}.json"), previous)
    write_json(approval_path, approval)
    return {"promotionSha256": prepared["promotionSha256"], "artifactPaths": paths,
            "status": "promoted_locally", "liveAuthorized": False}


def require_promotion(root: Path, slug: str) -> dict:
    prepared = _prepared(root, slug)
    paths = _checked_drafts(root, slug, prepared)
    approval = _json(package_path(root, f".local/{slug}/promotion-review.json"))
    if (approval.get("scope") != "local-promotion" or approval.get("slug") != slug
            or approval.get("assistantName") != prepared["candidate"]["assistantPayload"]["name"]
            or approval.get("promotionSha256") != prepared["promotionSha256"]
            or not isinstance(approval.get("approvalSource"), str)
            or not approval["approvalSource"].strip()):
        raise RecipeError("Local promotion approval is absent or stale; review and promote current sources.")
    for key, content in (("prompt", prepared["candidate"]["systemPrompt"]),
                         ("opening", prepared["candidate"]["firstMessage"] + "\n")):
        if _text(package_path(root, paths[key])) != content:
            raise RecipeError("Promoted artifact differs from current approved sources; review and promote again.")
    return prepared


def deployment_preview(root: Path, slug: str) -> dict:
    """Complete offline definition and provisional assistant, with no fabricated binding."""
    prepared = _prepared(root, slug)
    try:
        require_promotion(root, slug)
    except (RecipeError, OSError):
        status = "pending_or_stale"
    else:
        status = "approved_current_sources"
    return {"mode": "offline", "promotionStatus": status,
            "reviewSha256": prepared["reviewSha256"],
            "outputReviewStatus": "operator_live_review_required",
            "outputDefinition": prepared["outputDefinition"],
            "assistantCandidate": prepared["candidate"]["assistantPayload"],
            "bindingStatus": "provisional_unbound"}


def _existing_output(root: Path, slug: str, definition: dict, provider) -> dict:
    accepted = _json(package_path(root, f".local/{slug}/output-accepted.json"))
    identity = resource_id(accepted.get("id"))
    if accepted.get("payloadSha256") != digest(definition) or accepted.get("intent") != definition:
        raise DeploymentError("Accepted output binding conflicts with the reviewed definition; do not create a duplicate.")
    response = provider.get_output(identity)
    write_json(package_path(root, f".local/{slug}/evidence/output-get-{uuid.uuid4().hex}.json"), response)
    if response.get("id") != identity or differences(definition, response):
        raise DeploymentError("Accepted output GET differs from the reviewed definition; binding refused.")
    return response


def deploy(root: Path, slug: str, provider, *, reconcile_output_id: str | None = None,
           reconcile_assistant_id: str | None = None) -> dict:
    """Run the explicit live command against one package-owned output and assistant."""
    prepared = require_promotion(root, slug)
    state = instance_state(root, slug)
    # Reject invalid recovery intent before making any mutation or provider call.
    for kind, supplied in (("output", reconcile_output_id), ("assistant", reconcile_assistant_id)):
        if supplied is not None:
            resource_id(supplied)
            if not package_path(root, f".local/{slug}/{kind}-pending.json").exists():
                raise DeploymentError(f"No unresolved {kind} creation to reconcile.")
    deployment = Deployment(provider, state)
    definition = prepared["outputDefinition"]
    output_pending = package_path(root, f".local/{slug}/output-pending.json")
    output_accepted = package_path(root, f".local/{slug}/output-accepted.json")
    if output_pending.exists():
        if reconcile_output_id is None:
            raise DeploymentError("Unresolved output creation: rerun with --reconcile-output-id after identifying it; do not retry.")
        if output_accepted.exists():
            raise DeploymentError("Output has both accepted and pending state; review the conflict before proceeding.")
        output = deployment.reconcile("output", reconcile_output_id, definition)
    elif output_accepted.exists():
        output = _existing_output(root, slug, definition, provider)
    else:
        output = deployment.create("output", definition)
    identity = resource_id(output.get("id"))
    candidate = prepared["candidate"]["assistantPayload"]
    voice = {key: candidate["voice"][key] for key in ("provider", "voiceId", "model")}
    payload = render_assistant(deepcopy(candidate), name=candidate["name"],
                               prompt=prepared["candidate"]["systemPrompt"],
                               opening=candidate["firstMessage"], voice=voice,
                               output_ids=[identity])
    # render_assistant rejects provider-owned identity, but accepts our derived
    # model messages; both source and bound candidate retain all other settings.
    assistant_pending = package_path(root, f".local/{slug}/assistant-pending.json")
    assistant_accepted = package_path(root, f".local/{slug}/assistant-accepted.json")
    if assistant_pending.exists():
        if reconcile_assistant_id is None:
            raise DeploymentError("Unresolved assistant creation: rerun with --reconcile-assistant-id after identifying it; do not retry.")
        if assistant_accepted.exists():
            raise DeploymentError("Assistant has both accepted and pending state; review the conflict before proceeding.")
        assistant = deployment.reconcile("assistant", reconcile_assistant_id, payload)
    elif assistant_accepted.exists():
        assistant = deployment.update_assistant(payload)
    else:
        assistant = deployment.create("assistant", payload)
    return {"mode": "live", "status": "accepted_independent_readback",
            "assistantId": assistant["id"], "outputId": identity,
            "reviewSha256": prepared["reviewSha256"],
            "publicationStatus": "operator_dashboard_review_required",
            "callBehavior": "not_yet_proved"}

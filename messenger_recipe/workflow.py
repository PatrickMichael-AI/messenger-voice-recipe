"""Reviewed local promotion and the single assistant/output deployment sequence.

All paths and decisions belong to the standalone package. Offline preparation
never invents provider IDs; live execution accepts only independently verified
resources and preserves uncertain creations for explicit-ID reconciliation.
"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path

from .configuration import render_assistant
from .deployment import Deployment, DeploymentError, accepted_id, digest
from .output import render_output
from .provider import resource_id
from .recipe import _hash, _scalar, load_instance, render_candidate
from .state import InstanceState, RecipeError, package_path


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


def _checked_drafts(state: InstanceState, prepared: dict) -> dict:
    names = state.prompt_paths()
    candidate = prepared["candidate"]
    # render_candidate already enforces format, all twenty-one template sections,
    # unresolved-placeholder absence and the punctuated phrase-close contract.
    if state.read_text(names["draft"]) != candidate["systemPrompt"]:
        raise RecipeError("Staged system draft differs from current reviewed sources; stage and review again.")
    if state.read_text(names["openingDraft"]) != candidate["firstMessage"] + "\n":
        raise RecipeError("Staged opening differs from current reviewed sources; stage and review again.")
    staged = state.read_json(state.CANDIDATE)
    # Review-status labels are private history, not inputs to a new clone.
    for key in ("systemPrompt", "firstMessage", "assistantPayload", "sourceHashes"):
        if staged.get(key) != candidate[key]:
            raise RecipeError("Staged candidate differs from current sources; stage and review again.")
    return names


def promote(root: Path, slug: str, approval_source: str) -> dict:
    """Operator-confirmed promotion binds exact prompt, opening and runtime sources.

    The command's confirmation is its own explicit local review decision. It
    does not authorize a live request.
    """
    _scalar(approval_source, "approval_source")
    prepared = _prepared(root, slug)
    state = InstanceState(root, slug)
    names = _checked_drafts(state, prepared)
    contents = {"prompt": prepared["candidate"]["systemPrompt"],
                "opening": prepared["candidate"]["firstMessage"] + "\n"}
    # Validate every destination first, then preserve superseded promoted files.
    for key in contents:
        if state.path(names[key]).exists():
            state.read_text(names[key])
    # Approval is written last. An interrupted replacement cannot grant
    # approval for the new sources or a mixed pair of prompt files.
    for key, content in contents.items():
        state.supersede(names[key], content, backup_dir="prompts/backups")
    approval = {"scope": "local-promotion", "slug": slug,
                "assistantName": prepared["candidate"]["assistantPayload"]["name"],
                "promotionSha256": prepared["promotionSha256"], "approvalSource": approval_source}
    state.supersede("promotion-review.json", approval)
    return {"promotionSha256": prepared["promotionSha256"],
            "artifactPaths": {key: state.relative(name) for key, name in names.items()},
            "status": "promoted_locally", "liveAuthorized": False}


def require_promotion(root: Path, slug: str) -> dict:
    prepared = _prepared(root, slug)
    state = InstanceState(root, slug)
    names = _checked_drafts(state, prepared)
    approval = state.read_json("promotion-review.json")
    if (approval.get("scope") != "local-promotion" or approval.get("slug") != slug
            or approval.get("assistantName") != prepared["candidate"]["assistantPayload"]["name"]
            or approval.get("promotionSha256") != prepared["promotionSha256"]
            or not isinstance(approval.get("approvalSource"), str)
            or not approval["approvalSource"].strip()):
        raise RecipeError("Local promotion approval is absent or stale; review and promote current sources.")
    for key, content in (("prompt", prepared["candidate"]["systemPrompt"]),
                         ("opening", prepared["candidate"]["firstMessage"] + "\n")):
        if state.read_text(names[key]) != content:
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


def _bound_assistant(prepared: dict, output_id: str) -> dict:
    candidate = prepared["candidate"]["assistantPayload"]
    voice = {key: candidate["voice"][key] for key in ("provider", "voiceId", "model")}
    return render_assistant(deepcopy(candidate), name=candidate["name"],
                            prompt=prepared["candidate"]["systemPrompt"],
                            opening=candidate["firstMessage"], voice=voice,
                            output_ids=[output_id])


def verify_deployment(root: Path, slug: str, provider) -> dict:
    """GET-only verification; evidence writes never update accepted state.

    The fetched API body alone cannot prove dashboard publication or call behavior.
    """
    prepared = require_promotion(root, slug)
    state = InstanceState(root, slug)
    intents = {"output": prepared["outputDefinition"],
               "assistant": _bound_assistant(prepared, accepted_id(state, "output"))}
    report = Deployment(provider, state).check(intents)
    drift = {kind: result["differences"] for kind, result in report.items()}
    evidence = {kind: str(result["evidence"].relative_to(Path(root).absolute()))
                for kind, result in report.items()}
    return {"mode": "read_only", "status": "owned_setting_drift" if any(drift.values()) else "owned_settings_match",
            "assistantId": report["assistant"]["id"], "outputId": report["output"]["id"],
            "reviewSha256": prepared["reviewSha256"], "differences": drift, "evidencePaths": evidence,
            "publicationStatus": "not_proved_by_api_get", "callBehavior": "not_yet_proved"}


def deploy(root: Path, slug: str, provider, *, reconcile_output_id: str | None = None,
           reconcile_assistant_id: str | None = None, update_output: bool = False) -> dict:
    """Run the explicit live command against one package-owned output and assistant."""
    prepared = require_promotion(root, slug)
    deployment = Deployment(provider, InstanceState(root, slug))
    # An extraction change needs explicit --update-output; assistant changes
    # follow the promoted sources.
    requests = {"output": {"reconcile_id": reconcile_output_id, "update": update_output},
                "assistant": {"reconcile_id": reconcile_assistant_id, "update": True}}
    # Reject invalid recovery intent before making any mutation or provider call.
    actions = {kind: deployment.plan(kind, **request) for kind, request in requests.items()}
    if update_output and actions["output"] != "update":
        raise DeploymentError("Output update requires an accepted existing binding and no unresolved creation.")
    output = deployment.ensure("output", prepared["outputDefinition"], **requests["output"])
    identity = resource_id(output.get("id"))
    # render_assistant rejects provider-owned identity, but accepts our derived
    # model messages; both source and bound candidate retain all other settings.
    assistant = deployment.ensure("assistant", _bound_assistant(prepared, identity), **requests["assistant"])
    return {"mode": "live", "status": "accepted_independent_readback",
            "assistantId": assistant["id"], "outputId": identity,
            "reviewSha256": prepared["reviewSha256"],
            "publicationStatus": "operator_dashboard_review_required",
            "callBehavior": "not_yet_proved"}

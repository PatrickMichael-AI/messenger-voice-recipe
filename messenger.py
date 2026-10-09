#!/usr/bin/env python3
"""Package-owned Messenger preparation and explicit assistant/output deployment."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

from messenger_recipe.deployment import DeploymentError, accepted_id
from messenger_recipe.provider import ProviderError, VapiProvider, resource_id
from messenger_recipe.output import read_enquiry_result
from messenger_recipe.recipe import record_voice_selection, render_candidate, stage_candidate
from messenger_recipe.state import InstanceState
from messenger_recipe.workflow import deploy, deployment_preview, promote, require_promotion, verify_deployment


PACKAGE_ROOT = Path(__file__).resolve().parent


def _call_name(call_id: str, attempt: int) -> str:
    """Three explicit immutable snapshots; no polling or automatic retries."""
    if type(attempt) is not int or not 1 <= attempt <= 3:
        raise ValueError("Call evidence attempt must be 1, 2 or 3.")
    suffix = "" if attempt == 1 else f".attempt-{attempt}"
    return f"calls/{resource_id(call_id)}{suffix}.json"


def fetch_call(root: Path, slug: str, call_id: str, provider, *, attempt: int = 1) -> Path:
    """Fetch one exact call for the accepted Recipe-owned assistant only."""
    state = InstanceState(root, slug)
    name = _call_name(call_id, attempt)
    if state.path(name).exists():
        raise ValueError("Call evidence already exists; preserve it instead of replacing it.")
    identity = accepted_id(state, "assistant")
    call_id = resource_id(call_id)
    response = provider.get_call(call_id)
    if response.get("id") != call_id or response.get("assistantId") != identity:
        raise ValueError("Call identity does not match the accepted Recipe assistant.")
    state.create(name, response)
    return state.path(name)


def inspect_call(root: Path, slug: str, call_id: str, *, attempt: int = 1) -> dict:
    """Inspect saved native result shape offline; transcript semantics need review."""
    state = InstanceState(root, slug)
    name = _call_name(call_id, attempt)
    path = state.path(name)
    assistant_id = accepted_id(state, "assistant")
    output_id = accepted_id(state, "output")
    response = state.read_json(name)
    if response.get("id") != call_id or response.get("assistantId") != assistant_id:
        raise ValueError("Call identity does not match the accepted Recipe assistant.")
    report = {"mode": "offline", "callId": call_id, "assistantId": assistant_id, "outputId": output_id,
              "attempt": attempt, "evidencePath": str(path.relative_to(root)),
              "semanticValidity": "operator_transcript_review_required"}
    # The canonical result is caller content; the offline report never prints it.
    reading = read_enquiry_result(response, output_id)
    reading.pop("result", None)
    return {**report, **reading}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Standalone Messenger Recipe preparation and deployment")
    parser.add_argument("--instance", default="messenger_recipe_demo")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("check", help="Validate sources, voice selection and candidate offline")
    subparsers.add_parser("preview", help="Print the provisional, unbound candidate; no live request")
    stage = subparsers.add_parser("stage", help="Write local review artifacts; refuse divergent existing files")
    stage.add_argument("--refresh", action="store_true", help="Preserve old review files before explicitly restaging changed sources")
    promotion = subparsers.add_parser("promote", help="Approve and locally promote the reviewed prompt/opening/runtime")
    promotion.add_argument("--confirm", action="store_true", required=True,
                           help="Operator explicitly approves the staged prompt, opening and runtime; no live request")
    deployment = subparsers.add_parser("deploy", help="Preview offline; --live explicitly creates or verifies the output and single assistant")
    deployment.add_argument("--live", action="store_true", help="Operator explicitly authorizes the concrete output and assistant API requests")
    deployment.add_argument("--update-output", action="store_true", help="Explicitly authorize updating the existing output definition after review; retain its ID")
    deployment.add_argument("--reconcile-output-id", help="Recover unresolved output creation by exact provider ID, using GET only")
    deployment.add_argument("--reconcile-assistant-id", help="Recover unresolved assistant creation by exact provider ID, using GET only")
    subparsers.add_parser("verify-deployment", help="GET and compare accepted output/assistant; does not prove publication or calls")
    voice = subparsers.add_parser("select-voice", help="Record approval of this instance's exact voice tuple")
    voice.add_argument("--confirm", action="store_true", required=True,
                       help="Operator explicitly approves the configured provider/ID/model")
    call = subparsers.add_parser("fetch-call", help="Read one exact call belonging to the accepted assistant")
    call.add_argument("call_id")
    call.add_argument("--attempt", type=int, choices=(1, 2, 3), default=1, help="Explicit immutable evidence snapshot; no automatic refetch")
    inspection = subparsers.add_parser("inspect-call", help="Check saved exact-call native result shape offline; semantics require transcript review")
    inspection.add_argument("call_id")
    inspection.add_argument("--attempt", type=int, choices=(1, 2, 3), default=1)
    args = parser.parse_args(argv)
    try:
        if args.command == "select-voice":
            record_voice_selection(PACKAGE_ROOT, args.instance,
                                   approval_source="Operator action: select-voice --confirm")
            print("Exact configured voice selection recorded locally; no live action authorized.")
        elif args.command == "stage":
            result = stage_candidate(PACKAGE_ROOT, args.instance, refresh=args.refresh)
            print(json.dumps({"candidateStatus": result["candidateStatus"],
                              "artifactPaths": result["artifactPaths"]}, indent=2))
        elif args.command == "promote":
            result = promote(PACKAGE_ROOT, args.instance,
                             approval_source="Operator action: promote --confirm after local review")
            print(json.dumps(result, indent=2))
        elif args.command == "deploy":
            if not args.live:
                if args.reconcile_output_id or args.reconcile_assistant_id or args.update_output:
                    raise ValueError("Recovery and output updates require --live; offline preview makes no provider request.")
                print(json.dumps(deployment_preview(PACKAGE_ROOT, args.instance), indent=2, ensure_ascii=False))
            else:
                require_promotion(PACKAGE_ROOT, args.instance)
                # Public clone environment is explicit; no automatic .env reading.
                with VapiProvider(os.environ.get("VAPI_API_KEY", "")) as provider:
                    result = deploy(PACKAGE_ROOT, args.instance, provider,
                                    reconcile_output_id=args.reconcile_output_id,
                                    reconcile_assistant_id=args.reconcile_assistant_id,
                                    update_output=args.update_output)
                print(json.dumps(result, indent=2))
        elif args.command in {"check", "preview"}:
            candidate = render_candidate(PACKAGE_ROOT, args.instance)
            if args.command == "preview":
                print(json.dumps(candidate, indent=2, ensure_ascii=False))
            else:
                complete = deployment_preview(PACKAGE_ROOT, args.instance)
                if complete["promotionStatus"] == "approved_current_sources":
                    review = "current prompt/opening/runtime promoted locally"
                else:
                    review = "prompt/opening/runtime review pending"
                print(f"Offline checks passed; {review}; candidate is unbound. Use deploy for the complete offline preview.")
        elif args.command == "verify-deployment":
            with VapiProvider(os.environ.get("VAPI_API_KEY", "")) as provider:
                result = verify_deployment(PACKAGE_ROOT, args.instance, provider)
            print(json.dumps(result, indent=2))
            return 0 if result["status"] == "owned_settings_match" else 1
        elif args.command == "inspect-call":
            result = inspect_call(PACKAGE_ROOT, args.instance, args.call_id, attempt=args.attempt)
            print(json.dumps(result, indent=2))
            return 0 if result["status"] == "valid_shape_semantics_unproved" else 1
        else:
            # Public clone environment is explicit; no automatic .env reading.
            token = os.environ.get("VAPI_API_KEY", "")
            with VapiProvider(token) as provider:
                path = fetch_call(PACKAGE_ROOT, args.instance, args.call_id, provider, attempt=args.attempt)
            print(f"Exact-call provider evidence saved to {path}")
    except (ValueError, OSError, DeploymentError, ProviderError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

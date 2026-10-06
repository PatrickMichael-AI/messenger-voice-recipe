#!/usr/bin/env python3
"""Package-owned Messenger preparation and explicit assistant/output deployment."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

from messenger_recipe.deployment import DeploymentError, write_json
from messenger_recipe.provider import ProviderError, VapiProvider, resource_id
from messenger_recipe.recipe import (
    instance_state,
    package_path,
    record_voice_selection,
    render_candidate,
    stage_candidate,
)
from messenger_recipe.workflow import deploy, deployment_preview, promote, require_promotion


PACKAGE_ROOT = Path(__file__).resolve().parent


def fetch_call(root: Path, slug: str, call_id: str, provider) -> Path:
    """Fetch one exact call for the accepted Recipe-owned assistant only."""
    state = instance_state(root, slug)
    accepted_path = package_path(root, f".local/{slug}/assistant-accepted.json")
    accepted = json.loads(accepted_path.read_text(encoding="utf-8"))
    identity = resource_id(accepted.get("id"))
    call_id = resource_id(call_id)
    response = provider.get_call(call_id)
    if response.get("id") != call_id or response.get("assistantId") != identity:
        raise ValueError("Call identity does not match the accepted Recipe assistant.")
    path = package_path(root, f".local/{slug}/calls/{call_id}.json")
    if path.exists():
        raise ValueError("Call evidence already exists; preserve it instead of replacing it.")
    write_json(path, response)
    return path


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
    deployment.add_argument("--reconcile-output-id", help="Recover unresolved output creation by exact provider ID, using GET only")
    deployment.add_argument("--reconcile-assistant-id", help="Recover unresolved assistant creation by exact provider ID, using GET only")
    voice = subparsers.add_parser("select-voice", help="Record approval of this instance's exact voice tuple")
    voice.add_argument("--confirm", action="store_true", required=True,
                       help="Operator explicitly approves the configured provider/ID/model")
    call = subparsers.add_parser("fetch-call", help="Read one exact call belonging to the accepted assistant")
    call.add_argument("call_id")
    args = parser.parse_args(argv)
    try:
        if args.command == "select-voice":
            record_voice_selection(PACKAGE_ROOT, args.instance,
                                   approval_source="Operator action: select-voice --confirm")
            print("Exact configured voice selection recorded locally; no live action authorized.")
        elif args.command == "stage":
            result = stage_candidate(PACKAGE_ROOT, args.instance, refresh=args.refresh)
            print(json.dumps({"candidateStatus": result["candidateStatus"],
                              "reviewStatus": result["reviewStatus"],
                              "artifactPaths": result["artifactPaths"]}, indent=2))
        elif args.command == "promote":
            result = promote(PACKAGE_ROOT, args.instance,
                             approval_source="Operator action: promote --confirm after local review")
            print(json.dumps(result, indent=2))
        elif args.command == "deploy":
            if not args.live:
                if args.reconcile_output_id or args.reconcile_assistant_id:
                    raise ValueError("Reconciliation needs --live; offline preview makes no provider request.")
                print(json.dumps(deployment_preview(PACKAGE_ROOT, args.instance), indent=2, ensure_ascii=False))
            else:
                require_promotion(PACKAGE_ROOT, args.instance)
                # Public clone environment is explicit; no automatic .env reading.
                with VapiProvider(os.environ.get("VAPI_API_KEY", "")) as provider:
                    result = deploy(PACKAGE_ROOT, args.instance, provider,
                                    reconcile_output_id=args.reconcile_output_id,
                                    reconcile_assistant_id=args.reconcile_assistant_id)
                print(json.dumps(result, indent=2))
        elif args.command in {"check", "preview"}:
            candidate = render_candidate(PACKAGE_ROOT, args.instance)
            if args.command == "preview":
                print(json.dumps(candidate, indent=2, ensure_ascii=False))
            else:
                complete = deployment_preview(PACKAGE_ROOT, args.instance)
                if complete["promotionStatus"] == "approved_current_sources":
                    review = "current prompt/opening/runtime promoted locally"
                elif candidate["reviewStatus"] == "approved_phase_3":
                    review = "Phase 3 baseline approved locally; promotion pending"
                else:
                    review = "prompt/opening/runtime review pending"
                print(f"Offline checks passed; {review}; candidate is unbound. Use deploy for the complete offline preview.")
        else:
            # Public clone environment is explicit; no automatic .env reading.
            token = os.environ.get("VAPI_API_KEY", "")
            with VapiProvider(token) as provider:
                path = fetch_call(PACKAGE_ROOT, args.instance, args.call_id, provider)
            print(f"Exact-call provider evidence saved to {path}")
    except (ValueError, OSError, DeploymentError, ProviderError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

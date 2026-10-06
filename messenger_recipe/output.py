"""Native extraction source rendering and enquiry-v1 shape checks.

The definition follows Vapi's create-output API (checked 2026-10-06):
https://docs.vapi.ai/api-reference/structured-outputs/structured-output-controller-create
Custom extraction uses documented transcript/structuredOutput.schema Liquid
variables. minMessages count 0 removes the runtime default minimum. Offline
checks neither run an extractor nor establish transcript semantics/API acceptance.
"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import re

from .deployment import digest
from .recipe import RecipeError, _json, load_instance, package_path


SCHEMA_DIGEST = "f304b725ddd73019f564a84fea7cb2c422b53073df8f658ed4a32e6e73dddd19"
DEFINITION_PATH = "config/structured-output.json"


def render_output(root: Path, slug: str) -> dict:
    """Produce API intent without a provider ID or fictional binding.

    The result schema is injected unchanged from its sole instance-owned source.
    Changing the v1 contract requires a separately reviewed version/migration.
    """
    instance = load_instance(root, slug)
    definition = _json(package_path(root, DEFINITION_PATH))
    if set(definition) != {"name", "description", "type", "model", "conditions"}:
        raise RecipeError("Output source requires only name, description, type, model and conditions.")
    name = definition.get("name")
    if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name):
        raise RecipeError("Output name must be a non-empty lowercase name.")
    full_name = f"{slug}-{name}"
    if len(full_name) > 40:
        raise RecipeError("Instance slug plus output name exceeds Vapi's 40-character limit.")
    if not isinstance(definition.get("description"), str) or not definition["description"].strip():
        raise RecipeError("Output description is required.")
    if definition.get("type") != "ai":
        raise RecipeError("Messenger extraction requires the AI output type.")
    conditions = definition.get("conditions")
    if conditions != [{"type": "minMessages", "count": 0}] or type(conditions[0]["count"]) is not int:
        raise RecipeError("Output must explicitly remove the short-call minimum with minMessages count 0.")
    model = definition.get("model")
    if not isinstance(model, dict) or set(model) != {"provider", "model", "temperature", "maxTokens", "messages"}:
        raise RecipeError("Output requires an explicit extraction model and custom messages.")
    if model["provider"] != "openai" or model["model"] != "gpt-4.1-mini":
        raise RecipeError("This reviewed output uses the separate OpenAI gpt-4.1-mini extraction model.")
    if type(model["temperature"]) not in (int, float) or model["temperature"] != 0:
        raise RecipeError("Extraction temperature must be 0.")
    if type(model["maxTokens"]) is not int or not 1 <= model["maxTokens"] <= 10000:
        raise RecipeError("Extraction maxTokens must be a positive bounded integer.")
    messages = model["messages"]
    if not isinstance(messages, list) or len(messages) != 2:
        raise RecipeError("Extraction requires its own system and user messages.")
    for message, role in zip(messages, ("system", "user")):
        if (not isinstance(message, dict) or set(message) != {"role", "content"}
                or message.get("role") != role or not isinstance(message.get("content"), str)
                or not message["content"].strip()):
            raise RecipeError("Extraction messages require non-empty system and user content.")
    text = "\n".join(message["content"] for message in messages)
    variables = re.findall(r"{{\s*([^{}]+?)\s*}}", text)
    if set(variables) != {"transcript", "structuredOutput.schema"}:
        raise RecipeError("Extraction must reference only transcript and structuredOutput.schema.")
    remainder = re.sub(r"{{\s*[^{}]+?\s*}}", "", text)
    if "{{" in remainder or "}}" in remainder or "{%" in remainder or "%}" in remainder:
        raise RecipeError("Extraction contains an unsupported Liquid expression.")
    schema = _json(package_path(root, instance["result"]["schemaRef"]))
    if digest(schema) != SCHEMA_DIGEST:
        raise RecipeError("Enquiry v1.0.0 schema differs from the agreed contract; review a version change.")
    result = deepcopy(definition)
    result["name"] = full_name
    result["schema"] = schema
    return result


def validate_result_shape(result: dict) -> None:
    """Check the fixed enquiry-v1 schema shape; never claim semantic validity.

This small contract-specific check needs no JSON Schema runtime dependency.
It enforces the existing v1 fields/conditions, including nested evidence. It
does not verify quotes, consent, chronology, latest values or extraction success.
"""
    required = {"schemaVersion", "outcome", "completeness", "callbackNumberConfirmed", "evidence", "uncertainties"}
    captured = {"callerName", "message", "callbackNumber"}
    if not isinstance(result, dict) or not required.issubset(result) or set(result) - required - captured:
        raise RecipeError("Result has missing required or unknown fields.")
    if result["schemaVersion"] != "1.0.0":
        raise RecipeError("Result schemaVersion must be 1.0.0.")
    if result["outcome"] not in ("enquiry", "declined", "unsupported", "no_message"):
        raise RecipeError("Result outcome is invalid.")
    if result["completeness"] not in ("complete", "partial", "not_applicable"):
        raise RecipeError("Result completeness is invalid.")
    if type(result["callbackNumberConfirmed"]) is not bool:
        raise RecipeError("Result callbackNumberConfirmed must be a boolean.")
    for field in captured.intersection(result):
        if not isinstance(result[field], str) or len(result[field]) < 1:
            raise RecipeError(f"Result {field} must be a non-empty string when supplied.")
    if "callbackNumber" in result and not re.fullmatch(r"\+?[0-9]{3,20}", result["callbackNumber"]):
        raise RecipeError("Result callbackNumber must contain 3–20 digits with an optional supplied plus.")
    if result["outcome"] == "enquiry":
        if result["completeness"] not in ("complete", "partial"):
            raise RecipeError("Enquiry completeness must be complete or partial.")
    elif (result["completeness"] != "not_applicable" or result["callbackNumberConfirmed"]
          or captured.intersection(result)):
        raise RecipeError("Non-enquiry results omit captured fields and use not_applicable/false.")
    if result["completeness"] == "complete" and (not captured.issubset(result) or not result["callbackNumberConfirmed"]):
        raise RecipeError("Complete results require name, message, number and confirmation.")
    if result["callbackNumberConfirmed"] and "callbackNumber" not in result:
        raise RecipeError("Confirmed result requires a callback number.")
    if not isinstance(result["evidence"], list) or not isinstance(result["uncertainties"], list):
        raise RecipeError("Evidence and uncertainties must be arrays.")
    fields = ("consent", "callerName", "message", "callbackNumber", "callbackReadback", "callbackConfirmation", "outcome")
    for item in result["evidence"]:
        if (not isinstance(item, dict) or set(item) != {"field", "speaker", "quote"}
                or item["field"] not in fields or item["speaker"] not in ("caller", "assistant")
                or not isinstance(item["quote"], str) or not item["quote"]):
            raise RecipeError("Evidence items require a permitted field, speaker and non-empty quote.")
    if any(not isinstance(item, str) or not item for item in result["uncertainties"]):
        raise RecipeError("Uncertainties must contain non-empty strings.")

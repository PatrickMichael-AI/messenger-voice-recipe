"""Offline source and synthetic result shapes, never model extraction runs."""

from copy import deepcopy
import json
from pathlib import Path
import re
import shutil
import tempfile
import unittest

import httpx

from messenger_recipe.output import (
    normalize_native_result, read_enquiry_result, render_output, validate_result_shape,
)
from messenger_recipe.provider import VapiProvider
from messenger_recipe.recipe import RecipeError


PACKAGE = Path(__file__).resolve().parents[1]
SLUG = "messenger_recipe_demo"


def quote(field, speaker, text):
    return {"field": field, "speaker": speaker, "quote": text}


COMPLETE = {
    "schemaVersion": "1.0.0", "outcome": "enquiry", "completeness": "complete",
    "callerName": "Alex", "message": "Alex would like a call about repairing Alex's chair.",
    "callbackNumber": "+12025550123", "callbackNumberConfirmed": True,
    "evidence": [quote("consent", "caller", "Yes, please."),
                 quote("callerName", "caller", "Alex."),
                 quote("message", "caller", "Please call about repairing my chair."),
                 quote("message", "assistant", "You would like a call about repairing your chair. Is that correct?"),
                 quote("message", "caller", "Yes, that is correct."),
                 quote("callbackNumber", "caller", "Plus one, two zero two, five five five, zero one two three."),
                 quote("callbackReadback", "assistant", "Plus one, two zero two, five five five, zero one two three. Is that correct?"),
                 quote("callbackConfirmation", "caller", "Yes, correct.")],
    "uncertainties": [],
}


def no_message(outcome, evidence=None):
    return {"schemaVersion": "1.0.0", "outcome": outcome, "completeness": "not_applicable",
            "callbackNumberConfirmed": False, "evidence": evidence or [], "uncertainties": []}


CORRECTED = deepcopy(COMPLETE)
CORRECTED["callbackNumber"] = "+12025550124"
CORRECTED["evidence"] += [
    quote("callbackNumber", "caller", "Sorry, the last digit is four."),
    quote("callbackNumber", "caller", "Plus one, two zero two, five five five, zero one two four."),
    quote("callbackReadback", "assistant", "Plus one, two zero two, five five five, zero one two four. Is that correct?"),
    quote("callbackConfirmation", "caller", "Yes, that's right."),
]
PARTIAL = {"schemaVersion": "1.0.0", "outcome": "enquiry", "completeness": "partial",
           "callerName": "Alex", "message": "Alex would like a call about repairing Alex's chair.",
           "callbackNumberConfirmed": False, "evidence": deepcopy(COMPLETE["evidence"][:5]),
           "uncertainties": ["No spoken callback number was supplied."]}
UNCONFIRMED = deepcopy(CORRECTED)
UNCONFIRMED.update(completeness="partial", callbackNumberConfirmed=False,
                   uncertainties=["The corrected number was not read back and confirmed."])
UNCONFIRMED["evidence"] = deepcopy(CORRECTED["evidence"][:-2])

# These transcript/result pairs document expected behavior for Phase 5 calls.
# Tests only check result shape; no transcript-to-result computation occurs.
SYNTHETIC_EXPECTATIONS = {
    "complete": {
        "transcript": "Assistant: Would you like to leave a message?\nCaller: Yes, please.\nAssistant: Your name?\nCaller: Alex.\nAssistant: Your message?\nCaller: Please call about repairing my chair.\nAssistant: You would like a call about repairing your chair. Is that correct?\nCaller: Yes, that is correct.\nAssistant: Your callback number?\nCaller: Plus one, two zero two, five five five, zero one two three.\nAssistant: Plus one, two zero two, five five five, zero one two three. Is that correct?\nCaller: Yes, correct.",
        "result": COMPLETE,
    },
    "corrected": {
        "transcript": "Assistant: Would you like to leave a message?\nCaller: Yes, please.\nCaller: Alex.\nCaller: Please call about repairing my chair.\nAssistant: You would like a call about repairing your chair. Is that correct?\nCaller: Yes, that is correct.\nCaller: Plus one, two zero two, five five five, zero one two three.\nAssistant: Plus one, two zero two, five five five, zero one two three. Is that correct?\nCaller: Yes, correct.\nCaller: Sorry, the last digit is four.\nAssistant: Please give me the full number again.\nCaller: Plus one, two zero two, five five five, zero one two four.\nAssistant: Plus one, two zero two, five five five, zero one two four. Is that correct?\nCaller: Yes, that's right.",
        "result": CORRECTED,
    },
    "partial": {
        "transcript": "Assistant: Would you like to leave a message?\nCaller: Yes, please.\nCaller: Alex.\nCaller: Please call about repairing my chair.\nAssistant: You would like a call about repairing your chair. Is that correct?\nCaller: Yes, that is correct.\nCaller: Actually, I'll call back later.",
        "result": PARTIAL,
    },
    "corrected_unconfirmed": {
        "transcript": "Assistant: Would you like to leave a message?\nCaller: Yes, please.\nCaller: Alex.\nCaller: Please call about repairing my chair.\nAssistant: You would like a call about repairing your chair. Is that correct?\nCaller: Yes, that is correct.\nCaller: Plus one, two zero two, five five five, zero one two three.\nAssistant: Plus one, two zero two, five five five, zero one two three. Is that correct?\nCaller: Yes, correct.\nCaller: Sorry, the last digit is four.\nAssistant: Please give me the full number again.\nCaller: Plus one, two zero two, five five five, zero one two four.",
        "result": UNCONFIRMED,
    },
    "withdrawn": {
        "transcript": "Assistant: Would you like to leave a message?\nCaller: Yes, please.\nCaller: Alex. Please call about repairing my chair.\nCaller: Actually, don't pass that on.",
        "result": no_message("declined", [quote("outcome", "caller", "Actually, don't pass that on.")]),
    },
    "unsupported": {
        "transcript": "Caller: Can you take a payment?\nAssistant: I can only take messages for the team. Would you like to leave one?\nCaller: No thanks.",
        "result": no_message("unsupported", [quote("outcome", "caller", "No thanks.")]),
    },
    "empty": {"transcript": "", "result": no_message("no_message")},
}


# These are manual extraction expectations, not a transcript-to-result model.
MESSAGE_CONFIRMATION_EXPECTATIONS = {}
for case, summary in (
    ("confirmed_summary_after_transcript_omission", "discuss Alex's idea"),
    ("later_message_correction_reconfirmed", "license Alex's idea"),
):
    result = deepcopy(COMPLETE)
    result["message"] = f"Please ask the team to {summary}."
    message_evidence = [
        quote("message", "caller", "Please ask the team to my idea."),
        quote("message", "assistant", "You would like the team to discuss your idea. Is that correct?"),
        quote("message", "caller", "Yes, that is correct."),
    ]
    if case == "later_message_correction_reconfirmed":
        message_evidence += [
            quote("message", "caller", "Actually, I want the team to license my idea."),
            quote("message", "assistant", "You would like the team to license your idea. Is that correct?"),
            quote("message", "caller", "Yes, exactly."),
        ]
    result["evidence"][2:5] = message_evidence
    MESSAGE_CONFIRMATION_EXPECTATIONS[case] = {"result": result}

unresolved = deepcopy(COMPLETE)
unresolved.pop("message")
unresolved.update(completeness="partial",
                  uncertainties=["Message meaning is unclear and no message summary was confirmed."])
unresolved["evidence"] = deepcopy(COMPLETE["evidence"][:2] + COMPLETE["evidence"][5:]) + [
    quote("message", "caller", "Please ask the team to my idea."),
    quote("message", "assistant", "You would like the team to discuss your idea. Is that correct?"),
    quote("message", "caller", "I need to go now."),
]
MESSAGE_CONFIRMATION_EXPECTATIONS["ambiguous_message_ends_before_confirmation"] = {"result": unresolved}
for expectation in MESSAGE_CONFIRMATION_EXPECTATIONS.values():
    expectation["transcript"] = "\n".join(
        f"{item['speaker'].title()}: {item['quote']}" for item in expectation["result"]["evidence"]
    )


# Team-facing message expectations are manual review fixtures. Shape checks
# cannot prove a model resolves references correctly; live calls must do that.
MESSAGE_PERSPECTIVE_EXPECTATIONS = {}
for case, name, request, summary, message in (
    ("named_caller", "Taylor", "Please ask the team to discuss my proposal.",
     "You would like the team to discuss your proposal. Is that correct?",
     "Taylor would like the team to discuss Taylor's proposal."),
    ("unnamed_caller", None, "Please ask the team to discuss my idea.",
     "You would like the team to discuss your idea. Is that correct?",
     "The caller would like the team to discuss the caller's idea."),
    ("other_person_ownership", "Jordan", "Please ask the team to discuss Sam's idea.",
     "You would like the team to discuss Sam's idea. Is that correct?",
     "Jordan would like the team to discuss Sam's idea."),
):
    result = deepcopy(COMPLETE)
    result["message"] = message
    result["evidence"][2:5] = [
        quote("message", "caller", request),
        quote("message", "assistant", summary),
        quote("message", "caller", "Yes, that is correct."),
    ]
    if name is None:
        result.pop("callerName")
        result.update(completeness="partial", uncertainties=["Caller name was not supplied."])
        del result["evidence"][1]
    else:
        result["callerName"] = name
        result["evidence"][1] = quote("callerName", "caller", name + ".")
    MESSAGE_PERSPECTIVE_EXPECTATIONS[case] = {"result": result}

ambiguous_owner = deepcopy(unresolved)
ambiguous_owner["uncertainties"] = ["The conversation does not establish whose idea is meant."]
ambiguous_owner["evidence"] = deepcopy(COMPLETE["evidence"][:2] + COMPLETE["evidence"][5:]) + [
    quote("message", "caller", "Sam and Lee have ideas. Please ask the team to discuss their idea."),
    quote("message", "assistant", "Whose idea would you like the team to discuss?"),
    quote("message", "caller", "I need to go now."),
]
MESSAGE_PERSPECTIVE_EXPECTATIONS["unresolved_owner"] = {"result": ambiguous_owner}
for expectation in MESSAGE_PERSPECTIVE_EXPECTATIONS.values():
    expectation["transcript"] = "\n".join(
        f"{item['speaker'].title()}: {item['quote']}" for item in expectation["result"]["evidence"]
    )


# Manual expectations for readback comparison, not extraction-model tests.
NUMBER_READBACK_EXPECTATIONS = {}
for case, readback in (
    ("omitted_digits_despite_yes", "2 5 5 5 0 1 2 3"),
    ("wrong_digit_despite_yes", "2 0 2 5 5 5 0 1 2 4"),
    ("reordered_digits_despite_yes", "2 0 2 5 5 5 0 2 1 3"),
    ("added_digit_despite_yes", "2 0 2 5 5 5 5 0 1 2 3"),
):
    result = deepcopy(COMPLETE)
    result.update(callbackNumber="2025550123", callbackNumberConfirmed=False,
                  completeness="partial",
                  uncertainties=["The assistant's readback does not match the latest caller-supplied number."])
    result["evidence"][5:] = [
        quote("callbackNumber", "caller", "2 0 2 0 1 2 3."),
        quote("callbackNumber", "caller", "The number is 2 0 2 5 5 5 0 1 2 3."),
        quote("callbackReadback", "assistant", readback + ". Is that correct?"),
        quote("callbackConfirmation", "caller", "Yes. Correct."),
    ]
    NUMBER_READBACK_EXPECTATIONS[case] = {"result": result}

unclear_readback = deepcopy(NUMBER_READBACK_EXPECTATIONS["omitted_digits_despite_yes"]["result"])
unclear_readback["evidence"][-2]["quote"] = "Let me read that back [unclear]. Is that correct?"
unclear_readback["uncertainties"] = ["The transcript does not establish a full matching number readback."]
NUMBER_READBACK_EXPECTATIONS["unclear_readback_despite_yes"] = {"result": unclear_readback}

recovered_readback = deepcopy(NUMBER_READBACK_EXPECTATIONS["omitted_digits_despite_yes"]["result"])
recovered_readback.update(completeness="complete", callbackNumberConfirmed=True, uncertainties=[])
recovered_readback["evidence"] += [
    quote("callbackReadback", "assistant", "2 0 2 5 5 5 0 1 2 3. Is that correct?"),
    quote("callbackConfirmation", "caller", "Yes, that is correct."),
]
NUMBER_READBACK_EXPECTATIONS["later_matching_readback_confirmed"] = {"result": recovered_readback}
for expectation in NUMBER_READBACK_EXPECTATIONS.values():
    expectation["transcript"] = "\n".join(
        f"{item['speaker'].title()}: {item['quote']}" for item in expectation["result"]["evidence"]
    )


class OutputTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "recipe"
        self.root.mkdir()
        for directory in ("templates", "config", "instances", "result"):
            shutil.copytree(PACKAGE / directory, self.root / directory)

    def mutate_source(self, mutate):
        path = self.root / "config/structured-output.json"
        source = json.loads(path.read_text())
        mutate(source)
        path.write_text(json.dumps(source) + "\n")

    def test_definition_adapts_schema_without_changing_contract_binding_or_state(self):
        output = render_output(self.root, SLUG)
        source_path = self.root / "result/enquiry-result.v1.schema.json"
        original = source_path.read_bytes()
        source = json.loads(original)
        self.assertNotIn("$schema", output["schema"])

        # The canonical schema retains its cross-field rules; the provider
        # receives a simpler shape and the normalizer enforces those rules.
        self.assertIn("allOf", source)
        self.assertNotIn("allOf", output["schema"])
        self.assertEqual(set(output["schema"]["required"]), set(source["properties"]))
        self.assertEqual(output["schema"]["properties"]["schemaVersion"]["enum"], ["1.0.0"])
        self.assertNotIn("minLength", output["schema"]["properties"]["callerName"])
        self.assertEqual(source_path.read_bytes(), original)
        self.assertEqual(output["name"], "messenger_recipe_demo-enquiry-v1")
        self.assertEqual(output["conditions"], [{"type": "minMessages", "count": 0}])
        self.assertEqual(output["model"]["model"], "gpt-4.1-mini")
        self.assertEqual(output["model"]["provider"], "openai")
        self.assertEqual(output["model"]["messages"][-1]["content"],
                         "Result schema:\n{{structuredOutput.schema}}\n\nSpoken conversation:\n{{transcript}}")
        self.assertNotIn("allOf", output["schema"])
        self.assertIs(output["schema"]["additionalProperties"], False)
        self.assertNotIn("assistantIds", output)
        self.assertNotIn("id", output)
        self.assertNotIn("compliancePlan", output)
        self.assertFalse((self.root / ".local").exists())

    def test_rendered_request_passes_captured_vapi_schema_validation(self):
        # Replay only constraints identified by the 2026-10-06 live HTTP 400.
        # This is not a model run or proof that the live API accepts all fields.
        def handler(request):
            schema = json.loads(request.content)["schema"]
            messages = []

            def check(node, path):
                if "type" not in node:
                    messages.append(f"{path}.type must be one of the following values")
                for keyword in ("if", "then", "else", "not"):
                    if keyword in node:
                        check(node[keyword], f"{path}.{keyword}")
                for keyword in ("allOf", "anyOf"):
                    for index, child in enumerate(node.get(keyword, [])):
                        check(child, f"{path}.{keyword}.{index}")

            check(schema, "schema")
            if "$schema" in schema and not isinstance(schema["$schema"], dict):
                messages.append("schema.$schema must be an object")
            if messages:
                return httpx.Response(400, json={"statusCode": 400, "error": "Bad Request", "message": messages})
            return httpx.Response(201, json={**json.loads(request.content), "id": "synthetic-output"})

        with VapiProvider("synthetic-fixture", transport=httpx.MockTransport(handler)) as provider:
            response = provider.create_output(render_output(self.root, SLUG))
        self.assertEqual(response["id"], "synthetic-output")

    def test_extraction_schema_passes_captured_openai_failure_and_required_fields(self):
        # This reaches the real renderer/HTTP seam, replaying the captured
        # extraction rejection plus documented strict-output requirements.
        def handler(request):
            schema = json.loads(request.content)["schema"]

            def check(node):
                for keyword in ("allOf", "if", "then", "else", "not", "const"):
                    if keyword in node:
                        raise ValueError(f"Invalid schema: '{keyword}' is not permitted")
                self.assertIn(node.get("type"), ("object", "array", "string", "boolean"))
                if node["type"] == "object":
                    self.assertEqual(set(node["required"]), set(node["properties"]))
                    self.assertIs(node["additionalProperties"], False)
                    for child in node["properties"].values():
                        check(child)
                if node["type"] == "array":
                    check(node["items"])

            check(schema)
            return httpx.Response(201, json={**json.loads(request.content), "id": "synthetic-output"})

        with VapiProvider("synthetic-fixture", transport=httpx.MockTransport(handler)) as provider:
            response = provider.create_output(render_output(self.root, SLUG))
        self.assertEqual(response["id"], "synthetic-output")

    def test_wrong_variables_and_missing_context_are_rejected(self):
        path = self.root / "config/structured-output.json"
        original = path.read_text()
        for text in ("{{transcript}}", "{{structuredOutput.schema}}", "{{transcript}} {{structuredOutput.schema}} {{customer.number}}",
                     "{{transcript}} {{structuredOutput.schema}} {% if true %}"):
            with self.subTest(text=text):
                path.write_text(original)
                self.mutate_source(lambda source: source["model"]["messages"][-1].update(content=text))
                with self.assertRaises(RecipeError):
                    render_output(self.root, SLUG)

    def test_existing_output_patch_uses_documented_schema_override_and_exact_payload(self):
        requests = []
        payload = render_output(self.root, SLUG)

        def handler(request):
            requests.append(request)
            return httpx.Response(200, json={**payload, "id": "synthetic-output"})

        with VapiProvider("synthetic-fixture", transport=httpx.MockTransport(handler)) as provider:
            response = provider.update_output("synthetic-output", payload)
        self.assertEqual(response["id"], "synthetic-output")
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].method, "PATCH")
        self.assertEqual(str(requests[0].url), "https://api.vapi.ai/structured-output/synthetic-output?schemaOverride=true")
        self.assertEqual(json.loads(requests[0].content), payload)

    def test_unsupported_creation_fields_and_short_call_conditions_are_rejected(self):
        path = self.root / "config/structured-output.json"
        original = path.read_text()
        for field, value in (("id", "invented"), ("assistantIds", ["invented"]),
                             ("conditions", []), ("conditions", None),
                             ("conditions", [{"type": "minMessages", "count": False}])):
            with self.subTest(field=field, value=value):
                path.write_text(original)
                self.mutate_source(lambda source: source.update({field: value}))
                with self.assertRaises(RecipeError):
                    render_output(self.root, SLUG)

    def test_silent_schema_contract_change_is_rejected(self):
        path = self.root / "result/enquiry-result.v1.schema.json"
        schema = json.loads(path.read_text())
        schema.pop("allOf")
        path.write_text(json.dumps(schema) + "\n")
        with self.assertRaises(RecipeError):
            render_output(self.root, SLUG)

    def test_portable_number_pattern_matches_the_existing_local_number_rule(self):
        schema = json.loads((self.root / "result/enquiry-result.v1.schema.json").read_text())
        pattern = schema["properties"]["callbackNumber"]["pattern"]
        for value in ("2025550123", "+12025550123", "123"):
            with self.subTest(value=value):
                self.assertIsNotNone(re.fullmatch(pattern, value))
        for value in ("+12 025550123", "12", "unknown", "\\2025550123"):
            with self.subTest(value=value):
                self.assertIsNone(re.fullmatch(pattern, value))

    def test_named_synthetic_result_shapes_without_extraction_claim(self):
        for name, expectation in SYNTHETIC_EXPECTATIONS.items():
            with self.subTest(name=name):
                validate_result_shape(expectation["result"])
        self.assertNotIn("callbackNumber", PARTIAL)
        self.assertEqual(CORRECTED["callbackNumber"], "+12025550124")
        self.assertIs(UNCONFIRMED["callbackNumberConfirmed"], False)
        self.assertFalse({"callerName", "message", "callbackNumber"}.intersection(
            SYNTHETIC_EXPECTATIONS["withdrawn"]["result"]))

    def test_confirmation_expectations_preserve_v1_shape_without_model_claim(self):
        for name, expectation in {**MESSAGE_CONFIRMATION_EXPECTATIONS,
                                  **MESSAGE_PERSPECTIVE_EXPECTATIONS,
                                  **NUMBER_READBACK_EXPECTATIONS}.items():
            with self.subTest(name=name):
                expected = expectation["result"]
                validate_result_shape(expected)
                wire = deepcopy(expected)
                for field in ("callerName", "message", "callbackNumber"):
                    wire.setdefault(field, "")
                self.assertEqual(normalize_native_result(wire), expected)
        # Confirmed digits cannot make an unresolved, absent message complete.
        contradictory = deepcopy(unresolved)
        contradictory.update(completeness="complete", message="")
        with self.assertRaises(RecipeError):
            normalize_native_result(contradictory)

    def test_complete_and_confirmed_shape_requirements_are_enforced(self):
        for field in ("callerName", "message", "callbackNumber"):
            result = deepcopy(COMPLETE)
            result.pop(field)
            with self.subTest(field=field), self.assertRaises(RecipeError):
                validate_result_shape(result)
        result = deepcopy(COMPLETE)
        result["callbackNumberConfirmed"] = False
        with self.assertRaises(RecipeError):
            validate_result_shape(result)

    def test_non_enquiry_results_suppress_captured_fields(self):
        for outcome in ("declined", "unsupported", "no_message"):
            for field in ("callerName", "message", "callbackNumber"):
                result = no_message(outcome)
                result[field] = COMPLETE[field]
                with self.subTest(outcome=outcome, field=field), self.assertRaises(RecipeError):
                    validate_result_shape(result)

    def test_unknown_metadata_null_nested_fields_and_invalid_numbers_are_rejected(self):
        mutations = [lambda value: value.update(callId="invented"),
                     lambda value: value.update(message=None),
                     lambda value: value.update(callbackNumber="+12 025550123"),
                     lambda value: value.update(callbackNumberConfirmed=1),
                     lambda value: value.update(uncertainties=[""]),
                     lambda value: value["evidence"][0].update(speaker="system"),
                     lambda value: value["evidence"][0].update(field="providerId"),
                     lambda value: value["evidence"][0].update(callId="invented"),
                     lambda value: value.update(evidence=None)]
        for index, mutate in enumerate(mutations):
            result = deepcopy(COMPLETE)
            mutate(result)
            with self.subTest(index=index), self.assertRaises(RecipeError):
                validate_result_shape(result)

    def test_provider_empty_placeholders_restore_all_named_canonical_results(self):
        for name, expected in SYNTHETIC_EXPECTATIONS.items():
            canonical = expected["result"]
            raw = deepcopy(canonical)
            for field in ("callerName", "message", "callbackNumber"):
                raw.setdefault(field, "")
            original = deepcopy(raw)
            with self.subTest(case=name):
                self.assertEqual(normalize_native_result(raw), canonical)
                self.assertEqual(raw, original)
                self.assertEqual(normalize_native_result(canonical), canonical)

    def test_normalization_never_repairs_contradictions_or_invalid_values(self):
        invalid = []
        for field in ("callerName", "message", "callbackNumber"):
            value = deepcopy(COMPLETE)
            value[field] = ""
            invalid.append(value)
            value = deepcopy(PARTIAL)
            value[field] = None
            invalid.append(value)
        for outcome in ("declined", "unsupported", "no_message"):
            value = no_message(outcome)
            value.update(callerName="Alex", message="", callbackNumber="")
            invalid.append(value)
        for field in ("schemaVersion", "outcome", "completeness", "callbackNumberConfirmed", "evidence", "uncertainties"):
            value = deepcopy(COMPLETE)
            del value[field]
            invalid.append(value)
        invalid.extend([None, [], dict(COMPLETE, callbackNumber="123 456"),
                        dict(COMPLETE, callbackNumberConfirmed=1), dict(COMPLETE, unexpected=None)])
        for index, value in enumerate(invalid):
            with self.subTest(index=index), self.assertRaises(RecipeError):
                normalize_native_result(value)

    def test_shape_validation_does_not_pretend_to_validate_quotes(self):
        result = deepcopy(COMPLETE)
        result["evidence"][0]["quote"] = "This quote is absent from the synthetic transcript."
        validate_result_shape(result)
        # Actual quote matching, consent and latest-value checks belong to call proof.

    def test_enquiry_result_reading_distinguishes_absent_wrong_bound_failed_and_malformed(self):
        cases = [(None, "missing_native_result"), ({}, "missing_native_result"),
                 ({"foreign-output": {"result": {}}}, "wrong_output_binding"),
                 ({"output-fixture": {"structuredOutputId": "foreign-output", "result": {}}}, "wrong_output_binding"),
                 ([], "malformed_native_result"),
                 ({"output-fixture": {"error": "Provider failure"}}, "failed_native_result"),
                 ({"output-fixture": {"status": "failed"}}, "failed_native_result"),
                 ({"output-fixture": {"result": None}}, "missing_native_result"),
                 ({"output-fixture": {"result": "not json"}}, "malformed_native_result"),
                 ({"output-fixture": {"result": {}}}, "malformed_native_result"),
                 ({"output-fixture": []}, "malformed_native_result")]
        for outputs, status in cases:
            with self.subTest(status=status, outputs=outputs):
                call = {"id": "call-fixture", "artifact": {"structuredOutputs": outputs}}
                self.assertEqual(read_enquiry_result(call, "output-fixture"), {"status": status})
        self.assertEqual(read_enquiry_result({"id": "call-fixture"}, "output-fixture"),
                         {"status": "missing_native_result"})
        self.assertEqual(read_enquiry_result({"artifact": "invalid"}, "output-fixture"),
                         {"status": "malformed_native_result"})

    def test_enquiry_result_reading_returns_canonical_result_and_provider_metadata(self):
        raw = dict(no_message("declined"), callerName="", message="", callbackNumber="")
        call = {"artifact": {"structuredOutputs": {"output-fixture": {
            "result": raw, "version": 7, "versionId": True, "structuredOutputId": "output-fixture"}}}}
        original = deepcopy(call)
        self.assertEqual(read_enquiry_result(call, "output-fixture"), {
            "providerOutputMetadata": {"version": 7}, "result": no_message("declined"),
            "status": "valid_shape_semantics_unproved"})
        self.assertEqual(call, original)
        failed = {"artifact": {"structuredOutputs": {"output-fixture": {"status": "failed", "version": "v2"}}}}
        self.assertEqual(read_enquiry_result(failed, "output-fixture"),
                         {"providerOutputMetadata": {"version": "v2"}, "status": "failed_native_result"})


if __name__ == "__main__":
    unittest.main()

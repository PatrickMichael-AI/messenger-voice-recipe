"""Offline source and synthetic result shapes, never model extraction runs."""

from copy import deepcopy
import json
from pathlib import Path
import shutil
import tempfile
import unittest

from messenger_recipe.output import render_output, validate_result_shape
from messenger_recipe.recipe import RecipeError


PACKAGE = Path(__file__).resolve().parents[1]
SLUG = "messenger_recipe_demo"


def quote(field, speaker, text):
    return {"field": field, "speaker": speaker, "quote": text}


COMPLETE = {
    "schemaVersion": "1.0.0", "outcome": "enquiry", "completeness": "complete",
    "callerName": "Alex", "message": "Please call about repairing my chair.",
    "callbackNumber": "+12025550123", "callbackNumberConfirmed": True,
    "evidence": [quote("consent", "caller", "Yes, please."),
                 quote("callerName", "caller", "Alex."),
                 quote("message", "caller", "Please call about repairing my chair."),
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
           "callerName": "Alex", "message": "Please call about repairing my chair.",
           "callbackNumberConfirmed": False, "evidence": deepcopy(COMPLETE["evidence"][:3]),
           "uncertainties": ["No spoken callback number was supplied."]}
UNCONFIRMED = deepcopy(CORRECTED)
UNCONFIRMED.update(completeness="partial", callbackNumberConfirmed=False,
                   uncertainties=["The corrected number was not read back and confirmed."])
UNCONFIRMED["evidence"] = deepcopy(CORRECTED["evidence"][:-2])

# These transcript/result pairs document expected behavior for Phase 5 calls.
# Tests only check result shape; no transcript-to-result computation occurs.
SYNTHETIC_EXPECTATIONS = {
    "complete": {
        "transcript": "Assistant: Would you like to leave a message?\nCaller: Yes, please.\nAssistant: Your name?\nCaller: Alex.\nAssistant: Your message?\nCaller: Please call about repairing my chair.\nAssistant: Your callback number?\nCaller: Plus one, two zero two, five five five, zero one two three.\nAssistant: Plus one, two zero two, five five five, zero one two three. Is that correct?\nCaller: Yes, correct.",
        "result": COMPLETE,
    },
    "corrected": {
        "transcript": "Assistant: Would you like to leave a message?\nCaller: Yes, please.\nCaller: Alex.\nCaller: Please call about repairing my chair.\nCaller: Plus one, two zero two, five five five, zero one two three.\nAssistant: Plus one, two zero two, five five five, zero one two three. Is that correct?\nCaller: Yes, correct.\nCaller: Sorry, the last digit is four.\nAssistant: Please give me the full number again.\nCaller: Plus one, two zero two, five five five, zero one two four.\nAssistant: Plus one, two zero two, five five five, zero one two four. Is that correct?\nCaller: Yes, that's right.",
        "result": CORRECTED,
    },
    "partial": {
        "transcript": "Assistant: Would you like to leave a message?\nCaller: Yes, please.\nCaller: Alex.\nCaller: Please call about repairing my chair.\nCaller: Actually, I'll call back later.",
        "result": PARTIAL,
    },
    "corrected_unconfirmed": {
        "transcript": "Assistant: Would you like to leave a message?\nCaller: Yes, please.\nCaller: Alex.\nCaller: Please call about repairing my chair.\nCaller: Plus one, two zero two, five five five, zero one two three.\nAssistant: Plus one, two zero two, five five five, zero one two three. Is that correct?\nCaller: Yes, correct.\nCaller: Sorry, the last digit is four.\nAssistant: Please give me the full number again.\nCaller: Plus one, two zero two, five five five, zero one two four.",
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

    def test_definition_injects_unchanged_schema_without_binding_or_state(self):
        output = render_output(self.root, SLUG)
        self.assertEqual(output["schema"], json.loads((PACKAGE / "result/enquiry-result.v1.schema.json").read_text()))
        self.assertEqual(output["name"], "messenger_recipe_demo-enquiry-v1")
        self.assertEqual(output["conditions"], [{"type": "minMessages", "count": 0}])
        self.assertEqual(output["model"]["model"], "gpt-4.1-mini")
        self.assertEqual(output["model"]["provider"], "openai")
        self.assertEqual(output["model"]["messages"][-1]["content"],
                         "Result schema:\n{{structuredOutput.schema}}\n\nSpoken conversation:\n{{transcript}}")
        self.assertIn("allOf", output["schema"])
        self.assertIs(output["schema"]["additionalProperties"], False)
        self.assertNotIn("assistantIds", output)
        self.assertNotIn("id", output)
        self.assertNotIn("compliancePlan", output)
        self.assertFalse((self.root / ".local").exists())

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

    def test_named_synthetic_result_shapes_without_extraction_claim(self):
        for name, expectation in SYNTHETIC_EXPECTATIONS.items():
            with self.subTest(name=name):
                validate_result_shape(expectation["result"])
        self.assertNotIn("callbackNumber", PARTIAL)
        self.assertEqual(CORRECTED["callbackNumber"], "+12025550124")
        self.assertIs(UNCONFIRMED["callbackNumberConfirmed"], False)
        self.assertFalse({"callerName", "message", "callbackNumber"}.intersection(
            SYNTHETIC_EXPECTATIONS["withdrawn"]["result"]))

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

    def test_shape_validation_does_not_pretend_to_validate_quotes(self):
        result = deepcopy(COMPLETE)
        result["evidence"][0]["quote"] = "This quote is absent from the synthetic transcript."
        validate_result_shape(result)
        # Actual quote matching, consent and latest-value checks belong to call proof.


if __name__ == "__main__":
    unittest.main()

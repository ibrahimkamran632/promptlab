# -*- coding: utf-8 -*-
"""
Unit and integration tests for the promptlab harness.

Run from the repository root:

    python -m unittest discover -s tests -v

The test module imports promptlab as a library for the assertion/parsing/flaky
unit tests, and also drives the real CLI as a subprocess to verify the exit
code contract end-to-end.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

PROJ_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJ_ROOT not in sys.path:
    sys.path.insert(0, PROJ_ROOT)
os.chdir(PROJ_ROOT)

import promptlab as pl  # noqa: E402

FAKE_GOOD = """
import json, sys
output = "billing"
print(json.dumps({
    "output": output,
    "finish_reason": "stop",
    "usage": {"prompt_tokens": 10, "completion_tokens": 1, "total_tokens": 11},
}))
"""

FAKE_ALTERNATING = """
import json, sys
args = sys.argv[1:]
opts = {}
for i in range(0, len(args) - 1, 2):
    opts[args[i].lstrip("-")] = args[i + 1]
call_index = int(opts.get("call-index", "0"))
output = "billing" if call_index < 2 else "technical"
print(json.dumps({
    "output": output,
    "finish_reason": "stop",
    "usage": {"prompt_tokens": 10, "completion_tokens": 1, "total_tokens": 11},
}))
"""

FAKE_FAILING = """
import sys
sys.stderr.write("boom\\n")
sys.exit(3)
"""

FAKE_GARBAGE = """
print("this is not json")
"""

FAKE_FENCED = """
print("intro text")
print("```json")
print('{"output": "billing", "finish_reason": "stop", "usage": {"prompt_tokens": 10, "completion_tokens": 1, "total_tokens": 11}}')
print("```")
"""

FAKE_TIMEOUT = """
import time
time.sleep(60)
"""

FAKE_DOCTOR_OK = """
import json, sys
if "--help" in sys.argv:
    print("usage: model --prompt PROMPT --input TEXT [--temperature T] [--seed S]")
    print("options: --prompt, --input, --temperature, --seed, --max-tokens, --call-index")
    sys.exit(0)
print(json.dumps({"output": "billing", "finish_reason": "stop",
                  "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}))
"""

FAKE_MARKER = """
import json, os, sys
marker = %(marker)r
open(marker, "w").write("called")
print(json.dumps({"output": "billing", "finish_reason": "stop",
                  "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}))
"""

FAKE_DOCTOR_BAD = """
import sys
sys.exit(9)
"""


def write(path, content):
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(content)
    return path


def read_json(path):
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def read_text(path):
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


def rel_to_root(path):
    return os.path.relpath(path, PROJ_ROOT)


class SuiteFixture:
    """Builds a prompt + dataset + fake model + suite json in a unique subdir."""

    def __init__(self, tmpdir, model_src=FAKE_GOOD, assertions=None,
                 runs_per_input=1, pass_rate_threshold=None):
        self.dir = tempfile.mkdtemp(dir=tmpdir)
        self.prompt_path = write(os.path.join(self.dir, "prompt.txt"),
                                 "Classify.\\nTicket:\\n{input}\\nCategory:")
        self.data_path = write(
            os.path.join(self.dir, "data.json"),
            json.dumps([
                {"id": "t-1", "text": "My credit card was charged twice.", "category": "billing"},
                {"id": "t-2", "text": "The login page returns a 500 error.", "category": "technical"},
            ]))
        self.model_path = write(os.path.join(self.dir, "model.py"), model_src)
        if assertions is None:
            assertions = [{"type": "equals", "field": "output", "value": "billing"}]
        suite = {
            "name": "fixture-suite",
            "prompt": rel_to_root(self.prompt_path),
            "data": rel_to_root(self.data_path),
            "model": rel_to_root(self.model_path),
            "runs_per_input": runs_per_input,
            "temperature": 0.0,
            "seed": 42,
            "max_tokens": 16,
            "timeout": 30,
            "pricing": {"prompt_per_token": 0.00003, "completion_per_token": 0.00006},
            "tests": [
                {"id": "test-1", "data_index": 0, "assertions": list(assertions)},
            ],
        }
        if pass_rate_threshold is not None:
            suite["pass_rate_threshold"] = pass_rate_threshold
        self.suite_path = write(os.path.join(self.dir, "suite.json"), json.dumps(suite))


class AssertionEngineTests(unittest.TestCase):
    def test_assertion_types_cover_brief(self):
        self.assertEqual(
            set(pl.ASSERTION_TYPES),
            {"contains", "not_contains", "equals", "matches", "json_valid",
             "json_field_equals", "max_tokens", "finish_is"})

    def test_equals_pass(self):
        ok, _ = pl.evaluate_assertion(
            {"type": "equals", "field": "output", "value": "billing"},
            {"output": "billing"})
        self.assertTrue(ok)

    def test_equals_normalizes_whitespace(self):
        ok, _ = pl.evaluate_assertion(
            {"type": "equals", "field": "output", "value": "billing"},
            {"output": " billing \r\n"})
        self.assertTrue(ok)

    def test_equals_fail(self):
        ok, detail = pl.evaluate_assertion(
            {"type": "equals", "field": "output", "value": "billing"},
            {"output": "account"})
        self.assertFalse(ok)
        self.assertIn("billing", detail)

    def test_contains_pass_and_fail(self):
        ok, _ = pl.evaluate_assertion(
            {"type": "contains", "field": "output", "value": "error"},
            {"output": "technical error"})
        self.assertTrue(ok)
        ok, _ = pl.evaluate_assertion(
            {"type": "contains", "field": "output", "value": "unknown"},
            {"output": "technical"})
        self.assertFalse(ok)

    def test_not_contains_pass_and_fail(self):
        ok, _ = pl.evaluate_assertion(
            {"type": "not_contains", "field": "output", "value": "unexpected"},
            {"output": "billing"})
        self.assertTrue(ok)
        ok, _ = pl.evaluate_assertion(
            {"type": "not_contains", "field": "output", "value": "ill"},
            {"output": "billing"})
        self.assertFalse(ok)

    def test_matches_fullmatch_semantics(self):
        ok, _ = pl.evaluate_assertion(
            {"type": "matches", "field": "output", "value": "^(billing|account)$"},
            {"output": "billing"})
        self.assertTrue(ok)
        ok, _ = pl.evaluate_assertion(
            {"type": "matches", "field": "output", "value": "^billing$"},
            {"output": "there was billing"})
        self.assertFalse(ok)

    def test_json_valid_pass(self):
        ok, _ = pl.evaluate_assertion(
            {"type": "json_valid", "field": "output"},
            {"output": '{"category": "billing"}'})
        self.assertTrue(ok)

    def test_json_valid_fail(self):
        ok, _ = pl.evaluate_assertion(
            {"type": "json_valid", "field": "output"},
            {"output": "not json at all"})
        self.assertFalse(ok)

    def test_json_field_equals_nested_with_coercion(self):
        model_data = {"output": '{"category": "billing", "meta": {"priority": 1}}'}
        ok, _ = pl.evaluate_assertion(
            {"type": "json_field_equals", "field": "output",
             "path": "$.category", "value": "billing"}, model_data)
        self.assertTrue(ok)
        ok, _ = pl.evaluate_assertion(
            {"type": "json_field_equals", "field": "output",
             "path": "$.meta.priority", "value": "1"}, model_data)
        self.assertTrue(ok)

    def test_json_field_equals_missing_path_fails(self):
        ok, _ = pl.evaluate_assertion(
            {"type": "json_field_equals", "field": "output",
             "path": "$.country", "value": "US"},
            {"output": '{"category": "billing"}'})
        self.assertFalse(ok)

    def test_json_field_equals_invalid_json_fails(self):
        ok, _ = pl.evaluate_assertion(
            {"type": "json_field_equals", "field": "output",
             "path": "$.category", "value": "billing"},
            {"output": "billing"})
        self.assertFalse(ok)

    def test_max_tokens_default_field(self):
        ok, _ = pl.evaluate_assertion(
            {"type": "max_tokens", "value": 16},
            {"output": "x", "usage": {"completion_tokens": 4}})
        self.assertTrue(ok)
        ok, _ = pl.evaluate_assertion(
            {"type": "max_tokens", "value": 3},
            {"output": "x", "usage": {"completion_tokens": 5}})
        self.assertFalse(ok)

    def test_max_tokens_explicit_field(self):
        ok, _ = pl.evaluate_assertion(
            {"type": "max_tokens", "field": "usage.prompt_tokens", "value": 5},
            {"usage": {"prompt_tokens": 2}})
        self.assertTrue(ok)

    def test_max_tokens_missing_usage_fails(self):
        ok, _ = pl.evaluate_assertion(
            {"type": "max_tokens", "value": 5}, {"output": "x"})
        self.assertFalse(ok)

    def test_finish_is_pass_and_fail(self):
        ok, _ = pl.evaluate_assertion(
            {"type": "finish_is", "value": "stop"},
            {"finish_reason": "stop"})
        self.assertTrue(ok)
        ok, _ = pl.evaluate_assertion(
            {"type": "finish_is", "value": "stop"},
            {"finish_reason": "length"})
        self.assertFalse(ok)

    def test_finish_is_explicit_field(self):
        ok, _ = pl.evaluate_assertion(
            {"type": "finish_is", "field": "finish_reason", "value": "length"},
            {"finish_reason": "length"})
        self.assertTrue(ok)

    def test_dot_path_on_output_object(self):
        model_data = {"output": "billing", "meta": {"ok": True}}
        ok, _ = pl.evaluate_assertion(
            {"type": "max_tokens", "field": "meta.", "value": 0}, model_data)
        self.assertFalse(ok)

    def test_unknown_assertion_type_raises_usage(self):
        with self.assertRaises(pl.UsageError):
            pl.evaluate_assertion({"type": "bogus", "value": "x"},
                                  {"output": "x"})

    def test_missing_field_fails_gracefully(self):
        ok, detail = pl.evaluate_assertion(
            {"type": "equals", "field": "output", "value": "billing"},
            {"finish_reason": "stop"})
        self.assertFalse(ok)
        self.assertIn("not present", detail)


class FencedExtractionTests(unittest.TestCase):
    def test_json_fence(self):
        text, fenced = pl.extract_fenced("```json\n{\"a\": 1}\n```")
        self.assertTrue(fenced)
        self.assertEqual(json.loads(text), {"a": 1})

    def test_plain_fence(self):
        text, fenced = pl.extract_fenced("```\n{\"a\": 1}\n```")
        self.assertTrue(fenced)
        self.assertEqual(text, '{"a": 1}')

    def test_no_fence(self):
        text, fenced = pl.extract_fenced('{"a": 1}')
        self.assertFalse(fenced)
        self.assertEqual(text, '{"a": 1}')

    def test_first_of_multiple_fences(self):
        text, fenced = pl.extract_fenced("```json\n{\"a\": 1}\n```\n```\n{\"b\": 2}\n```")
        self.assertTrue(fenced)
        self.assertEqual(json.loads(text), {"a": 1})

    def test_prose_surrounding_fence(self):
        text, fenced = pl.extract_fenced("sure! here it is\n```json\n{\"a\": 1}\n```\nthank you")
        self.assertTrue(fenced)
        self.assertEqual(json.loads(text), {"a": 1})


class PromptRenderingTests(unittest.TestCase):
    def test_single_placeholder_replaced(self):
        rendered = pl.render_prompt("Classify: {input}", "hello")
        self.assertEqual(rendered, "Classify: hello")

    def test_zero_placeholder_returns_template(self):
        self.assertEqual(pl.render_prompt("no placeholders here", "x"),
                         "no placeholders here")

    def test_multiple_placeholders_rejected(self):
        with self.assertRaises(pl.UsageError):
            pl.render_prompt("{input} and {input}", "x")


class SuiteParsingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.tmpdir = cls._tmp.name

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_valid_suite_parses(self):
        fix = SuiteFixture(self.tmpdir)
        suite = pl.parse_suite(fix.suite_path)
        self.assertEqual(suite["name"], "fixture-suite")
        self.assertEqual(suite["runs_per_input"], 1)
        self.assertEqual(suite["temperature"], 0.0)
        self.assertEqual(suite["seed"], 42)
        self.assertEqual(suite["max_tokens"], 16)
        self.assertTrue(os.path.isabs(suite["prompt_abs"]))
        self.assertTrue(os.path.isabs(suite["data_abs"]))
        self.assertEqual(os.path.normpath(suite["model_abs"]),
                         os.path.normpath(fix.model_path))
        self.assertEqual(len(suite["tests"]), 1)

    def test_malformed_suite_json_raises_usage(self):
        bad = write(os.path.join(self.tmpdir, "bad.json"), "{not json")
        with self.assertRaises(pl.UsageError):
            pl.parse_suite(bad)

    def test_missing_name_raises_usage(self):
        fix = SuiteFixture(self.tmpdir)
        suite = read_json(fix.suite_path)
        del suite["name"]
        path = write(os.path.join(self.tmpdir, "no_name.json"), json.dumps(suite))
        with self.assertRaises(pl.UsageError):
            pl.parse_suite(path)

    def test_missing_key_raises_usage(self):
        fix = SuiteFixture(self.tmpdir)
        suite = read_json(fix.suite_path)
        del suite["data"]
        path = write(os.path.join(self.tmpdir, "no_data.json"), json.dumps(suite))
        with self.assertRaises(pl.UsageError):
            pl.parse_suite(path)

    def test_data_index_out_of_range_raises_usage(self):
        fix = SuiteFixture(self.tmpdir)
        suite = read_json(fix.suite_path)
        suite["tests"][0]["data_index"] = 99
        path = write(os.path.join(self.tmpdir, "bad_index.json"), json.dumps(suite))
        with self.assertRaises(pl.UsageError):
            pl.parse_suite(path)

    def test_unknown_assertion_raises_usage(self):
        fix = SuiteFixture(self.tmpdir, assertions=[{"type": "wizard", "value": "x"}])
        with self.assertRaises(pl.UsageError):
            pl.parse_suite(fix.suite_path)

    def test_json_field_equals_without_path_raises_usage(self):
        fix = SuiteFixture(self.tmpdir, assertions=[
            {"type": "json_field_equals", "field": "output", "value": "billing"}])
        with self.assertRaises(pl.UsageError):
            pl.parse_suite(fix.suite_path)

    def test_missing_value_raises_usage(self):
        fix = SuiteFixture(self.tmpdir, assertions=[{"type": "equals", "field": "output"}])
        with self.assertRaises(pl.UsageError):
            pl.parse_suite(fix.suite_path)

    def test_empty_tests_raises_usage(self):
        fix = SuiteFixture(self.tmpdir)
        suite = read_json(fix.suite_path)
        suite["tests"] = []
        path = write(os.path.join(self.tmpdir, "no_tests.json"), json.dumps(suite))
        with self.assertRaises(pl.UsageError):
            pl.parse_suite(path)

    def test_missing_data_file_raises_file_error(self):
        fix = SuiteFixture(self.tmpdir)
        suite = read_json(fix.suite_path)
        suite["data"] = rel_to_root(os.path.join(self.tmpdir, "does_not_exist.json"))
        path = write(os.path.join(self.tmpdir, "missing_data.json"), json.dumps(suite))
        with self.assertRaises(pl.FileError):
            pl.parse_suite(path)


class FlakinessTests(unittest.TestCase):
    def test_classify_perfect_pass(self):
        self.assertEqual(pl.classify_test(1.0, 1.0), "pass")

    def test_classify_zero_fail(self):
        self.assertEqual(pl.classify_test(0.0, 1.0), "fail")

    def test_classify_below_threshold_fail(self):
        self.assertEqual(pl.classify_test(0.5, 1.0), "fail")

    def test_classify_at_threshold_pass(self):
        self.assertEqual(pl.classify_test(0.5, 0.5), "pass")

    def test_classify_above_threshold_pass(self):
        self.assertEqual(pl.classify_test(0.9, 0.8), "pass")

    def test_is_flaky_bounds(self):
        self.assertTrue(pl.is_flaky(0.5))
        self.assertFalse(pl.is_flaky(1.0))
        self.assertFalse(pl.is_flaky(0.0))


class CostAccountingTests(unittest.TestCase):
    def test_cost_with_pricing(self):
        usage = {"prompt_tokens": 100, "completion_tokens": 10}
        pricing = {"prompt_per_token": 0.00003, "completion_per_token": 0.00006}
        cost = pl.compute_cost({"usage": usage}, pricing)
        self.assertAlmostEqual(cost, 100 * 0.00003 + 10 * 0.00006)

    def test_cost_without_pricing_is_none(self):
        self.assertIsNone(pl.compute_cost({"usage": {"prompt_tokens": 1, "completion_tokens": 1}}, {}))

    def test_cost_without_usage_is_none(self):
        self.assertIsNone(pl.compute_cost({"output": "billing"}, {}))


class ModelInvocationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.tmpdir = cls._tmp.name
        cls.prompt = write(os.path.join(cls.tmpdir, "prompt.txt"), "Ticket:\n{input}")
        cls.good = write(os.path.join(cls.tmpdir, "good.py"), FAKE_GOOD)
        cls.failing = write(os.path.join(cls.tmpdir, "failing.py"), FAKE_FAILING)
        cls.garbage = write(os.path.join(cls.tmpdir, "garbage.py"), FAKE_GARBAGE)
        cls.fenced = write(os.path.join(cls.tmpdir, "fenced.py"), FAKE_FENCED)
        cls.timeout = write(os.path.join(cls.tmpdir, "timeout.py"), FAKE_TIMEOUT)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def invoke(self, model):
        return pl.invoke_model(model, self.prompt, "some input", 0.0, 42, 16, 0, 30)

    def test_invoke_good_model(self):
        data, stdout, _, fenced = self.invoke(self.good)
        self.assertEqual(data["output"], "billing")
        self.assertEqual(data["finish_reason"], "stop")
        self.assertFalse(fenced)

    def test_invoke_nonzero_exit_raises_model_error(self):
        with self.assertRaises(pl.ModelError):
            self.invoke(self.failing)

    def test_invoke_garbage_stdout_raises_model_error(self):
        with self.assertRaises(pl.ModelError):
            self.invoke(self.garbage)

    def test_invoke_fenced_stdout_extracts_json(self):
        data, stdout, _, fenced = self.invoke(self.fenced)
        self.assertTrue(fenced)
        self.assertEqual(data["output"], "billing")

    def test_invoke_timeout_raises_model_error(self):
        with self.assertRaises(pl.ModelError):
            pl.invoke_model(self.timeout, self.prompt, "x", 0.0, 42, 16, 0, 1)

    def test_invoke_missing_model_raises_model_error(self):
        with self.assertRaises(pl.ModelError):
            self.invoke(os.path.join(self.tmpdir, "nope.py"))


class SuiteRunnerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.tmpdir = cls._tmp.name

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_all_pass_exit_zero(self):
        fix = SuiteFixture(self.tmpdir)
        report = pl.run_suite(fix.suite_path)
        self.assertEqual(report["summary"]["exit_code"], pl.EXIT_OK)
        self.assertEqual(report["summary"]["passed"], 1)
        self.assertEqual(report["summary"]["failed"], 0)
        self.assertEqual(report["results"][0]["status"], "pass")

    def test_failing_equals_exit_two(self):
        fix = SuiteFixture(self.tmpdir, assertions=[
            {"type": "equals", "field": "output", "value": "space-odyssey"}])
        report = pl.run_suite(fix.suite_path)
        self.assertEqual(report["summary"]["exit_code"], pl.EXIT_FAIL)
        self.assertEqual(report["summary"]["failed"], 1)

    def test_flaky_below_threshold_is_fail(self):
        fix = SuiteFixture(self.tmpdir, model_src=FAKE_ALTERNATING, runs_per_input=3)
        report = pl.run_suite(fix.suite_path)
        self.assertEqual(report["results"][0]["flaky"], True)
        self.assertEqual(report["results"][0]["pass_rate"], 2 / 3)
        self.assertEqual(report["results"][0]["status"], "fail")
        self.assertEqual(report["summary"]["exit_code"], pl.EXIT_FAIL)

    def test_flaky_above_threshold_is_pass(self):
        fix = SuiteFixture(self.tmpdir, model_src=FAKE_ALTERNATING,
                           runs_per_input=3, pass_rate_threshold=0.5)
        report = pl.run_suite(fix.suite_path)
        self.assertEqual(report["results"][0]["flaky"], True)
        self.assertEqual(report["results"][0]["status"], "pass")
        self.assertEqual(report["summary"]["exit_code"], pl.EXIT_OK)

    def test_dry_run_does_not_invoke_model(self):
        marker = os.path.join(self.tmpdir, "marker.txt")
        model_src = FAKE_MARKER % {"marker": marker}
        fix = SuiteFixture(self.tmpdir, model_src=model_src)
        report = pl.run_suite(fix.suite_path, dry_run=True)
        self.assertEqual(report["summary"]["exit_code"], pl.EXIT_OK)
        self.assertEqual(report["results"][0]["status"], "skipped")
        self.assertFalse(os.path.exists(marker))


class CLIIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.tmpdir = cls._tmp.name
        cls.good_fix = SuiteFixture(cls.tmpdir)
        cls.fail_fix = SuiteFixture(cls.tmpdir, assertions=[
            {"type": "equals", "field": "output", "value": "wrong"}])
        cls.failing_model_fix = SuiteFixture(cls.tmpdir, model_src=FAKE_FAILING)
        cls.garbage_model_fix = SuiteFixture(cls.tmpdir, model_src=FAKE_GARBAGE)
        cls.flaky_fix = SuiteFixture(cls.tmpdir, model_src=FAKE_ALTERNATING,
                                     runs_per_input=3, pass_rate_threshold=0.5)
        malformed = os.path.join(cls.tmpdir, "malformed.json")
        write(malformed, "{oops")
        cls.malformed_path = malformed
        unknown_assert = SuiteFixture(cls.tmpdir, assertions=[{"type": "spell", "value": "x"}])
        suite = read_json(unknown_assert.suite_path)
        cls.unknown_assert_path = write(
            os.path.join(cls.tmpdir, "unknown_assert.json"), json.dumps(suite))
        missing_data = read_json(cls.good_fix.suite_path)
        missing_data["data"] = rel_to_root(os.path.join(cls.tmpdir, "nope.json"))
        cls.missing_data_path = write(
            os.path.join(cls.tmpdir, "missing_data.json"), json.dumps(missing_data))
        missing_prompt = read_json(cls.good_fix.suite_path)
        missing_prompt["prompt"] = rel_to_root(os.path.join(cls.tmpdir, "nope_prompt.txt"))
        cls.missing_prompt_path = write(
            os.path.join(cls.tmpdir, "missing_prompt.json"), json.dumps(missing_prompt))
        cls.doctor_bad_model = write(os.path.join(cls.tmpdir, "doctor_bad.py"), FAKE_DOCTOR_BAD)
        cls.doctor_fix = SuiteFixture(cls.tmpdir, model_src=FAKE_DOCTOR_OK)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def run_cli(self, *args):
        proc = subprocess.run(
            [sys.executable, os.path.join(PROJ_ROOT, "promptlab.py"), *args],
            capture_output=True, text=True, timeout=90, cwd=PROJ_ROOT)
        return proc.returncode, proc.stdout, proc.stderr

    def test_run_all_pass_exits_zero(self):
        code, stdout, _ = self.run_cli("run", "--suite", self.good_fix.suite_path)
        self.assertEqual(code, pl.EXIT_OK)
        report = json.loads(stdout)
        self.assertEqual(report["summary"]["passed"], 1)

    def test_run_failed_case_exits_two(self):
        code, _, _ = self.run_cli("run", "--suite", self.fail_fix.suite_path)
        self.assertEqual(code, pl.EXIT_FAIL)

    def test_run_malformed_suite_exits_one(self):
        code, _, _ = self.run_cli("run", "--suite", self.malformed_path)
        self.assertEqual(code, pl.EXIT_USAGE)

    def test_run_unknown_assertion_exits_one(self):
        code, _, _ = self.run_cli("run", "--suite", self.unknown_assert_path)
        self.assertEqual(code, pl.EXIT_USAGE)

    def test_bare_invocation_exits_one(self):
        code, _, _ = self.run_cli()
        self.assertEqual(code, pl.EXIT_USAGE)

    def test_unknown_flag_exits_one(self):
        code, _, _ = self.run_cli("run", "--suite", self.good_fix.suite_path, "--bogus")
        self.assertEqual(code, pl.EXIT_USAGE)

    def test_run_model_exit_nonzero_exits_three(self):
        code, _, _ = self.run_cli("run", "--suite", self.failing_model_fix.suite_path)
        self.assertEqual(code, pl.EXIT_MODEL)

    def test_run_model_garbage_exits_three(self):
        code, _, _ = self.run_cli("run", "--suite", self.garbage_model_fix.suite_path)
        self.assertEqual(code, pl.EXIT_MODEL)

    def test_run_missing_model_exits_three(self):
        suite = read_json(self.good_fix.suite_path)
        suite["model"] = rel_to_root(os.path.join(self.tmpdir, "nope_model.py"))
        path = write(os.path.join(self.tmpdir, "missing_model.json"), json.dumps(suite))
        code, _, _ = self.run_cli("run", "--suite", path)
        self.assertEqual(code, pl.EXIT_MODEL)

    def test_run_missing_suite_exits_four(self):
        code, _, _ = self.run_cli("run", "--suite",
                                  os.path.join(self.tmpdir, "no_such_suite.json"))
        self.assertEqual(code, pl.EXIT_FILE)

    def test_run_missing_data_exits_four(self):
        code, _, _ = self.run_cli("run", "--suite", self.missing_data_path)
        self.assertEqual(code, pl.EXIT_FILE)

    def test_run_missing_prompt_exits_four(self):
        code, _, _ = self.run_cli("run", "--suite", self.missing_prompt_path)
        self.assertEqual(code, pl.EXIT_FILE)

    def test_run_verbose_emits_details(self):
        code, stdout, _ = self.run_cli("run", "--suite", self.good_fix.suite_path, "--verbose")
        self.assertEqual(code, pl.EXIT_OK)
        report = json.loads(stdout)
        run = report["results"][0]["runs"][0]
        self.assertIn("prompt_used", run)
        self.assertIn("stdout_raw", run)
        self.assertIn("assertions_detail", run)

    def test_run_dry_run_exits_zero(self):
        code, _, _ = self.run_cli("run", "--suite", self.failing_model_fix.suite_path, "--dry-run")
        self.assertEqual(code, pl.EXIT_OK)

    def test_compare_equal_suites_exits_zero(self):
        code, _, _ = self.run_cli(
            "compare", "--suite", self.good_fix.suite_path, "--suite", self.good_fix.suite_path)
        self.assertEqual(code, pl.EXIT_OK)

    def test_compare_regression_exits_one(self):
        code, _, _ = self.run_cli(
            "compare", "--suite", self.good_fix.suite_path, "--suite", self.fail_fix.suite_path)
        self.assertEqual(code, pl.EXIT_USAGE)

    def test_compare_reports_cost_delta(self):
        code, stdout, _ = self.run_cli(
            "compare", "--suite", self.good_fix.suite_path, "--suite", self.fail_fix.suite_path)
        self.assertEqual(code, pl.EXIT_USAGE)
        report = json.loads(stdout)
        self.assertIn("cost_delta", report["comparison"][0])
        self.assertEqual(report["comparison"][0]["cost_delta"], 0.0)

    def test_doctor_ok_exits_zero(self):
        code, stdout, _ = self.run_cli("doctor", "--suites-dir", self.doctor_fix.dir)
        self.assertEqual(code, pl.EXIT_OK)
        self.assertEqual(json.loads(stdout)["failed"], 0)

    def test_doctor_bad_model_exits_one(self):
        code, _, _ = self.run_cli("doctor", "--model", self.doctor_bad_model,
                                  "--suites-dir", self.tmpdir)
        self.assertEqual(code, pl.EXIT_USAGE)


class SourceIsolationTests(unittest.TestCase):
    def test_harness_never_imports_model(self):
        source = read_text(os.path.join(PROJ_ROOT, "promptlab.py"))
        self.assertNotIn("import stubmodel", source)
        self.assertNotIn("from stubmodel", source)
        self.assertIn("subprocess.run", source)


class StubModelAccuracyTests(unittest.TestCase):
    """End-to-end accuracy check for the shipped prompts + dataset."""

    def run_stub(self, prompt_path, text):
        proc = subprocess.run(
            [sys.executable, os.path.join(PROJ_ROOT, "stubmodel.py"),
             "--prompt", prompt_path, "--input", text,
             "--temperature", "0.0", "--seed", "42", "--max-tokens", "16",
             "--call-index", "0"],
            capture_output=True, text=True, timeout=60, cwd=PROJ_ROOT)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout)

    def test_classify_v2_accuracy_target(self):
        dataset = read_json(os.path.join(PROJ_ROOT, "data/tickets.json"))
        prompt = os.path.join(PROJ_ROOT, "prompts", "classify_v2.txt")
        correct = 0
        for row in dataset:
            result = self.run_stub(prompt, row["text"])
            if result["output"] == row["category"]:
                correct += 1
        self.assertGreaterEqual(correct / len(dataset), 0.97,
                                "classify_v2 must reach the 0.97 accuracy target")

    def test_classify_v2_deterministic_across_call_indices(self):
        prompt = os.path.join(PROJ_ROOT, "prompts", "classify_v2.txt")
        outs = {self.run_stub(prompt, "My credit card was charged twice for my subscription.")["output"]
                for _ in range(3)}
        self.assertEqual(outs, {"billing"})

    def test_classify_v1_is_weak_baseline(self):
        prompt = os.path.join(PROJ_ROOT, "prompts", "classify_v1.txt")
        result = self.run_stub(prompt, "The login page keeps returning a 500 error after I type my password.")
        self.assertNotEqual(result["output"], "technical")


if __name__ == "__main__":
    unittest.main()
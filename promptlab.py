#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
promptlab - CLI harness for testing, comparing, and validating LLM prompt
pipelines. Uses only the Python standard library.

Commands
--------
  promptlab run      --suite <path> [--model <path>] [--verbose] [--dry-run]
  promptlab compare  --suite <a> --suite <b> [--model <path>]
                     [--metric pass_rate|mean_tokens|total_cost] [--threshold X]
  promptlab doctor   [--model <path>] [--suites-dir <path>]

Exit codes
----------
  0  all tests passed
  1  bad usage / malformed suite or configuration
  2  one or more test cases failed
  3  model error (subprocess failure, timeout, or unusable stdout)
  4  unreadable file or unexpected harness error

The harness NEVER imports the model. Every model call is a subprocess.run
invocation of the model binary (e.g. stubmodel.py) with a JSON envelope on
stdout, which the harness parses and scores with the assertion engine.
"""

import argparse
import json
import os
import re
import subprocess
import sys

EXIT_OK = 0
EXIT_USAGE = 1
EXIT_FAIL = 2
EXIT_MODEL = 3
EXIT_FILE = 4

ASSERTION_TYPES = (
    "contains",
    "not_contains",
    "equals",
    "matches",
    "json_valid",
    "json_field_equals",
    "max_tokens",
    "finish_is",
)

SUPPORTED_METRICS = ("pass_rate", "mean_tokens", "total_cost")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_MODEL = os.path.join(BASE_DIR, "stubmodel.py")
DEFAULT_TIMEOUT = 30
DEFAULT_THRESHOLD = 1.0

PLACEHOLDER = "{input}"
FENCE_RE = re.compile(r"```(?:json)?\s*\n(.*?)\n```", re.DOTALL)


class UsageError(Exception):
    """Bad arguments, malformed suite, or invalid configuration (exit 1)."""


class FileError(Exception):
    """A required file is missing or unreadable (exit 4)."""


class ModelError(Exception):
    """The model subprocess failed or produced unusable output (exit 3)."""


def resolve_path(base_dir, value):
    if os.path.isabs(value):
        return os.path.normpath(value)
    return os.path.normpath(os.path.join(base_dir, value))


def read_json(path):
    if not os.path.isfile(path):
        raise FileError("file not found: %s" % path)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except OSError as exc:
        raise FileError("cannot read %s: %s" % (path, exc)) from exc
    except json.JSONDecodeError as exc:
        raise UsageError("invalid JSON in %s: %s" % (path, exc)) from exc


def read_text(path):
    if not os.path.isfile(path):
        raise FileError("file not found: %s" % path)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read()
    except OSError as exc:
        raise FileError("cannot read %s: %s" % (path, exc)) from exc


def extract_fenced(text):
    match = FENCE_RE.search(text)
    if match is None:
        return text.strip(), False
    return match.group(1).strip(), True


def get_field(data, field):
    if field in (None, ""):
        return data.get("output")
    path = field
    if path.startswith("$"):
        path = path[2:] if path.startswith("$.") else path[1:]
    node = data
    for part in path.split("."):
        if not part:
            continue
        if isinstance(node, dict) and part in node:
            node = node[part]
        elif isinstance(node, list) and part.isdigit() and int(part) < len(node):
            node = node[int(part)]
        else:
            raise KeyError("field '%s' not present in model output" % field)
    return node


def normalize_text(value):
    text = str(value)
    return text.replace("\r\n", "\n").replace("\r", "\n").strip()


def evaluate_assertion(assertion, model_data):
    assert_type = assertion.get("type")
    field = assertion.get("field")
    value = assertion.get("value")

    try:
        if assert_type == "equals":
            actual = normalize_text(get_field(model_data, field or "output"))
            return actual == normalize_text(value), "expected=%r actual=%r" % (value, actual)

        if assert_type == "contains":
            actual = str(get_field(model_data, field or "output"))
            return value in actual, "probe=%r in=%r" % (value, actual)

        if assert_type == "not_contains":
            actual = str(get_field(model_data, field or "output"))
            return value not in actual, "probe=%r not_in=%r" % (value, actual)

        if assert_type == "matches":
            actual = normalize_text(get_field(model_data, field or "output"))
            return re.fullmatch(value, actual) is not None, "regex=%r actual=%r" % (value, actual)

        if assert_type == "json_valid":
            raw = str(get_field(model_data, field or "output"))
            try:
                json.loads(raw)
                return True, "valid JSON"
            except (json.JSONDecodeError, TypeError):
                return False, "not valid JSON"

        if assert_type == "json_field_equals":
            raw = str(get_field(model_data, field or "output"))
            parsed = json.loads(raw)
            leaf = get_field(parsed, assertion["path"])
            return str(leaf) == normalize_text(value), "jsonfield=%r expected=%r" % (leaf, value)

        if assert_type == "max_tokens":
            used = int(get_field(model_data, field or "usage.completion_tokens"))
            return used <= int(value), "used=%d limit=%s" % (used, value)

        if assert_type == "finish_is":
            actual = normalize_text(get_field(model_data, field or "finish_reason"))
            return actual == normalize_text(value), "finish=%r expected=%r" % (actual, value)

    except KeyError as exc:
        return False, str(exc)
    except (json.JSONDecodeError, TypeError, ValueError, AttributeError) as exc:
        return False, "assertion error: %s" % exc

    raise UsageError("unknown assertion type: %r" % assert_type)


def validate_assertion(assertion, index):
    if not isinstance(assertion, dict) or "type" not in assertion:
        raise UsageError("assertion #%d must be an object with a 'type'" % (index + 1))
    assert_type = assertion["type"]
    if assert_type not in ASSERTION_TYPES:
        raise UsageError(
            "unsupported assertion type %r (supported: %s)"
            % (assert_type, ", ".join(ASSERTION_TYPES)))
    if assert_type == "json_field_equals" and not assertion.get("path"):
        raise UsageError("json_field_equals assertion #%d requires a 'path'" % (index + 1))
    if assert_type != "json_valid" and "value" not in assertion:
        raise UsageError("assertion #%d (%s) requires a 'value'" % (index + 1, assert_type))


def parse_suite(suite_path):
    suite = read_json(suite_path)
    if not isinstance(suite, dict):
        raise UsageError("suite root must be a JSON object: %s" % suite_path)
    if not isinstance(suite.get("name"), str) or not suite["name"].strip():
        raise UsageError("suite is missing a non-empty 'name'")
    for key in ("prompt", "data", "tests"):
        if key not in suite:
            raise UsageError("suite is missing required key '%s'" % key)

    base_dir = os.getcwd()
    parsed = dict(suite)
    parsed["prompt_abs"] = resolve_path(base_dir, suite["prompt"])
    parsed["data_abs"] = resolve_path(base_dir, suite["data"])
    parsed["model_abs"] = resolve_path(base_dir, suite.get("model", DEFAULT_MODEL))
    parsed["runs_per_input"] = int(suite.get("runs_per_input", 1))
    parsed["temperature"] = float(suite.get("temperature", 0.0))
    parsed["seed"] = int(suite.get("seed", 42))
    parsed["max_tokens"] = int(suite.get("max_tokens", 32))
    parsed["timeout"] = float(suite.get("timeout", DEFAULT_TIMEOUT))
    parsed["pass_rate_threshold"] = float(suite.get("pass_rate_threshold", DEFAULT_THRESHOLD))
    parsed["pricing"] = suite.get("pricing", {})

    if parsed["runs_per_input"] < 1:
        raise UsageError("runs_per_input must be >= 1")
    if parsed["timeout"] <= 0:
        raise UsageError("timeout must be > 0")
    if not 0.0 <= parsed["pass_rate_threshold"] <= 1.0:
        raise UsageError("pass_rate_threshold must be in [0, 1]")

    dataset = read_json(parsed["data_abs"])
    if not isinstance(dataset, list):
        raise UsageError("data file must contain a JSON array: %s" % parsed["data_abs"])
    parsed["_dataset"] = dataset

    tests = suite.get("tests")
    if not isinstance(tests, list) or not tests:
        raise UsageError("suite 'tests' must be a non-empty list")
    for test_index, test in enumerate(tests):
        if not isinstance(test, dict):
            raise UsageError("test #%d must be a JSON object" % (test_index + 1))
        if not isinstance(test.get("id"), str):
            raise UsageError("test #%d requires a string 'id'" % (test_index + 1))
        data_index = test.get("data_index")
        if not isinstance(data_index, int) or isinstance(data_index, bool):
            raise UsageError("test '%s' requires an integer 'data_index'" % test.get("id"))
        if not 0 <= data_index < len(dataset):
            raise UsageError("test '%s' data_index %s out of range (dataset has %d rows)"
                             % (test.get("id"), data_index, len(dataset)))
        assertions = test.get("assertions")
        if not isinstance(assertions, list) or not assertions:
            raise UsageError("test '%s' requires a non-empty 'assertions' list" % test.get("id"))
        for assertion_index, assertion in enumerate(assertions):
            validate_assertion(assertion, assertion_index)

    return parsed


def render_prompt(template, input_text):
    count = template.count(PLACEHOLDER)
    if count == 0:
        print("warning: prompt template contains no '%s' placeholder" % PLACEHOLDER, file=sys.stderr)
        return template
    if count > 1:
        raise UsageError("prompt template must contain exactly one '%s' placeholder" % PLACEHOLDER)
    return template.replace(PLACEHOLDER, input_text)


def invoke_model(model_path, prompt_path, input_text, temperature, seed,
                 max_tokens, call_index, timeout):
    command = [
        sys.executable,
        model_path,
        "--prompt", prompt_path,
        "--input", input_text,
        "--temperature", str(temperature),
        "--seed", str(seed),
        "--max-tokens", str(max_tokens),
        "--call-index", str(call_index),
    ]
    try:
        proc = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise ModelError("model subprocess timed out after %ss: %s" % (timeout, model_path)) from exc
    except OSError as exc:
        raise ModelError("failed to launch model subprocess %s: %s" % (model_path, exc)) from exc

    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise ModelError("model process exited with code %d: %s" % (proc.returncode, detail))

    raw_stdout = proc.stdout or ""
    evaluation_text, fenced = extract_fenced(raw_stdout)
    if not evaluation_text:
        raise ModelError("model produced empty stdout")
    try:
        parsed = json.loads(evaluation_text)
    except json.JSONDecodeError as exc:
        raise ModelError("model stdout is not valid JSON: %s\n%s" % (exc, evaluation_text[:500])) from exc
    if not isinstance(parsed, dict):
        raise ModelError("model stdout must decode to a JSON object")

    return parsed, raw_stdout, proc.stderr or "", fenced


def get_usage_tokens(model_data):
    usage = model_data.get("usage")
    if not isinstance(usage, dict):
        return None
    return usage


def compute_cost(model_data, pricing):
    if not isinstance(pricing, dict) or "usage" not in model_data:
        return None
    usage = model_data.get("usage")
    if not isinstance(usage, dict):
        return None
    if "prompt_tokens" not in usage or "completion_tokens" not in usage:
        return None
    per_prompt = pricing.get("prompt_per_token")
    per_completion = pricing.get("completion_per_token")
    if per_prompt is None or per_completion is None:
        return None
    return (usage["prompt_tokens"] * per_prompt) + (usage["completion_tokens"] * per_completion)


def classify_test(pass_rate, threshold):
    if pass_rate >= 1.0 - 1e-9:
        return "pass"
    if pass_rate <= 1e-9:
        return "fail"
    if pass_rate >= threshold - 1e-9:
        return "pass"
    return "fail"


def is_flaky(pass_rate):
    return 1e-9 < pass_rate < 1.0 - 1e-9


def run_suite(suite_path, model_override=None, verbose=False, dry_run=False):
    suite = parse_suite(suite_path)
    model_path = os.path.abspath(model_override) if model_override else suite["model_abs"]
    if not os.path.isfile(model_path):
        raise ModelError("model binary not found: %s" % model_path)

    dataset = suite["_dataset"]
    threshold = suite["pass_rate_threshold"]
    results = []

    if not dry_run:
        template = read_text(suite["prompt_abs"])
    else:
        template = ""

    mean_token_total = 0
    token_samples = 0
    cost_total = None

    for test in suite["tests"]:
        row = dataset[test["data_index"]]
        input_text = row.get("text")
        if not isinstance(input_text, str):
            raise UsageError("data row %d has no string 'text'" % test["data_index"])

        runs = []
        for call_index in range(suite["runs_per_input"]):
            if dry_run:
                runs.append({"call_index": call_index, "pass": None, "dry_run": True})
                continue

            rendered = render_prompt(template, input_text)
            model_data, stdout_raw, stderr_raw, fenced = invoke_model(
                model_path=model_path,
                prompt_path=suite["prompt_abs"],
                input_text=input_text,
                temperature=suite["temperature"],
                seed=suite["seed"],
                max_tokens=suite["max_tokens"],
                call_index=call_index,
                timeout=suite["timeout"],
            )

            assertion_results = []
            passed = True
            for assertion in test["assertions"]:
                ok, detail = evaluate_assertion(assertion, model_data)
                assertion_results.append({
                    "type": assertion.get("type"),
                    "field": assertion.get("field"),
                    "value": assertion.get("value"),
                    "pass": ok,
                    "detail": detail,
                })
                if not ok:
                    passed = False

            cost = compute_cost(model_data, suite["pricing"])
            if cost is not None:
                cost_total = (cost_total or 0.0) + cost

            usage = get_usage_tokens(model_data)
            run_entry = {
                "call_index": call_index,
                "pass": passed,
                "output": model_data.get("output"),
                "cost": cost,
            }
            if usage is not None and isinstance(usage.get("total_tokens"), int):
                mean_token_total += usage["total_tokens"]
                token_samples += 1
            if verbose:
                run_entry.update({
                    "prompt_used": rendered,
                    "input_text": input_text,
                    "stdout_raw": stdout_raw,
                    "stderr_raw": stderr_raw,
                    "fenced_extracted": fenced,
                    "assertions_detail": assertion_results,
                })
            runs.append(run_entry)

        if dry_run:
            pass_rate = None
            status = "skipped"
        else:
            passed_runs = sum(1 for run in runs if run["pass"])
            pass_rate = passed_runs / len(runs)
            status = classify_test(pass_rate, threshold)

        results.append({
            "test_id": test["id"],
            "data_index": test["data_index"],
            "runs": runs,
            "pass_rate": pass_rate,
            "flaky": is_flaky(pass_rate) if not dry_run else False,
            "status": status,
        })

    if dry_run:
        total_cost = None
        mean_tokens = None
    else:
        total_cost = cost_total
        mean_tokens = (mean_token_total / token_samples) if token_samples else None

    passed_tests = sum(1 for result in results if result["status"] == "pass")
    failed_tests = sum(1 for result in results if result["status"] == "fail")
    flaky_tests = sum(1 for result in results if result["flaky"])
    suite_pass_rate = (passed_tests / len(results)) if results and not dry_run else None
    exit_code = EXIT_OK if failed_tests == 0 else EXIT_FAIL

    report = {
        "suite": suite["name"],
        "model": model_path,
        "prompt": suite["prompt_abs"],
        "data": suite["data_abs"],
        "runs_per_input": suite["runs_per_input"],
        "temperature": suite["temperature"],
        "seed": suite["seed"],
        "max_tokens": suite["max_tokens"],
        "pass_rate_threshold": threshold,
        "results": results,
        "summary": {
            "total_tests": len(results),
            "passed": passed_tests,
            "failed": failed_tests,
            "flaky": flaky_tests,
            "pass_rate": suite_pass_rate,
            "total_cost": total_cost,
            "mean_tokens": mean_tokens,
            "exit_code": exit_code,
        },
    }
    return report


def suite_metric(report, metric):
    summary = report["summary"]
    if metric == "pass_rate":
        return summary["pass_rate"]
    if metric == "total_cost":
        return summary["total_cost"]
    if metric == "mean_tokens":
        return summary["mean_tokens"]
    raise UsageError("unsupported metric: %s" % metric)


def compare_suites(suite_paths, model_override=None, metric="pass_rate", threshold=0.0):
    if len(suite_paths) < 2:
        raise UsageError("compare requires at least two --suite arguments")
    reports = [run_suite(path, model_override=model_override) for path in suite_paths]

    higher_is_better = metric == "pass_rate"
    baseline = reports[0]
    baseline_value = suite_metric(baseline, metric)
    comparison = []
    regressions = []

    for index, report in enumerate(reports):
        value = suite_metric(report, metric)
        baseline_total = baseline["summary"]["total_cost"]
        report_total = report["summary"]["total_cost"]
        cost_delta = None
        if baseline_total is not None and report_total is not None:
            cost_delta = round(report_total - baseline_total, 6)

        if value is None:
            regressed = False
        elif higher_is_better:
            regressed = baseline_value is not None and (baseline_value - value) > threshold
        else:
            regressed = baseline_value is not None and (value - baseline_value) > threshold

        if regressed:
            regressions.append(report["suite"])
        comparison.append({
            "position": index,
            "suite": report["suite"],
            "pass_rate": report["summary"]["pass_rate"],
            "failed": report["summary"]["failed"],
            "passed": report["summary"]["passed"],
            "flaky": report["summary"]["flaky"],
            "total_cost": report_total,
            "cost_delta": cost_delta,
            "mean_tokens": report["summary"]["mean_tokens"],
            "metric_value": value,
            "regression": regressed,
            "exit_code": report["summary"]["exit_code"],
        })

    if regressions:
        exit_code = EXIT_USAGE
    elif any(item["exit_code"] == EXIT_FAIL for item in comparison):
        exit_code = EXIT_FAIL
    else:
        exit_code = EXIT_OK

    return {
        "metric": metric,
        "threshold": threshold,
        "baseline": baseline["suite"],
        "baseline_metric": baseline_value,
        "comparison": comparison,
        "regressions": regressions,
        "exit_code": exit_code,
    }


def check_model(model_path):
    if not os.path.isfile(model_path):
        return False, "model binary not found: %s" % model_path
    try:
        proc = subprocess.run(
            [sys.executable, model_path, "--help"],
            capture_output=True, text=True, timeout=15)
    except OSError as exc:
        return False, "cannot launch model: %s" % exc
    output = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode != 0:
        return False, "model --help exited with code %d" % proc.returncode
    for flag in ("--prompt", "--input"):
        if flag not in output:
            return False, "model --help does not advertise %s" % flag
    return True, "model binary runs and supports required flags"


def doctor(model_override=None, suites_dir="suites"):
    checks = []

    version = sys.version_info
    checks.append(("python >= 3.9",
                   version.major >= 3 and version.minor >= 9,
                   "detected %d.%d" % (version.major, version.minor)))

    model_path = os.path.abspath(model_override) if model_override else DEFAULT_MODEL
    ok, detail = check_model(model_path)
    checks.append(("model binary", ok, detail))

    if os.path.isdir(suites_dir):
        suite_files = []
        for name in sorted(os.listdir(suites_dir)):
            if name.endswith(".json"):
                candidate = os.path.join(suites_dir, name)
                try:
                    data = read_json(candidate)
                except (UsageError, FileError):
                    continue
                if isinstance(data, dict) and "tests" in data:
                    suite_files.append(candidate)
    else:
        suite_files = []
        checks.append(("suites directory", False, "suites directory not found: %s" % suites_dir))

    for suite_file in suite_files:
        try:
            suite = parse_suite(suite_file)
        except UsageError as exc:
            checks.append(("suite %s" % suite_file, False, str(exc)))
            continue
        except FileError as exc:
            checks.append(("suite %s" % suite_file, False, str(exc)))
            continue
        try:
            template = read_text(suite["prompt_abs"])
            count = template.count(PLACEHOLDER)
            checks.append(("prompt placeholder: %s" % suite_file,
                           count == 1,
                           "prompt has %d '%s' placeholder(s)" % (count, PLACEHOLDER)))
        except FileError as exc:
            checks.append(("suite %s" % suite_file, False, str(exc)))
            continue
        datasets_ok = True
        for test in suite["tests"]:
            data_index = test["data_index"]
            if not 0 <= data_index < len(suite["_dataset"]):
                datasets_ok = False
        checks.append(("data_index bounds: %s" % suite_file,
                       datasets_ok,
                       "all data_index values in range"))

    results_dir = os.path.join(os.getcwd(), "results")
    try:
        os.makedirs(results_dir, exist_ok=True)
        probe = os.path.join(results_dir, ".doctor_probe")
        with open(probe, "w", encoding="utf-8") as handle:
            handle.write("ok")
        os.remove(probe)
        checks.append(("output directory", True, "writable: %s" % results_dir))
    except OSError as exc:
        checks.append(("output directory", False, "not writable: %s" % results_dir))

    report = {
        "checks": [
            {"name": name, "ok": ok, "detail": detail}
            for name, ok, detail in checks
        ],
        "passed": sum(1 for _, ok, _ in checks if ok),
        "failed": sum(1 for _, ok, _ in checks if not ok),
        "exit_code": EXIT_OK if all(ok for _, ok, _ in checks) else EXIT_USAGE,
    }
    return report


def build_argument_parser():
    parser = PromptlabArgumentParser(
        prog="promptlab",
        description="CLI harness for testing and comparing LLM prompt pipelines.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.parser_class = PromptlabArgumentParser

    run_parser = subparsers.add_parser("run", help="run a single test suite")
    run_parser.add_argument("--suite", required=True, help="path to suite JSON")
    run_parser.add_argument("--model", help="override model binary path")
    run_parser.add_argument("--verbose", action="store_true",
                            help="include prompt/input/stdout detail per run")
    run_parser.add_argument("--dry-run",
                            help="validate suite without invoking the model",
                            action="store_true")

    compare_parser = subparsers.add_parser("compare", help="compare two or more suites")
    compare_parser.add_argument("--suite", action="append", required=True,
                                help="path to suite JSON (repeatable, min 2)")
    compare_parser.add_argument("--model", help="override model binary path")
    compare_parser.add_argument("--metric", choices=SUPPORTED_METRICS, default="pass_rate",
                                help="primary comparison metric (default: pass_rate)")
    compare_parser.add_argument("--threshold", type=float, default=0.0,
                                help="regression threshold relative to baseline (default: 0.0)")

    doctor_parser = subparsers.add_parser("doctor", help="validate the local environment")
    doctor_parser.add_argument("--model", help="model binary to probe")
    doctor_parser.add_argument("--suites-dir", default="suites",
                               help="directory to scan for suite JSON files (default: suites)")

    return parser


class PromptlabArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        self.print_usage(sys.stderr)
        self.exit(EXIT_USAGE, "%s: error: %s\n" % (self.prog, message))


def emit(report):
    print(json.dumps(report, indent=2, default=str))


def main(argv=None):
    parser = build_argument_parser()
    args = parser.parse_args(argv)

    try:
        if args.command == "run":
            report = run_suite(args.suite, model_override=args.model,
                               verbose=args.verbose, dry_run=args.dry_run)
            emit(report)
            return report["summary"]["exit_code"]

        if args.command == "compare":
            report = compare_suites(
                args.suite,
                model_override=args.model,
                metric=args.metric,
                threshold=args.threshold,
            )
            emit(report)
            return report["exit_code"]

        if args.command == "doctor":
            report = doctor(model_override=args.model, suites_dir=args.suites_dir)
            emit(report)
            return report["exit_code"]

    except UsageError as exc:
        print("promptlab: error: %s" % exc, file=sys.stderr)
        return EXIT_USAGE
    except FileError as exc:
        print("promptlab: file error: %s" % exc, file=sys.stderr)
        return EXIT_FILE
    except ModelError as exc:
        print("promptlab: model error: %s" % exc, file=sys.stderr)
        return EXIT_MODEL
    except Exception as exc:
        print("promptlab: internal error: %s" % exc, file=sys.stderr)
        return EXIT_FILE

    return EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
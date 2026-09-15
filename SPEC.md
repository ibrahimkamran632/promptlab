# PromptLab Specification

**Version:** 0.1.0  
**Status:** Draft  
**Last Updated:** 2026-09-15  

---

## Table of Contents

1. [Overview](#overview)
2. [Project Structure](#project-structure)
3. [CLI Commands](#cli-commands)
4. [Subprocess Execution Model](#subprocess-execution-model)
5. [Suite JSON Schema](#suite-json-schema)
6. [Assertion Semantics](#assertion-semantics)
7. [Non-Determinism & Flakiness Handling](#non-determinism--flakiness-handling)
8. [Fenced JSON Decision](#fenced-json-decision)
9. [Cost Accounting](#cost-accounting)
10. [Exit Codes](#exit-codes)
11. [Output Format](#output-format)

---

## Overview

PromptLab is a CLI harness for testing, comparing, and validating LLM prompt pipelines. It provides deterministic execution, structured assertions, and reproducible benchmarking by treating LLM calls as subprocesses with controlled inputs.

**Key Design Principles:**

- **Subprocess isolation:** LLM calls execute in separate processes, not as imported modules
- **Reproducibility:** Seeds and deterministic models enable repeatable test runs
- **No implementation coupling:** The harness never imports the model; it invokes it via CLI
- **Exit-code driven:** CI/CD pipelines consume structured exit codes for automation

---

## Project Structure

```
promptlab/
├── SPEC.md                 # This specification
├── promptlab.py            # CLI harness (run / compare / doctor)
├── stubmodel.py            # LLM simulator (subprocess target)
├── prompts/
│   ├── classify_v1.txt     # Weak baseline prompt ({input} placeholder)
│   └── classify_v2.txt     # Optimized prompt (JSON contract + few-shots)
├── data/
│   └── tickets.json        # Test data with ground truth
├── suites/
│   ├── smoke.json          # Baseline suite (classify_v1, weak by design)
│   └── classify_v2.json    # Regression suite (classify_v2, 0.97+)
├── instructor/             # Human-facing lesson materials
├── tests/
│   ├── __init__.py
│   └── test_promptlab.py   # unittest suite for the harness
├── results/                # doctor output directory (created on demand)
└── *.md                    # USAGE.md, PROMPTS.md, CLAUDE.md, IMPROVEMENT.md, JOURNAL.md
```

**`prompts/`** contains plain-text prompt templates. The token `{input}` is replaced at runtime with the test input value.

**`data/`** contains JSON arrays of test cases. Each object must have an `id` field and a `text` field. Additional fields (e.g., `category`) supply ground truth for assertions.

**`suites/`** contains JSON suite files that bind a prompt, dataset, model configuration, and assertion list.

> **Path resolution:** `prompt`, `data`, and `model` entries in a suite are resolved
> relative to the directory you run `promptlab` from (normally the repository
> root). Absolute paths are used as-is.

**`instructor/`** holds pedagogical materials (READMEs, walkthroughs, grading rubrics). Not consumed by the harness.

**`tests/`** holds Python `unittest` files that validate the harness itself.
Run them with `python -m unittest discover -s tests -v`.

---

## CLI Commands

### `promptlab run`

Execute a single test suite and report results.

```bash
python promptlab.py run --suite suites/smoke.json [--verbose] [--dry-run]
```

**Flags:**

| Flag | Description |
|------|-------------|
| `--suite <path>` | (Required) Path to suite JSON file |
| `--verbose` | Print full prompt/input/output for each test |
| `--dry-run` | Validate suite JSON and print what would execute without making subprocess calls |
| `--model <path>` | Override model binary path (default: `stubmodel.py`) |

**Behavior:**

1. Parse and validate the suite JSON
2. For each test case in `tests`, for each run (1..`runs_per_input`):
   a. Read the prompt template, replace `{input}` with `data[data_index].text`
   b. Launch the model as a subprocess with the configured arguments
   c. Capture stdout (parsed as JSON) and stderr
   d. Evaluate all assertions against the captured output
3. Aggregate results, compute pass rates, determine flakiness
4. Print results to stdout; exit with appropriate code

### `promptlab compare`

Compare two or more suites side-by-side.

```bash
python promptlab.py compare \
  --suite suites/smoke_v1.json \
  --suite suites/smoke_v2.json \
  [--metric pass_rate] [--threshold 0.9]
```

**Flags:**

| Flag | Description |
|------|-------------|
| `--suite <path>` | (Repeatable, minimum 2) Suites to compare |
| `--metric <name>` | Primary comparison metric: `pass_rate`, `mean_tokens`, `total_cost` (default: `pass_rate`) |
| `--threshold <float>` | Reject regression if metric drops below this value (default: `0.0`, meaning no threshold) |

**Behavior:**

1. Run each suite independently via the `run` logic
2. Compute the selected metric for each suite (baseline = first suite)
3. Print a comparison table to stdout
4. Exit with code 1 if any suite regresses below `--threshold`; code 2 if any suite has failed tests but no regression; otherwise exit 0

### `promptlab doctor`

Validate the local environment and report readiness.

```bash
python promptlab.py doctor
```

**Checks performed:**

1. Python >= 3.9 is available
2. `--prompt`, `--input`, and all required flags are supported by the model binary (test invocation with `--help` or a probe call)
3. All suite JSON files parse without error
4. All prompt templates contain exactly one `{input}` placeholder
5. All `data_index` values in suites are within bounds of the referenced data file
6. Write access to an output directory (default: `./results/`)

**Exit codes:** 0 if all checks pass; 1 if any check fails (details printed to stderr).

---

## Subprocess Execution Model

The harness **never** imports the model as a Python module. All model invocations are subprocess calls using `subprocess.Popen` (or `subprocess.run`) from the Python standard library.

**Invocation pattern:**

```python
proc = subprocess.Popen(
    [
        sys.executable,          # Python interpreter
        model_path,              # e.g., "stubmodel.py"
        "--prompt", prompt_path,
        "--input", input_text,
        "--temperature", str(temperature),
        "--seed", str(seed),
        "--max-tokens", str(max_tokens),
        "--call-index", str(call_index),
    ],
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    text=True,
)
stdout, stderr = proc.communicate(timeout=timeout)
```

**Contract:**

- The model binary MUST write a single JSON object to stdout
- The harness reads stdout and parses it as JSON
- stderr is captured for diagnostics but not parsed for assertions
- If the process exits with non-zero code, the test is recorded as a `fail` with error details from stderr
- Timeout defaults to 30 seconds; configurable in the suite JSON

**Why subprocess?**

- Prevents model code from corrupting harness memory
- Enables testing of non-Python model binaries
- Matches production deployment patterns (containers, CLI wrappers)
- Allows true isolation and crash recovery

---

## Suite JSON Schema

```jsonc
{
  "name": "string",                    // Human-readable suite name
  "description": "string",             // Optional description
  "prompt": "string",                  // Path to prompt template (relative to suite file)
  "data": "string",                    // Path to data JSON file
  "model": "string",                   // Path to model binary (optional, default: stubmodel.py)
  "runs_per_input": 3,                 // Number of repetitions per test (for flakiness detection)
  "temperature": 0.0,                  // Model temperature
  "seed": 42,                          // Random seed
  "max_tokens": 32,                    // Max tokens for model output
  "timeout": 30,                       // Subprocess timeout in seconds
  "tests": [
    {
      "id": "string",                  // Unique test identifier
      "data_index": 0,                 // Index into data array
      "assertions": [                  // Array of assertion objects
        {
          "type": "string",            // Assertion type (see Assertion Semantics)
          "field": "string",           // Field to evaluate (optional, depends on type)
          "value": "mixed"             // Expected value (type depends on assertion type)
        }
      ]
    }
  ]
}
```

**`{input}` substitution:**

Before launching the subprocess, the harness reads the prompt template file and replaces the literal string `{input}` with `data[data_index].text`. If the template contains zero or more than one `{input}` tokens, the harness logs a warning (zero) or error (more than one).

---

## Assertion Semantics

Each assertion object has a `type` field that determines evaluation logic. The following 8 types are supported.

### 1. `equals`

Exact string equality after stripping trailing whitespace and normalizing line endings.

```jsonc
{ "type": "equals", "field": "output", "value": "billing" }
```

- `field` selects which JSON key from the model output to compare (typically `"output"`)
- `value` is the expected string
- Pass: `output.strip() == value`
- Fail: strings do not match

### 2. `contains`

Substring membership check.

```jsonc
{ "type": "contains", "field": "output", "value": "error" }
```

- Pass: `value in output`
- Fail: substring not found

### 3. `not_contains`

Negated substring check.

```jsonc
{ "type": "not_contains", "field": "output", "value": "I don't know" }
```

- Pass: `value not in output`
- Fail: substring was found

### 4. `matches`

Regular expression match against the full output string.

```jsonc
{ "type": "matches", "field": "output", "value": "^(billing|technical|account|other)$" }
```

- Pass: `re.fullmatch(value, output)` is not None
- Fail: regex does not match the full output

### 5. `json_valid`

Validates that the model output is parseable as JSON.

```jsonc
{ "type": "json_valid", "field": "output" }
```

- Pass: `json.loads(output)` succeeds without exception
- Fail: `json.JSONDecodeError` is raised
- Note: `value` field is not required for this assertion type

### 6. `json_field_equals`

Extracts a nested field from JSON output and compares it to the expected value.

```jsonc
{ "type": "json_field_equals", "field": "output", "path": "$.category", "value": "billing" }
```

- `path` is a dot-separated or JSONPath-style path into the parsed JSON (e.g., `"$.result.label"`)
- The harness parses `output` as JSON, traverses `path`, and compares the leaf value to `value`
- Type coercion: both sides are compared as strings (`str(extracted) == str(value)`)
- Fail: JSON parse error, path not found, or value mismatch

### 7. `max_tokens`

Asserts that the model used at most N tokens for completion.

```jsonc
{ "type": "max_tokens", "field": "usage.completion_tokens", "value": 16 }
```

- Extracts `usage.completion_tokens` from the model output JSON
- Pass: `int(extracted) <= int(value)`
- Fail: token count exceeded the limit

### 8. `finish_is`

Asserts the model's finish reason.

```jsonc
{ "type": "finish_is", "field": "finish_reason", "value": "stop" }
```

- Pass: `output["finish_reason"] == value`
- Common values: `"stop"`, `"length"`, `"error"`

---

## Non-Determinism & Flakiness Handling

LLMs are inherently non-deterministic. PromptLab handles this through repeated execution and statistical classification of test outcomes.

### Per-Test Outcome Classification

For a test with `runs_per_input = N`, the harness runs the test N times and classifies the aggregate outcome:

| Term | Definition |
|------|-----------|
| **pass** | All N runs passed every assertion |
| **fail** | At least one assertion failed in every single run |
| **flaky** | Some runs passed and some failed (mixed results across N runs) |

### Pass Rate

The **pass rate** for a test is:

```
pass_rate = (number of runs where all assertions passed) / N
```

### Flakiness Policy

The suite-level outcome depends on the `pass_rate` and a configurable **threshold** (default: `1.0`):

```
if pass_rate == 1.0:
    suite_result = "pass"
elif pass_rate == 0.0:
    suite_result = "fail"
elif pass_rate >= threshold:
    suite_result = "pass"       # flaky, but above threshold
else:
    suite_result = "fail"       # flaky, below threshold
```

**Threshold configuration in suite JSON:**

```jsonc
{
  "pass_rate_threshold": 0.9,   // Accept flaky tests if >= 90% of runs pass
  ...
}
```

**Flaky reporting:**

- Even when a test passes the threshold, it is flagged as `flaky` in the verbose output
- The comparison report includes a flakiness indicator per test
- A dedicated exit code (see Exit Codes) signals that flaky tests were encountered

**Determinism in CI:**

- When `temperature = 0.0` and `seed` is fixed, `stubmodel.py` produces identical output for identical inputs
- For real LLM APIs, set `temperature = 0.0` where possible; seed may not be honored by all providers
- The harness logs the effective seed and temperature for each call to aid debugging

---

## Fenced JSON Decision

When the model outputs text that includes a fenced code block (triple backticks), the harness must decide whether to use the **raw output** or the **extracted content inside the fence** for assertion evaluation.

### Rule

1. If the output contains a fenced code block matching ` ```json\n...\n``` ` (or ` ```\n...\n``` `), extract the content inside the fence
2. Use the extracted content as the evaluation target for all assertions on that test
3. If no fence is found, use the raw output as-is
4. If multiple fences exist, use the **first** fence
5. Log a warning in verbose mode indicating which extraction path was taken

### Rationale

Many LLMs wrap JSON output in markdown fences. The harness normalizes this behavior so prompt authors do not need to account for model-specific formatting quirks.

---

## Cost Accounting

PromptLab tracks token usage and computes estimated cost for each run and suite.

### Token Tracking

The harness reads `usage.prompt_tokens`, `usage.completion_tokens`, and `usage.total_tokens` from the model's JSON output. If the model does not provide these fields, they are recorded as `null` and cost is reported as `N/A`.

### Cost Calculation

A per-token price table is defined in the suite JSON (or defaults to `$0.00 / token` for stubmodel):

```jsonc
{
  "pricing": {
    "prompt_per_token": 0.00003,        // $30 per 1M prompt tokens
    "completion_per_token": 0.00006     // $60 per 1M completion tokens
  }
}
```

**Per-call cost:**

```
cost = (prompt_tokens * prompt_per_token) + (completion_tokens * completion_per_token)
```

**Suite cost:**

```
total_cost = sum(cost for each call in suite)
```

### Reporting

- Each test result line includes per-call cost (if pricing is defined)
- Suite summary includes total cost
- `promptlab compare` can sort by `total_cost` metric
- Cost is always reported in USD

---

## Exit Codes

PromptLab uses distinct exit codes to enable CI/CD pipeline integration:

| Code | Meaning | Description |
|------|---------|-------------|
| **0** | All tests passed | Every assertion in every test passed. Flaky tests at or above the pass-rate threshold are acceptable. |
| **1** | Bad usage or malformed suite | Invalid CLI arguments, malformed suite JSON, unknown assertion types, or missing required schema keys. |
| **2** | One or more test cases failed | At least one test fell below the pass-rate threshold or failed all runs. |
| **3** | Model error | The model binary exited non-zero, timed out, or produced unparseable output. |
| **4** | Unreadable file or harness error | A required file (suite, data, prompt) is missing/unreadable, or an unexpected internal error occurred. |

**Decision tree (run):**

```
config_error (usage / malformed suite)?  → exit 1
file unreadable?                         → exit 4
model_error?                             → exit 3
any test failed?                         → exit 2
all tests pass clean?                    → exit 0
```

**Compare exits:**

```
any suite has regression below threshold?  → exit 1
any suite has failed tests?                → exit 2
all suites clean?                          → exit 0
```

**`promptlab doctor` uses only codes 0 (all checks passed) and 1 (any check failed).**

---

## Output Format

All commands print structured JSON to stdout. Machine-readable by default; `--verbose` adds human-readable sections.

### `run` output (default):

```jsonc
{
  "suite": "classify-smoke",
  "model": "stubmodel.py",
  "runs_per_input": 3,
  "results": [
    {
      "test_id": "classify-billing-001",
      "runs": [
        { "pass": true, "output": "billing", "cost": 0.00012, "call_index": 0 },
        { "pass": true, "output": "billing", "cost": 0.00012, "call_index": 1 },
        { "pass": true, "output": "billing", "cost": 0.00012, "call_index": 2 }
      ],
      "pass_rate": 1.0,
      "status": "pass"
    }
  ],
  "summary": {
    "total_tests": 6,
    "passed": 6,
    "failed": 0,
    "flaky": 0,
    "total_cost": 0.00216,
    "exit_code": 0
  }
}
```

### `--verbose` additions:

For each run, the following fields are appended:

```jsonc
{
  "prompt_used": "...(full prompt after substitution)...",
  "input_text": "...(the input string)...",
  "stdout_raw": "...(raw stdout from subprocess)...",
  "stderr_raw": "...(raw stderr from subprocess)...",
  "assertions_detail": [
    { "type": "equals", "field": "output", "expected": "billing", "actual": "billing", "pass": true }
  ]
}
```

---

## Appendix: Testing Strategy

### Unit Tests (`tests/`)

- Test assertion evaluation functions in isolation (mock model output)
- Test `{input}` substitution logic
- Test JSON parsing and fenced extraction
- Test exit code decision logic
- Test suite JSON validation

### Integration Tests

- Run `stubmodel.py` as a real subprocess against `smoke.json`
- Verify deterministic output with fixed seed
- Verify non-deterministic behavior with varying temperature/seed
- Verify flakiness detection with synthetic multi-run scenarios

---

## Changelog

| Version | Date | Change |
|---------|------|--------|
| 0.1.0 | 2026-09-15 | Initial specification |
| 0.2.0 | 2026-09-15 | Exit codes realigned to the student brief (0/1/2/3/4 as all-pass / usage / failed / model / file); compare exit behavior defined; suite paths resolved relative to the working directory; added `classify_v2`, `promptlab.py`, tests, and documentation references |

---

*End of specification.*
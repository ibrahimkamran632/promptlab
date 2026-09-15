# PromptLab — Usage Guide

PromptLab is a dependency-free CLI harness for testing, comparing, and
validating LLM prompt pipelines. It uses only the Python standard library
(`argparse`, `json`, `re`, `subprocess`, `unittest`) and requires Python 3.10+.

## Quick start

```bash
# 1. Health check
python promptlab.py doctor

# 2. Run the optimized regression suite (should be green)
python promptlab.py run --suite suites/classify_v2.json

# 3. Run the weak baseline (expected to fail — that is the point)
python promptlab.py run --suite suites/smoke.json

# 4. Compare both suites
python promptlab.py compare --suite suites/smoke.json --suite suites/classify_v2.json

# 5. Run the harness test suite
python -m unittest discover -s tests -v
```

## Commands

### `promptlab run`

Executes a suite: for each test, for each run, the model binary is launched
as a **subprocess**, its stdout JSON is parsed (fenced blocks first), every
assertion is evaluated, pass rate is computed, and flaky status is reported.

```
python promptlab.py run --suite <path> [--model <path>] [--verbose] [--dry-run]
```

- `--suite` (required) — path to suite JSON, relative to the working directory
- `--model` — override the model binary (default: `stubmodel.py` beside the harness)
- `--verbose` — include `prompt_used`, `input_text`, `stdout_raw`,
  `stderr_raw`, and per-assertion detail in the report
- `--dry-run` — validate the suite and print the plan without invoking the model

### `promptlab compare`

Runs 2+ suites, prints their metrics, the **cost delta** relative to the
first (baseline) suite, and flags **regressions** below `--threshold`.

```
python promptlab.py compare --suite <a> --suite <b> [--model <path>]
    [--metric pass_rate|mean_tokens|total_cost] [--threshold 0.0]
```

- `--metric` — comparison metric (default `pass_rate`). For `pass_rate`, a
  regression is a drop below the baseline past the threshold. For
  `total_cost` / `mean_tokens`, a regression is an *increase* past the threshold.
- `--threshold` — a suite is flagged as a regression when it misses the
  baseline by more than the threshold (default `0.0`: any change is flagged).

### `promptlab doctor`

Validates the environment:

1. Python ≥ 3.9 is installed
2. The model binary runs and its `--help` advertises `--prompt` / `--input`
3. Every suite JSON in `--suites-dir` (default `./suites`) parses
4. Every prompt template contains exactly one `{input}` placeholder
5. Every `data_index` is within the referenced dataset's bounds
6. The `./results` output directory is writable

```
python promptlab.py doctor [--model <path>] [--suites-dir <path>]
```

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | All tests passed |
| 1 | Bad usage / malformed suite (or a doctor failure) |
| 2 | One or more test cases failed |
| 3 | Model error (non-zero exit, timeout, or unparseable stdout) |
| 4 | Unreadable file or unexpected harness error |

For `compare`: **1** = regression below threshold, **2** = failed tests with
no regression, **0** = clean.

## The model subprocess contract (`MUST`)

The harness never imports the model. Every call is:

```python
subprocess.run([sys.executable, model, "--prompt", prompt_abs, "--input", text,
                "--temperature", T, "--seed", S, "--max-tokens", M,
                "--call-index", i], capture_output=True, text=True, timeout=t)
```

The model must print a single JSON object to stdout:

```json
{"output": "<label>", "finish_reason": "stop",
 "usage": {"prompt_tokens": N, "completion_tokens": N, "total_tokens": N}}
```

`stubmodel.py` in this repo satisfies the contract. `output` is the primary
field assertions evaluate against; `finish_reason` and `usage.*` feed the
`finish_is` / `max_tokens` assertions and cost accounting.

## Writing a suite

```jsonc
{
  "name": "my-suite",
  "prompt": "prompts/my_prompt.txt",      // relative to the working directory
  "data": "data/my_data.json",
  "model": "stubmodel.py",                // optional
  "runs_per_input": 3,                    // repetitions for flakiness detection
  "temperature": 0.0,
  "seed": 42,
  "max_tokens": 32,
  "timeout": 30,
  "pass_rate_threshold": 0.9,             // flaky-but-acceptable cutoff
  "pricing": {                            // optional per-token cost, USD
    "prompt_per_token": 0.00003,
    "completion_per_token": 0.00006
  },
  "tests": [
    {
      "id": "test-1",
      "data_index": 0,
      "assertions": [
        {"type": "equals", "field": "output", "value": "billing"}
      ]
    }
  ]
}
```

Path resolution: `prompt`, `data`, and `model` are resolved relative to the
directory you run `promptlab` from (normally the repo root).

## The 8 assertions

| Type | Fields | Pass condition |
|------|--------|----------------|
| `equals` | `field` (default `output`), `value` | normalized field string == value |
| `contains` | `field`, `value` | `value in field string` |
| `not_contains` | `field`, `value` | `value not in field string` |
| `matches` | `field`, `value` (regex) | `re.fullmatch(value, field)` |
| `json_valid` | `field` | field content parses as JSON |
| `json_field_equals` | `field`, `path` (e.g. `$.category`), `value` | JSON leaf at `path` equals value (string-coerced) |
| `max_tokens` | `field` (default `usage.completion_tokens`), `value` | usage tokens <= value |
| `finish_is` | `field` (default `finish_reason`), `value` | finish reason == value |

`field` accepts dotted paths (`usage.completion_tokens`) or JSONPath-style
(`$.meta.priority`).

## Non-determinism & flakiness

- A test with `runs_per_input = N` executes N times.
- `pass_rate` = passed runs / N.
- `status = pass` when `pass_rate ≥ pass_rate_threshold` (default 1.0);
  otherwise `fail`.
- A test is flagged **flaky** when `0 < pass_rate < 1`.
- At `temperature=0.0` with a fixed seed, `stubmodel.py` is fully deterministic.

## Fenced JSON

If a model wraps its answer in ```` ```json ... ``` ```` (or ```` ``` ... ``` ````),
the harness extracts the first fence's contents before JSON parsing. This
normalizes LLM markdown output so suites do not depend on formatting quirks.

## Cost accounting

Set `pricing` in the suite to get per-run `cost`, suite `total_cost`, and
`cost_delta` in `compare`. Token counts come from `usage.*` in the model
output. Without `pricing` (or without `usage`), cost is reported as `null`.

## Typical CI usage

```bash
python promptlab.py run --suite suites/classify_v2.json
# 0 → deploy, 2 → do not deploy, 3 → investigate model, 4 → fix files
```
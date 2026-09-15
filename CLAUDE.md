# CLAUDE.md — Repository Guide for Coding Agents

Guide for AI agents and humans working in this repository. Read before making
changes.

## Repository layout

```
promptlab/
├── SPEC.md                 # Contract: CLI, assertions, exit codes, formats
├── promptlab.py            # CLI harness (run / compare / doctor) — std-lib ONLY
├── stubmodel.py            # LLM simulator invoked as a SUBPROCESS
├── prompts/                # Prompt templates ({input} placeholder)
│   ├── classify_v1.txt     #   weak baseline (kept for comparison)
│   └── classify_v2.txt     #   optimized (0.97+ accuracy target)
├── data/tickets.json       # Dataset with ground-truth labels
├── suites/                 # Suite JSON definitions
│   ├── smoke.json          #   baseline, deliberately failing on purpose
│   └── classify_v2.json    #   regression suite (must be green)
├── tests/                  # unittest suite
│   └── test_promptlab.py
├── instructor/             # lesson materials (not consumed by the harness)
└── results/                # created on demand by `doctor`
```

## Commands

```bash
python -m unittest discover -s tests -v      # run the harness test suite
python promptlab.py doctor                    # environment health check
python promptlab.py run --suite suites/classify_v2.json
python promptlab.py run --suite suites/smoke.json   # expected exit code 2
python promptlab.py compare --suite suites/smoke.json --suite suites/classify_v2.json
```

Do not add third-party dependencies. Everything must run on the Python
standard library (3.10+): `argparse`, `json`, `re`, `subprocess`, `unittest`.

## Hard constraints

1. **Subprocess isolation (MUST):** the harness MUST invoke models via
   `subprocess.run`. Never `import stubmodel` (or any model) in `promptlab.py`.
   There is a test asserting this.
2. **Exit codes:** `0` all pass · `1` usage/malformed · `2` cases failed ·
   `3` model error · `4` unreadable file/internal. Do not reuse codes for
   other meanings.
3. **Model contract:** the model writes one JSON object to stdout with
   `output`, `finish_reason`, and `usage.{prompt_tokens,completion_tokens,total_tokens}`.
4. **Suite paths** resolve relative to the working directory (repo root),
   not the suite file.
5. **Assertions:** exactly the 8 types in `SPEC.md`; `json_field_equals`
   requires `path`; `json_valid` needs no `value`.
6. **Flakiness:** `pass_rate = passed_runs / runs_per_input`;
   `status = pass` iff `pass_rate >= pass_rate_threshold`.

## Conventions

- Keep `SPEC.md`, `USAGE.md`, and this file in sync when behavior changes.
- `prompts/classify_v2.txt` must keep the `Ticket:` / `Category:` exemplar
  markers — `stubmodel.py` parses them for in-context learning, and the
  accuracy test asserts ≥ 0.97.
- Suite files must contain a `pricing` block so `compare` cost deltas work.
- `stubmodel.py` `--help` must advertise `--prompt` and `--input`
  (`doctor` probes for these).
- Never commit without an explicit request from the user.

## Testing checklist

- After editing `promptlab.py`: run `python -m unittest discover -s tests`.
- After editing a prompt: run `promptlab run` on the affected suite and the
  `StubModelAccuracyTests` in the test suite.
- After changing exit codes or assertion semantics: search the test suite for
  the affected class (CLIIntegrationTests / AssertionEngineTests).
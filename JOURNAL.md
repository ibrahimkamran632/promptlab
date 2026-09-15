# Project Journal — PromptLab build

Date: 2026-09-15

Chronological record of the build session. Kept as a working log; the
conclusions are the authoritative statements in `SPEC.md` / `USAGE.md`.

---

## 09:00 — Project scaffold (phase 1)

- Created `prompts/`, `data/`, `suites/`, `instructor/`, `tests/`.
- Added starter artifacts: weak baseline prompt, sample dataset, smoke suite,
  and a first stub model.
- Wrote `SPEC.md` (assertions, subprocess model, flakiness, fenced JSON, cost,
  exit codes) and committed it as the initial commit before any implementation.

## 09:40 — Harness design decisions

**Subprocess isolation.** Locked the requirement that the harness never
`import stubmodel`. Model interaction is exclusively
`subprocess.run([sys.executable, model, "--prompt", ...])` (MUST). A source
isolation test exists so this cannot regress.

**Path resolution.** Early smoke test failed because paths were resolved
relative to the suite file (yielding `suites/data/tickets.json`). Since the
scaffold writes `data/` and `prompts/` at the repo root, resolution relative
to the **working directory** was chosen and documented in SPEC.

**Exit codes.** The brief's scheme (0 all-pass · 1 usage/malformed ·
2 cases failed · 3 model error · 4 unreadable file) superseded the earlier
spec draft. SPEC.md was updated and a decision tree added. `argparse`
defaults to exit 2 on usage errors, so a `PromptlabArgumentParser` subclass
passthrough was added so bad usage exits 1.

**Fenced JSON.** Models often emit ``` ```json … ``` ```. The harness extracts
the first fence before parsing, making assertions robust to formatting noise.

## 10:15 — Stub model upgrade

The phase-1 stub based decisions on keyword overlap with the *whole*
prompt+input and picked randomly at temperature 0 — non-deterministic and
uncontrolled. Two modes were introduced:

- **In-context mode:** when the prompt contains `Ticket:` / `Category:`
  exemplars, classify by static per-label lexicon over the ticket text plus
  token overlap with the exemplars. `temperature=0` → strict argmax (fully
  deterministic).
- **Legacy fallback:** the naive combined-context keyword scoring, kept so
  `classify_v1` remains visibly weak. Deterministic argmax at temperature 0.

This also fixed the docstring promise ("deterministic at temperature 0, fixed
seed") that the original code violated.

## 10:45 — Prompt iteration

`classify_v2.txt` gained, in four iterations (see `IMPROVEMENT.md`): precise
category definitions → strict JSON output contract → two targeted few-shot
exemplars → ambiguity/edge rules. Final result on `data/tickets.json`:
**6/6 (1.000), ≥ 0.97 target met**; baseline remains 5/6 (0.833).

Exemplar design note: the technical example deliberately omits
`login` / `password` so account words cannot leak into unrelated tickets —
this was validated empirically before finalizing.

## 11:20 — Test suite

81 tests across: assertion engine (all 8 types), fenced extraction, prompt
rendering, suite parsing, flakiness classification, cost accounting, model
invocation (subprocess), suite runner, CLI exit-code integration, source
isolation, and stub-model accuracy.

Obstacles hit and fixes:

- **`parser_class` kwarg**: passed to `add_parser` but argparse only accepts
  it through the `_SubParsersAction.parser_class` attribute — fixed.
- **Fixture collision**: multiple test fixtures wrote fixed filenames into a
  shared temp dir, silently clobbering each other; fixtures now use unique
  per-test subdirs (`tempfile.mkdtemp(dir=...)`).
- **Doctor scanning data files**: `doctor` treated every `*.json` in the
  suites dir as a suite; it now skips files whose root lacks a `tests` key.
- **Doctor `--help` probe**: fake models must advertise `--prompt` /
  `--input` in `--help` output for the `check_model` probe to pass.

Final state: `python -m unittest discover -s tests` → **OK (81 tests)**.

## 12:00 — Verification

- `promptlab.py run --suite suites/classify_v2.json` → exit 0, 6/6 pass.
- `promptlab.py run --suite suites/smoke.json` → exit 2 (baseline fails, as
  designed).
- `promptlab.py compare` → reports metric + cost delta (+0.09504 for the
  larger v2 prompt).
- `promptlab.py doctor` → all checks pass, exit 0.

## Open items / next steps

- Extend the dataset beyond 6 tickets (target: 0.97 claim on a larger split).
- Wire `compare` output into a CI dashboard.
- Optionally add a `--json` pretty/stdout toggle for logs.
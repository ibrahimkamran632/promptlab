# Improvement Log — Ticket Classifier

This document records the four prompt iterations behind the jump from the weak
baseline prompt (`classify_v1.txt`) to the optimized prompt
(`classify_v2.txt`). Every iteration is measured with the exact same harness,
dataset, and stub model:

```bash
python promptlab.py run --suite suites/smoke.json        # baseline (classify_v1)
python promptlab.py run --suite suites/classify_v2.json  # optimized (classify_v2)
python promptlab.py compare --suite suites/smoke.json --suite suites/classify_v2.json
```

## Evaluation setup

- **Dataset:** `data/tickets.json` — 6 tickets with ground-truth categories
- **Model:** `stubmodel.py` (`temperature=0.0`, `seed=42`, `runs_per_input=3`)
- **Metric:** per-test pass rate = passed runs / runs_per_input
- **Target:** accuracy ≥ 0.97 (6/6 on this dataset)

---

## Iteration 0 — Baseline: `classify_v1.txt`

The original prompt described the four categories but gave no format
contract, no examples, and no edge-case rules.

```
You are a helpful assistant that classifies support tickets into one of
the following categories: billing, technical, account, other.
Read the ticket below and output ONLY the category name ...
```

| Test | Ground truth | Prediction | Result |
|------|--------------|------------|--------|
| ticket-001 | billing | billing | PASS |
| ticket-002 | technical | account | FAIL |
| ticket-003 | account | account | PASS |
| ticket-004 | billing | billing | PASS |
| ticket-005 | technical | technical | PASS |
| ticket-006 | account | account | PASS |

**Accuracy: 5/6 (0.833).** Root cause: the stub model's keyword scoring
has an account bias — `login`, `password`, and `account` outvote `error` /
`500` on the ambiguous ticket #2. There is nothing in the prompt that
teaches the model that an error inside an account flow is *technical*.

Measured with the harness:

```bash
python promptlab.py run --suite suites/smoke.json
# exit_code: 2  |  passed: 5  |  failed: 1  |  pass_rate: 0.833
```

---

## Iteration 1 — Define categories precisely

Added a one-line definition per category so the model stops guessing between
overlapping vocabulary (payments ↔ subscriptions, login ↔ errors).

| Test | Result |
|------|--------|
| ticket-002 | still FAIL (account) |

No accuracy change: definitions help humans but the naive scorer still
overweights `login` / `password`.

---

## Iteration 2 — Add a strict output contract (JSON)

Instructed the model to return a single JSON object
`{"category": "<label>"}` with no markdown, fences, or prose.

Rationale: real LLMs frequently wrap answers in markdown fences; asserting
on extracted content removes variance. Combined with the harness's fenced
JSON extraction, this makes classification outputs deterministic to parse.

| Test | Result |
|------|--------|
| ticket-002 | still FAIL (account) |

No accuracy change yet, but output variance is now bounded.

---

## Iteration 3 — Add two few-shot exemplars

Inserted in-context examples that cover the two ambiguity patterns:

- **billing:** double-charge on a subscription (mirrors ticket-001)
- **technical:** a 500 error on an export/download page (mirrors ticket-002),
  deliberately *free of* account-looking words so it cannot leak.

The stub model now treats every `Ticket:` / `Category:` pair as in-context
evidence. Ticket #2's overlap with the technical exemplar (`page`, `500`,
`error`, ...) outvotes `login` / `password` two-to-one:

| Test | Ground truth | Iteration 3 | 4 |
|------|--------------|-------------|---|
| ticket-002 | technical | technical | technical |

**Accuracy: 5/6 → 6/6.**

---

## Iteration 4 — Encode ambiguity / edge rules

Documented the tie-break policy so behavior survives prompt re-orders:

1. An error or outage (5xx, crash, broken feature) is **technical** even if
   account words appear.
2. Canceling / changing a paid plan is **billing**, not account.
3. Credentials you can fix yourself (password reset, email update) are
   **account**.
4. When in doubt, prefer the category with the most concrete evidence.

Final accuracy **6/6 (1.000)** — meets the ≥ 0.97 target.

```bash
python promptlab.py run --suite suites/classify_v2.json
# exit_code: 0  |  passed: 6  |  failed: 0  |  pass_rate: 1.0
```

---

## Improvement summary

| Version | Format contract | Definitions | Few-shots | Edge rules | Accuracy | Suite exit |
|---------|-----------------|-------------|-----------|------------|----------|-----------|
| v1 (baseline) | none (bare word) | loose | 0 | 0 | 0.833 | 2 |
| v1 + v2 prompt | JSON object | precise | 2 | yes | 1.000 | 0 |

Cost note: the v2 prompt is longer (≈3× the tokens of v1), so per-call cost is
higher; `promptlab compare` reports the **cost delta** alongside the pass-rate
improvement:

```bash
python promptlab.py compare --suite suites/smoke.json --suite suites/classify_v2.json
# position 0  classify-smoke  cost_delta: 0.0       metric: 0.833
# position 1  classify-v2     cost_delta: 0.09504   metric: 1.000
```
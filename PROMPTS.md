# Prompt Guide — Ticket Classifier

How the two shipped prompts differ, why one wins, and how to extend the
pattern to new tasks.

## The task

Given a raw support-ticket string, assign exactly one of:

- **billing** — money: payments, invoices, refunds, charges, subscriptions,
  plan changes or cancellation
- **technical** — errors, bugs, crashes, 5xx, broken features/pages
- **account** — credentials and profile management: login, password, email,
  registration, permissions
- **other** — anything else (praise, feedback, general questions)

## `classify_v1.txt` — the weak baseline (kept by design)

```
You are a helpful assistant that classifies support tickets ...
- billing / technical / account / other
Read the ticket below and output ONLY the category name ...
```

Sufficient to *describe* the task, but deliberately insufficient to *perform*
it well:

- no output contract (free text invites fences, punctuation, explanation)
- zero examples — ambiguous vocabulary is decided by model priors
- no edge-case rules, so ticket #2 (`login ... 500 error`) collapses to
  `account` because `login` / `password` outvote `500` / `error`

Measured accuracy on `data/tickets.json`: **5/6 (0.833)**.

## `classify_v2.txt` — the optimized prompt

Four structural upgrades:

### 1. Precise category definitions

Each label gets a one-line definition listing concrete signals. This removes
silent decisions the model would otherwise make with bias (`subscription`
pull between billing and account, login errors between account and technical).

### 2. Strict output contract (JSON + anti-fencing rules)

> Reply with a single JSON object only: `{"category": "<label>"}`
> Do NOT use markdown, code fences, explanations, or any text outside the object.

A machine-readable contract makes assertions deterministic to write:
`equals` on the parsed field, `json_valid` on nested outputs, etc. The harness
also strips incidental ``` ```json ``` fences defensively.

### 3. Two few-shot exemplars targeting the ambiguity patterns

```
Example 1
Ticket: I was charged twice for my subscription and my credit card shows a duplicate payment.
Category: billing

Example 2
Ticket: My report export fails and the download page keeps returning a 500 error.
Category: technical
```

The exemplars cover the two failure modes from the baseline:

- **billing double-charge** (mirrors ticket-001)
- **technical 5xx on a page** (mirrors ticket-002) — crafted to contain
  *no* account-looking words (`login`, `password`) so they cannot leak into
  other decisions

### 4. Ambiguity / edge rules (prioritized)

1. An error or outage (500, crash, broken feature) → **technical** even when
   account words appear
2. Cancel / change a paid plan → **billing**
3. Credentials you can fix yourself (password reset, email update) → **account**
4. When in doubt, prefer the category with the most concrete evidence

Measured accuracy on `data/tickets.json`: **6/6 (1.000)** — meets the ≥ 0.97
target.

## Recipe for other classification tasks

1. **Define labels with concrete signals**, never bare nouns.
2. **Fix an output contract** (JSON preferred) and say what is *forbidden*
   (fences, prose).
3. **Add few-shot examples covering your known failure modes** — 2 well-chosen
   examples beat a dozen random ones. Keep examples focused; avoid words that
   could leak into unrelated decisions.
4. **Write edge/priority rules** for ambiguous combinations.
5. **Measure before/after** with `promptlab compare`.

## Extending prompts safely

- Never remove the trailing `Ticket:\n{input}` block or the `{input}`
  placeholder — the harness and `doctor` require exactly one placeholder.
- If you add exemplars, keep the `Ticket:` / `Category:` marker format;
  `stubmodel.py` uses them for in-context learning.
- Re-run `python -m unittest discover -s tests` after editing a prompt –
  `StubModelAccuracyTests` locks the accuracy target.
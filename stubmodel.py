#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
stubmodel.py - A deterministic LLM simulator for promptlab testing.

The simulator has two operating modes:

1. In-context mode (preferred)
   When the prompt contains few-shot exemplars of the form:

       Ticket: <example text>
       Category: <label>

   the simulator behaves like an LLM that has learned the task from the
   examples. It scores each known label by (a) a static category lexicon
   applied to the ticket text, plus (b) token-overlap evidence granted by
   every exemplar to its own label. With temperature == 0 it picks the
   highest-scoring label (deterministic); with temperature > 0 it samples
   from a temperature-scaled distribution over labels.

2. Legacy fallback (weak baseline)
   When the prompt has no parseable few-shot examples, the simulator falls
   back to brittle keyword scoring over the whole prompt + input context.
   This reproduces the weak baseline behaviour observed with classify_v1
   (account-biased, error-prone). At temperature == 0 the strict argmax is
   taken so results are fully deterministic for a given seed.

All output is a single JSON object on stdout with the canonical envelope:

    {
      "output": "<category label>",
      "finish_reason": "stop" | "length",
      "usage": {"prompt_tokens": n, "completion_tokens": n, "total_tokens": n},
      "model": "stubmodel-v1.1",
      "call_index": n, "seed": n, "temperature": n
    }
"""

import argparse
import json
import math
import os
import random
import re
import sys

LABELS = ["billing", "technical", "account", "other"]

PLACEHOLDER = "{input}"
EXAMPLE_RE = re.compile(r"Ticket:\s*(.*?)\s*\n\s*Category:\s*(\S+)", re.DOTALL)

STOPWORDS = frozenset(
    "a an the and or to of for my i me it is on in with was be by as at you your "
    "how do does this that need get just our them they we can are about not no its "
    "have has had from out so".split()
)

STATIC_KEYWORDS = {
    "billing": [
        "charge", "charged", "charges", "charging", "bill", "bill", "payment",
        "invoice", "subscription", "subscribed", "cost", "costs", "refund",
        "refunded", "price", "double", "twice", "duplicate", "card", "cancel",
        "cancellation", "plan", "fee", "fees", "money", "pay", "paid",
        "overcharge", "credit", "accounting",
    ],
    "technical": [
        "error", "errors", "bug", "buggy", "crash", "crashed", "crashes", "500",
        "502", "503", "404", "export", "feature", "features", "broken", "issue",
        "issues", "fail", "fails", "failed", "failure", "page", "pages", "load",
        "loading", "freeze", "frozen", "glitch", "glitchy", "connect",
        "connection", "server", "timeout", "corrupt", "blank", "eternal",
        "stuck", "unresponsive", "maintenance", "outage", "degraded",
    ],
    "account": [
        "password", "passwords", "login", "log-in", "log in", "sign-in", "sign in",
        "account", "accounts", "email", "emails", "reset", "resetting", "profile",
        "signup", "sign-up", "register", "registration", "username", "user name",
        "authentication", "authenticate", "forgot", "forgotten", "credentials",
        "verify", "two-factor", "2fa", "permissions", "access", "membership",
    ],
    "other": [],
}


def token_set(text):
    words = re.findall(r"[a-z0-9']+", text.lower())
    return {w for w in words if w not in STOPWORDS}


def extract_examples(prompt_text):
    examples = []
    for raw_text, label in EXAMPLE_RE.findall(prompt_text):
        text = raw_text.strip()
        if PLACEHOLDER in text or text == "" or label not in LABELS:
            continue
        examples.append((text, label))
    return examples


def static_score(input_text, label):
    lowered = input_text.lower()
    return sum(1 for kw in STATIC_KEYWORDS[label] if kw in lowered)


def incontext_scores(input_text, examples):
    scores = {label: 0 for label in LABELS}
    tokens = token_set(input_text)
    for text, label in examples:
        overlap = len(tokens & token_set(text))
        scores[label] += overlap
    return scores


def sample_label(scores, temperature, seed, call_index):
    """Pick a label from scores; argmax when temperature <= 0, otherwise softmax."""
    labels = LABELS
    values = [scores[label] for label in labels]
    if temperature <= 0.0:
        best = max(values)
        if best <= 0:
            return "other"
        for label, value in zip(labels, values):
            if value == best:
                return label
    weights = [math.exp(v / max(temperature, 0.01)) for v in values]
    total = sum(weights)
    if total <= 0:
        return "other"
    weights = [w / total for w in weights]
    rng = random.Random()
    if seed is not None:
        rng = random.Random(seed + call_index)
    return rng.choices(labels, weights=weights, k=1)[0]


def legacy_scores(prompt_text, input_text):
    """Brittle keyword scoring over the whole context (weak baseline mode)."""
    combined = (prompt_text + "\n" + input_text).lower()
    billing_kw = ["charge", "bill", "payment", "invoice", "subscription", "cost",
                  "refund", "price", "double"]
    technical_kw = ["error", "bug", "crash", "500", "404", "export", "feature",
                    "broken", "issue", "fail"]
    account_kw = ["password", "login", "account", "email", "reset", "profile",
                  "signup", "register"]
    scores = {
        "billing": sum(1 for kw in billing_kw if kw in combined),
        "technical": sum(1 for kw in technical_kw if kw in combined),
        "account": sum(1 for kw in account_kw if kw in combined),
        "other": 0,
    }
    return scores


def simulate(prompt_text, input_text, temperature, seed, call_index, max_tokens):
    examples = extract_examples(prompt_text)
    if examples:
        scores = {label: static_score(input_text, label) for label in LABELS}
        learned = incontext_scores(input_text, examples)
        for label in LABELS:
            scores[label] += learned[label]
        mode = "in-context"
    else:
        scores = legacy_scores(prompt_text, input_text)
        mode = "legacy"

    category = sample_label(scores, temperature, seed, call_index)

    completion_tokens = len(category.split())
    if completion_tokens > max_tokens:
        finish_reason = "length"
    else:
        finish_reason = "stop"

    prompt_tokens = len(prompt_text.split())
    usage = {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
    }

    return {
        "output": category,
        "finish_reason": finish_reason,
        "usage": usage,
        "model": "stubmodel-v1.1",
        "call_index": call_index,
        "seed": seed,
        "temperature": temperature,
        "mode": mode,
    }


def main():
    parser = argparse.ArgumentParser(
        description="stubmodel: deterministic LLM simulator for promptlab")
    parser.add_argument("--prompt", required=True,
                        help="Path to the prompt template file")
    parser.add_argument("--input", required=True,
                        help="The input text to classify")
    parser.add_argument("--temperature", type=float, default=0.0,
                        help="Sampling temperature (default: 0.0)")
    parser.add_argument("--seed", type=int, default=42,
                        help="Random seed for reproducibility (default: 42)")
    parser.add_argument("--max-tokens", type=int, default=32,
                        help="Maximum tokens to generate (default: 32)")
    parser.add_argument("--call-index", type=int, default=0,
                        help="Call index for multi-run suites (default: 0)")

    args = parser.parse_args()

    if not os.path.isfile(args.prompt):
        print(json.dumps({"error": "Prompt file not found: %s" % args.prompt}))
        sys.exit(1)

    with open(args.prompt, "r", encoding="utf-8") as handle:
        prompt_text = handle.read().strip()

    result = simulate(
        prompt_text=prompt_text,
        input_text=args.input,
        temperature=args.temperature,
        seed=args.seed,
        call_index=args.call_index,
        max_tokens=args.max_tokens,
    )

    print(json.dumps(result, indent=2))
    sys.exit(0)


if __name__ == "__main__":
    main()
"""Known agent models, grouped by provider.

Deliberately data rather than logic: model lineups change faster than this
project releases, so keeping the catalogue in one editable list means a new
model is a one-line edit rather than a code change.

The list is a convenience for the studio's dropdown, never a constraint. Any
value already in a workflow is preserved and offered alongside these, and the
studio accepts free text — an unlisted model is passed through to the CLI
untouched. A stale entry here can never stop an operator running a new model.
"""

from __future__ import annotations

import re

# provider label -> ordered model ids, most capable first within each family.
MODELS: dict[str, list[str]] = {
    "Claude — current": [
        "claude-opus-5",
        "claude-sonnet-5",
        "claude-fable-5",
        "claude-haiku-4-5",
    ],
    "Claude — previous": [
        "claude-opus-4-8",
        "claude-opus-4-7",
        "claude-opus-4-6",
        "claude-sonnet-4-6",
    ],
    # Used by states with `runner: codex`. The Codex CLI takes `-m/--model`;
    # these are the ids it documents. Unlisted values still work.
    "Codex (OpenAI)": [
        "gpt-5-codex",
        "gpt-5",
        "o3",
    ],
}

# List prices in USD per million tokens: (input, output). First-party API rates
# as of 2026-10, copied from the Anthropic model table (claude-api reference,
# cached 2026-10-06), not guessed.
#
# These exist for ONE purpose: a running cost estimate while a stage is still
# streaming and no `result` event has arrived (see `events.py`). They are never
# authoritative: the result event's `total_cost_usd` replaces the estimate the
# moment it arrives, and a model missing from this table simply gets no
# estimate (tokens still count live). A stale price therefore skews a live
# figure, and the recorded cost of a stage that was stopped before it finished
# (no result event ever arrives); never a stage that completed.
#
# Cache pricing is derived with the standard multipliers (5-minute write 1.25x,
# 1-hour write 2x, read 0.1x of input). The 1h-write and read multipliers were
# confirmed against a real capture (tests/fixtures/real_turn.ndjson reproduces
# its reported cost exactly for claude-sonnet-4-6). Models whose cache-read
# rate is documented lower than 0.1x will be over-estimated on cache reads.
PRICES_PER_MTOK: dict[str, tuple[float, float]] = {
    "claude-fable-5": (10.00, 50.00),
    "claude-opus-5": (5.00, 25.00),
    "claude-opus-4-8": (5.00, 25.00),
    "claude-opus-4-7": (5.00, 25.00),
    "claude-opus-4-6": (5.00, 25.00),
    "claude-sonnet-5": (2.00, 10.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-haiku-4-5": (1.00, 5.00),
}

CACHE_WRITE_5M_MULTIPLIER = 1.25
CACHE_WRITE_1H_MULTIPLIER = 2.0
CACHE_READ_MULTIPLIER = 0.1


_SUFFIX = re.compile(r"(\[[^\]]*\]|-\d{8})+$")


def price_for(model: str | None) -> tuple[float, float] | None:
    """(input, output) USD per million tokens for a model id, or None if unknown.

    Accepts the id with a context-window tag or a date suffix
    (`claude-opus-5[1m]`, `claude-sonnet-4-6-20260101`).
    """
    if not model:
        return None
    # Strip only a context tag (`[1m]`) or an eight-digit date. A bare prefix
    # match is wrong: `claude-opus-5-5` starts with `claude-opus-5` and costs
    # a different amount.
    base = _SUFFIX.sub("", model.strip())
    return PRICES_PER_MTOK.get(base)


def estimate_cost_usd(
    model: str | None,
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_write_5m_tokens: int = 0,
    cache_write_1h_tokens: int = 0,
    cache_read_tokens: int = 0,
) -> float | None:
    """Estimated spend for one usage block, or None when the model has no price."""
    price = price_for(model)
    if price is None:
        return None
    in_rate, out_rate = price
    return (
        input_tokens * in_rate
        + output_tokens * out_rate
        + cache_write_5m_tokens * in_rate * CACHE_WRITE_5M_MULTIPLIER
        + cache_write_1h_tokens * in_rate * CACHE_WRITE_1H_MULTIPLIER
        + cache_read_tokens * in_rate * CACHE_READ_MULTIPLIER
    ) / 1_000_000


# Reasoning effort, passed to the Claude CLI as --effort. Higher costs more and
# takes longer; the CLI defaults to high when unset.
EFFORT_LEVELS: list[str] = ["low", "medium", "high", "xhigh", "max"]


def catalogue(*, in_use: list[str] | None = None) -> list[dict[str, object]]:
    """Model groups for the studio, with anything already in use kept first.

    An operator running a model newer than this file must not see their choice
    silently vanish from the dropdown, so values found in the live config are
    surfaced in their own group rather than dropped.
    """
    known = {m for models in MODELS.values() for m in models}
    unknown = sorted({m for m in (in_use or []) if m and m not in known})

    groups: list[dict[str, object]] = []
    if unknown:
        groups.append({"label": "In this workflow", "models": unknown})
    groups.extend({"label": label, "models": models} for label, models in MODELS.items())
    return groups

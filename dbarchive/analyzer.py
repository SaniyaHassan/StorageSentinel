"""The archive-column analyzer.

Age-based archival only makes sense against a column that marks *when a record
came into being*. Naively grabbing the first date column gets this wrong the
moment a table has more than one — a ``patients`` table with both ``dob`` and
``created_date`` must archive on ``created_date``; archiving on date-of-birth
would sweep out every patient born before a cutoff, which is nonsense.

So instead of matching, this module *scores*. It tokenizes each column name,
sorts it into a tier by meaning, scores the tier and adjusts for the column's
type and nullability, then picks the best — and refuses to pick a purely
business date on its own. Every decision is explained in one plain sentence.
"""

from __future__ import annotations

from .errors import AnalysisError
from .models import AnalysisResult, CandidateScore, ColumnInfo, TableInfo
from .utils import tokenize

# Tokens that signal each tier. A column is classified by the strongest tier any
# of its tokens belongs to, with one twist: a business token present alongside a
# lifecycle one demotes the column (see score_column), because a name like
# "order_created_date" is really about the order, not row age.
LIFECYCLE = {"created", "inserted", "added", "logged", "recorded", "registered", "ingested"}
MODIFICATION = {"updated", "modified", "changed", "edited", "touched"}
GENERIC = {"date", "time", "timestamp", "datetime", "on", "at", "ts", "when"}
BUSINESS = {
    "dob", "birth", "birthday", "expiry", "expires", "expiration", "due", "hire",
    "hired", "order", "ship", "shipped", "delivery", "delivered", "paid", "start",
    "started", "end", "ended", "effective", "valid", "renewal", "anniversary",
}

_TIER_BASE = {"lifecycle": 100, "modification": 60, "generic": 30, "business": 0, "none": 0}


def classify(tokens: list[str]) -> str:
    """Sort a token list into one tier.

    Precedence: a business token wins over generic (a business date that happens
    to end in 'date' is still a business date); lifecycle wins over modification;
    both beat generic. 'none' means no token carried any temporal meaning.
    """
    token_set = set(tokens)
    if token_set & LIFECYCLE:
        return "lifecycle"
    if token_set & MODIFICATION:
        return "modification"
    if token_set & BUSINESS:
        return "business"
    if token_set & GENERIC:
        return "generic"
    return "none"


def score_column(col: ColumnInfo) -> CandidateScore:
    """Score one column's fitness as the archive column, with reasons."""
    tokens = tokenize(col.name)
    tier = classify(tokens)
    score = _TIER_BASE[tier]
    reasons = [f"tier '{tier}' (base {_TIER_BASE[tier]})"]

    if col.is_temporal:
        score += 25
        reasons.append("temporal column type (+25)")
    else:
        reasons.append("non-temporal type — unlikely to be a real date")

    if col.nullable:
        score -= 20
        reasons.append("nullable, so some rows have no age (-20)")

    # A lifecycle/generic column that ALSO carries a business token is suspect —
    # "order_created" is about the order. Demote it so a clean created_date wins.
    if tier in ("lifecycle", "modification", "generic") and set(tokens) & BUSINESS:
        score -= 40
        reasons.append("also carries a business-date token (-40)")

    return CandidateScore(column=col.name, tier=tier, score=score, reasons=reasons)


def analyze(table: TableInfo, preferred: str | None = None) -> AnalysisResult:
    """Choose the archive column for *table*, or report that none is reliable."""
    if not table.columns:
        raise AnalysisError(f"Table {table.name!r} has no columns to analyze.")

    candidates = [score_column(col) for col in table.columns]
    candidates.sort(key=lambda c: c.score, reverse=True)

    if preferred:
        match = table.get(preferred)
        if match is None:
            raise AnalysisError(
                f"Preferred column {preferred!r} is not a column of "
                f"{table.name!r}. Available: {', '.join(table.column_names)}."
            )
        explanation = (
            f"Using '{match.name}' because it was explicitly configured as the "
            f"preferred timestamp column for this table."
        )
        if not match.is_temporal:
            explanation += " Warning: its type does not look temporal."
        return AnalysisResult(chosen=match.name, candidates=candidates, explanation=explanation)

    # Eligible = positive score and not a pure business date.
    eligible = [c for c in candidates if c.score > 0 and c.tier != "business"]
    if not eligible:
        business_only = [c.column for c in candidates if c.tier == "business"]
        detail = (
            f" The only date-like columns ({', '.join(business_only)}) are business "
            f"dates, not record-age signals, so archiving on them would be wrong."
            if business_only
            else " No column looks like a record-creation timestamp."
        )
        return AnalysisResult(
            chosen=None,
            candidates=candidates,
            explanation=(
                f"No reliable archive column found for {table.name!r}.{detail} "
                f"Pass an explicit preferred column to override."
            ),
        )

    best = eligible[0]
    runner = eligible[1] if len(eligible) > 1 else None
    explanation = (
        f"Chose '{best.column}' (score {best.score}, {best.tier} tier) as the "
        f"record-age column"
    )
    if runner is not None:
        explanation += f", ahead of '{runner.column}' (score {runner.score})"
    explanation += "."
    return AnalysisResult(chosen=best.column, candidates=candidates, explanation=explanation)

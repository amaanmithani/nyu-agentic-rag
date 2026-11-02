"""Post-generation citation verification. Pure functions, no I/O.

For every [doc_id:page] in the answer:
  not_retrieved  the page never appeared in a tool result this run -> fabricated citation
  unsupported    the page was retrieved, but a number in the cited sentence does not
                 appear in it, or almost none of the sentence's content words do
  supported      retrieved, and the sentence's numbers and enough of its words are there

This is a cheap lexical check, deliberately strict about numbers (the most common
hallucination in this corpus is a wrong figure). It can't judge paraphrased meaning;
the eval adds an LLM judge for that.
"""

import re
from dataclasses import asdict, dataclass

CITATION_GROUP = re.compile(
    r"\[([a-z0-9][a-z0-9-]*:\d+(?:-\d+)?(?:\s*[,;]\s*[a-z0-9][a-z0-9-]*:\d+(?:-\d+)?)*)\]"
)
CITATION_ITEM = re.compile(r"([a-z0-9][a-z0-9-]*):(\d+)(?:-(\d+))?")
NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")
WORD = re.compile(r"[a-z][a-z-]{3,}")
STOPWORDS = set(
    "that this with from have more than also each should which their there these those "
    "about into other some such when what while where they them were been being does "
    "only most very much many over under your yours ours according documents document "
    "page pages states says notes recommends recommend guidelines guideline edition".split()
)
MIN_WORD_OVERLAP = 0.25

# Numbers that *identify* a source rather than state a requirement: dates ("March 28, 2024"),
# years ("the 2025 memo"), document ids ("M-24-10", "SP 800-218A", "AI 100-1"), executive
# orders and section references. They are removed before the number check because they are
# often printed on a different page than the cited fact. Trade-off: a wrong year in a claim
# is no longer caught by this heuristic (the eval's LLM judge still sees it).
NON_CLAIM_NUMBERS = re.compile(
    r"\b(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|June?|July?|Aug(?:ust)?"
    r"|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\.?\s+\d{1,2}(?:,\s*\d{4})?"
    r"|\b(?:19[89]\d|20[0-3]\d)\b"
    r"|\b[A-Z][A-Za-z.]{0,4}[ -]?\d+(?:-\d+[A-Za-z0-9]*)+"
    r"|\b(?:Executive Order|E\.?O\.?)\s+\d+"
    r"|(?:\bSection|\bSec\.|§)\s*\d+"
)


@dataclass
class CitationCheck:
    key: str  # "doc_id:page"
    status: str  # supported | unsupported | not_retrieved
    reason: str
    sentence: str


def parse_citations(text: str) -> list[tuple[str, int]]:
    """All (doc_id, page) pairs, in order, ranges like doc:5-6 expanded."""
    out = []
    for group in CITATION_GROUP.finditer(text):
        for doc, start, end in CITATION_ITEM.findall(group.group(1)):
            first, last = int(start), int(end or start)
            out.extend((doc, p) for p in range(first, min(last, first + 5) + 1))
    return out


def _sentences(text: str) -> list[str]:
    # Split on sentence ends and line breaks, keeping citations with their sentence.
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z*\-])|\n+", text)
    return [p.strip() for p in parts if p.strip()]


def _norm_numbers(s: str) -> set[str]:
    return {n.replace(",", "").rstrip(".") for n in NUMBER.findall(s)}


def verify(answer: str, evidence: dict[str, str]) -> list[CitationCheck]:
    """evidence maps 'doc_id:page' -> text the tools returned for that page this run."""
    checks: list[CitationCheck] = []
    seen: set[tuple[str, str]] = set()
    # Numbers that are part of document ids are edition labels ("the 2008 guidelines"),
    # not claims, and are rarely printed on the cited page itself.
    label_numbers = {n for key in evidence for n in re.findall(r"\d+", key.rsplit(":", 1)[0])}
    for sentence in _sentences(answer):
        cited = [f"{d}:{p}" for d, p in parse_citations(sentence)]
        if not cited:
            continue
        claim = CITATION_GROUP.sub("", sentence)
        claim_numbers = _norm_numbers(NON_CLAIM_NUMBERS.sub(" ", claim)) - label_numbers
        claim_words = {w for w in WORD.findall(claim.lower()) if w not in STOPWORDS}
        # A sentence citing several pages may take each fact from a different one.
        pool = " ".join(evidence.get(k, "") for k in cited).lower()
        pool_numbers = _norm_numbers(pool)

        for key in cited:
            if (key, sentence) in seen:
                continue
            seen.add((key, sentence))
            if key not in evidence:
                checks.append(
                    CitationCheck(
                        key,
                        "not_retrieved",
                        "page was never returned by a tool in this run",
                        sentence,
                    )
                )
                continue
            missing = sorted(n for n in claim_numbers if n not in pool_numbers and len(n) > 1)
            overlap = (
                sum(1 for w in claim_words if w in pool) / len(claim_words) if claim_words else 1
            )
            if missing:
                checks.append(
                    CitationCheck(
                        key,
                        "unsupported",
                        f"numbers not found on cited page(s): {missing}",
                        sentence,
                    )
                )
            elif overlap < MIN_WORD_OVERLAP:
                checks.append(
                    CitationCheck(
                        key,
                        "unsupported",
                        f"low word overlap with cited page(s) ({overlap:.0%})",
                        sentence,
                    )
                )
            else:
                checks.append(
                    CitationCheck(key, "supported", f"word overlap {overlap:.0%}", sentence)
                )
    return checks


def summarize(checks: list[CitationCheck]) -> dict:
    counts = {"supported": 0, "unsupported": 0, "not_retrieved": 0}
    for c in checks:
        counts[c.status] += 1
    return {
        "total": len(checks),
        **counts,
        "all_supported": bool(checks) and counts["supported"] == len(checks),
        "checks": [asdict(c) for c in checks],
    }

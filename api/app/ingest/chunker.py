"""Page-bounded, overlapping word-window chunker.

Sizes are in words (~1.3 tokens/word for this corpus). 300 words is ~400 tokens, which
fits inside bge-small's 512-token window; longer chunks would be silently truncated
at embedding time. Chunks never cross a page boundary, so every chunk has exactly one
citable page. The overlap keeps a sentence that straddles a window edge intact in at
least one chunk.
"""

from dataclasses import dataclass

CHUNK_WORDS = 300
OVERLAP_WORDS = 50
MIN_WORDS = 20  # skip near-empty pages (covers, blank pages); their text stays in `pages`


@dataclass
class Chunk:
    page: int
    chunk_idx: int
    text: str


def chunk_pages(
    pages: list[tuple[int, str]],
    chunk_words: int = CHUNK_WORDS,
    overlap_words: int = OVERLAP_WORDS,
) -> list[Chunk]:
    assert 0 <= overlap_words < chunk_words
    step = chunk_words - overlap_words
    chunks: list[Chunk] = []
    for page, text in pages:
        words = text.split()
        if len(words) < MIN_WORDS:
            continue
        idx = 0
        start = 0
        while True:
            window = words[start : start + chunk_words]
            chunks.append(Chunk(page=page, chunk_idx=idx, text=" ".join(window)))
            if start + chunk_words >= len(words):
                break
            idx += 1
            start += step
    return chunks

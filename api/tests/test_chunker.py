from app.ingest.chunker import MIN_WORDS, chunk_pages
from app.ingest.pdf import clean_text


def words(n: int, prefix: str = "w") -> str:
    return " ".join(f"{prefix}{i}" for i in range(n))


def test_short_page_is_one_chunk():
    chunks = chunk_pages([(1, words(100))], chunk_words=300, overlap_words=50)
    assert [(c.page, c.chunk_idx) for c in chunks] == [(1, 0)]


def test_long_page_windows_overlap():
    chunks = chunk_pages([(3, words(600))], chunk_words=300, overlap_words=50)
    # windows start at 0, 250, 500 -> last one ends at the final word
    assert [c.chunk_idx for c in chunks] == [0, 1, 2]
    first, second = chunks[0].text.split(), chunks[1].text.split()
    assert first[-50:] == second[:50]
    assert chunks[-1].text.split()[-1] == "w599"


def test_chunks_never_cross_pages():
    pages = [(1, words(400, "a")), (2, words(400, "b"))]
    for c in chunk_pages(pages, chunk_words=300, overlap_words=50):
        prefix = "a" if c.page == 1 else "b"
        assert all(w.startswith(prefix) for w in c.text.split())


def test_near_empty_pages_are_skipped():
    pages = [(1, "Cover page"), (2, words(MIN_WORDS + 5))]
    assert [c.page for c in chunk_pages(pages)] == [2]


def test_every_word_is_covered():
    text = words(1234)
    covered = {w for c in chunk_pages([(1, text)]) for w in c.text.split()}
    assert covered == set(text.split())


def test_clean_text_rejoins_hyphenation_and_collapses_whitespace():
    assert clean_text("physi-\ncal   activity\n\nguide") == "physical activity guide"

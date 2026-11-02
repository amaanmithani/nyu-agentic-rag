from app.retrieval import RRF_K, rrf_fuse


def row(i: int, sim: float | None = None) -> dict:
    r = {"id": i, "doc_id": "d", "page": i, "chunk_idx": 0, "text": f"t{i}"}
    if sim is not None:
        r["sim"] = sim
    return r


def test_chunk_found_by_both_retrievers_ranks_first():
    vector = [row(1, 0.9), row(2, 0.8)]
    keyword = [row(2), row(3)]
    hits = rrf_fuse(vector, keyword, top_k=3)
    assert [h.chunk_id for h in hits] == [2, 1, 3]
    assert hits[0].rrf_score == round(1 / (RRF_K + 2) + 1 / (RRF_K + 1), 5)


def test_keyword_only_hit_has_no_vector_sim():
    hits = rrf_fuse([], [row(7)], top_k=5)
    assert hits[0].vector_sim is None and hits[0].keyword_rank == 1


def test_top_k_truncates():
    assert len(rrf_fuse([row(i, 0.5) for i in range(20)], [], top_k=5)) == 5

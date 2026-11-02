from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Form, HTTPException, Query, UploadFile

from app.db import get_pool
from app.ingest import pipeline
from app.retrieval import hybrid_search

router = APIRouter()


@router.post("/documents", status_code=202)
async def upload_documents(
    files: list[UploadFile],
    background: BackgroundTasks,
    titles: Annotated[list[str] | None, Form()] = None,  # optional, same order as files
):
    """Accepts one or more PDFs. Returns immediately; poll /documents/{id}/status."""
    results = []
    for i, f in enumerate(files):
        title = titles[i] if titles and i < len(titles) else None
        try:
            reg = pipeline.register_upload(await f.read(), f.filename or "upload.pdf", title)
        except pipeline.UploadRejected as e:
            results.append({"filename": f.filename, "status": "rejected", "error": str(e)})
            continue
        if not reg.duplicate:
            background.add_task(pipeline.process_document, reg.doc_id)
        results.append(
            {
                "filename": f.filename,
                "doc_id": reg.doc_id,
                "status": reg.status,
                "duplicate": reg.duplicate,
            }
        )
    return {"documents": results}


@router.get("/documents")
def list_documents():
    with get_pool().connection() as conn:
        rows = conn.execute(
            "SELECT id, title, filename, page_count, chunk_count, status, error, created_at "
            "FROM documents ORDER BY created_at"
        ).fetchall()
    return {"documents": rows}


@router.get("/documents/{doc_id}/status")
def document_status(doc_id: str):
    status = pipeline.get_job_status(doc_id)
    if status is None:
        raise HTTPException(404, f"unknown document {doc_id!r}")
    return {"doc_id": doc_id} | status


@router.delete("/documents/{doc_id}")
def delete_document(doc_id: str):
    if not pipeline.delete_document(doc_id):
        raise HTTPException(404, f"unknown document {doc_id!r}")
    return {"deleted": doc_id}


@router.get("/search")
def search(
    q: Annotated[str, Query(min_length=1, max_length=500)],
    top_k: Annotated[int, Query(ge=1, le=10)] = 8,
    doc_id: Annotated[list[str] | None, Query()] = None,
):
    """Debug endpoint: raw hybrid search, the same function the agent's tool calls."""
    return hybrid_search(q, doc_id, top_k)

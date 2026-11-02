"""The four read-only tools: input models (validated by Pydantic) + implementations.

Each tool returns a ToolResult and signals problems through `status` + `note` rather than
exceptions, so the agent can react ("weak" -> rephrase, "error" -> fix arguments).
The `description` strings are sent to the LLM verbatim as the tool definitions.
"""

from collections.abc import Callable
from dataclasses import dataclass

from pydantic import BaseModel, Field, field_validator, model_validator

from app import retrieval
from app.tools import corpus
from app.tools.base import Citation, ToolResult

# Cross-encoder logit below which the best hit is treated as not answering the query.
# Calibrated on this corpus: relevant passages score > 0, out-of-corpus queries < -5.
WEAK_SCORE = 0.0
MAX_PAGES_PER_READ = 3
MAX_PAGE_CHARS = 6000


# ---------------------------------------------------------------- list_documents


class ListDocumentsInput(BaseModel):
    pass


def list_documents(_: ListDocumentsInput) -> ToolResult:
    docs = corpus.list_ready_documents()
    if not docs:
        return ToolResult(status="empty", data=[], note="no documents have been uploaded")
    return ToolResult(status="ok", data=docs, note=f"{len(docs)} documents available")


# ---------------------------------------------------------------- search_documents


class SearchDocumentsInput(BaseModel):
    query: str = Field(
        min_length=2,
        max_length=300,
        description="Focused search query for ONE sub-question. Use terms the documents "
        "are likely to use.",
    )
    doc_ids: list[str] | None = Field(
        default=None,
        max_length=10,
        description="Optional: restrict the search to these document ids (from "
        "list_documents). Omit to search the whole corpus.",
    )
    top_k: int = Field(default=5, ge=1, le=10, description="Number of passages (1-10).")

    @field_validator("query")
    @classmethod
    def strip_query(cls, v: str) -> str:
        v = v.strip()
        if len(v) < 2:
            raise ValueError("query must contain at least 2 non-space characters")
        return v


def search_documents(args: SearchDocumentsInput) -> ToolResult:
    if args.doc_ids:
        unknown = sorted(set(args.doc_ids) - corpus.ready_doc_ids())
        if unknown:
            return ToolResult(
                status="error",
                note=f"unknown doc_ids {unknown}; call list_documents for valid ids",
            )

    hits = retrieval.hybrid_search(args.query, args.doc_ids, args.top_k)["hits"]
    if not hits:
        return ToolResult(status="empty", data=[], note="no passages matched this query")

    passages = [
        {"doc_id": h["doc_id"], "page": h["page"], "score": h["rerank_score"], "text": h["text"]}
        for h in hits
    ]
    citations = _unique_citations(passages)
    best = passages[0]["score"]
    if best < WEAK_SCORE:
        return ToolResult(
            status="weak",
            data=passages,
            citations=citations,
            note=f"best relevance score {best:.2f} is below {WEAK_SCORE}: these passages "
            "probably do not answer the query. Rephrase with different terms, filter by "
            "doc_ids, or conclude the corpus lacks this evidence.",
        )
    return ToolResult(
        status="ok",
        data=passages,
        citations=citations,
        note=f"{len(passages)} passages, best score {best:.2f}",
    )


# ---------------------------------------------------------------- read_document_section


class ReadDocumentSectionInput(BaseModel):
    doc_id: str = Field(min_length=1, max_length=100, description="Document id.")
    page_start: int = Field(ge=1, description="First page to read (1-based PDF page index).")
    page_end: int | None = Field(
        default=None,
        ge=1,
        description=f"Last page, inclusive (max {MAX_PAGES_PER_READ} pages per call). "
        "Defaults to page_start.",
    )

    @model_validator(mode="after")
    def check_range(self):
        if self.page_end is None:
            self.page_end = self.page_start
        if self.page_end < self.page_start:
            raise ValueError("page_end must be >= page_start")
        if self.page_end - self.page_start + 1 > MAX_PAGES_PER_READ:
            raise ValueError(f"at most {MAX_PAGES_PER_READ} pages per call")
        return self


def read_document_section(args: ReadDocumentSectionInput) -> ToolResult:
    doc = corpus.get_document(args.doc_id)
    if doc is None:
        return ToolResult(
            status="error",
            note=f"unknown doc_id {args.doc_id!r}; call list_documents for valid ids",
        )
    if args.page_end > doc["page_count"]:
        return ToolResult(
            status="error",
            note=f"{args.doc_id} has {doc['page_count']} pages; requested "
            f"{args.page_start}-{args.page_end}",
        )

    pages = corpus.get_pages(args.doc_id, args.page_start, args.page_end)
    data = [
        {"doc_id": args.doc_id, "page": p["page"], "text": p["text"][:MAX_PAGE_CHARS]}
        for p in pages
        if p["text"].strip()
    ]
    if not data:
        return ToolResult(status="empty", data=[], note="these pages contain no extractable text")
    return ToolResult(
        status="ok",
        data=data,
        citations=_unique_citations(data),
        note=f"read {args.doc_id} pages {args.page_start}-{args.page_end}",
    )


# ---------------------------------------------------------------- ask_clarification


class AskClarificationInput(BaseModel):
    question: str = Field(
        min_length=5, max_length=300, description="One targeted question for the user."
    )
    options: list[str] = Field(
        min_length=2,
        max_length=5,
        description="2-5 short candidate answers grounded in the corpus (e.g. edition names).",
    )

    @field_validator("options")
    @classmethod
    def short_options(cls, v: list[str]) -> list[str]:
        v = [o.strip() for o in v if o.strip()]
        if len(v) < 2 or any(len(o) > 100 for o in v):
            raise ValueError("need 2-5 non-empty options of at most 100 characters")
        return v


def ask_clarification(args: AskClarificationInput) -> ToolResult:
    # Terminal: the loop stops and the session stores this as pending_clarification.
    return ToolResult(
        status="ok",
        data={"question": args.question, "options": args.options},
        note="clarification requested from user",
        terminal=True,
    )


# ---------------------------------------------------------------- registry


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    input_model: type[BaseModel]
    fn: Callable[[BaseModel], ToolResult]

    def anthropic_schema(self) -> dict:
        schema = self.input_model.model_json_schema()
        schema.pop("title", None)
        return {"name": self.name, "description": self.description, "input_schema": schema}


TOOLS: dict[str, Tool] = {
    t.name: t
    for t in [
        Tool(
            "list_documents",
            "List every document in the corpus with its id, title and page count. Use it to "
            "learn valid doc_ids, or when the user asks which documents exist.",
            ListDocumentsInput,
            list_documents,
        ),
        Tool(
            "search_documents",
            "Hybrid semantic + keyword search over all document passages, reranked by "
            "relevance. Returns passages with doc_id, page and a relevance score. Status "
            "'weak' means the best passage is a poor match: rephrase the query (synonyms, "
            "the documents' terminology), narrow with doc_ids, or conclude the evidence is "
            "missing. Search once per sub-question rather than one big query.",
            SearchDocumentsInput,
            search_documents,
        ),
        Tool(
            "read_document_section",
            f"Read the full text of 1-{MAX_PAGES_PER_READ} consecutive pages of one "
            "document. Use it when a search hit is relevant but cut off, or to check the "
            "pages next to a hit (tables and definitions often continue on the next page).",
            ReadDocumentSectionInput,
            read_document_section,
        ),
        Tool(
            "ask_clarification",
            "Ask the user ONE targeted question and stop. Use only when the question is "
            "genuinely ambiguous in a way that changes the answer (e.g. it could refer to "
            "two different editions of a guideline) and the documents cannot resolve it. "
            "Do not use it to avoid searching.",
            AskClarificationInput,
            ask_clarification,
        ),
    ]
}


def _unique_citations(items: list[dict]) -> list[Citation]:
    seen: dict[str, Citation] = {}
    for it in items:
        c = Citation(doc_id=it["doc_id"], page=it["page"])
        seen.setdefault(c.key(), c)
    return list(seen.values())

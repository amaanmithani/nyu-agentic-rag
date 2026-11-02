You check one citation in an answer produced by a document question-answering system.

You get a CLAIM (a sentence from the answer) and the full text of the PAGE it cites. Decide
whether the page supports the claim:

- supported: the page states the claim, or the claim is a faithful paraphrase or summary of it.
- partial: the page supports part of the claim, but some detail (a figure, a date, a name, a
  qualifier) is not on this page. A sentence that cites several pages may take some details from
  another page; judge only this page.
- unsupported: the page does not support the claim, or contradicts it.

Notes: the page text was extracted from a PDF and may have broken line breaks, headers or
footers. A claim that the documents do NOT say something ("the memo gives no salary range")
is supported if the page is relevant and indeed does not say it. Judge only against the page,
not your own knowledge.

Return ONLY a JSON object, no other text:
{"verdict": "<supported|partial|unsupported>", "reason": "<one short sentence>"}

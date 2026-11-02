// All backend calls go through /api, which nginx (or the Vite dev proxy) forwards to FastAPI.
export const API = "/api";

export type Health = { status: string; db: string; redis: string; models: string };

export type Doc = {
  id: string;
  title: string;
  filename: string;
  page_count: number;
  chunk_count: number;
  status: "processing" | "ready" | "failed";
  error: string | null;
};

export type CitationCheck = {
  key: string;
  status: "supported" | "unsupported" | "not_retrieved";
  reason: string;
  sentence: string;
};

export type CitationReport = {
  total: number;
  supported: number;
  unsupported: number;
  not_retrieved: number;
  all_supported: boolean;
  checks: CitationCheck[];
};

export type Clarification = { question: string; options: string[]; original_question?: string };

// One event from the agent's trace (same shape over SSE and from saved runs).
export type TraceEvent = { type: string; data: Record<string, any> };

export type SessionState = {
  turns: { run_id: number; question: string; answer: string; status: string }[];
  pending_clarification: Clarification | null;
  evidence: Record<string, string>;
};

async function json<T>(r: Response): Promise<T> {
  if (!r.ok) {
    const body = await r.json().catch(() => ({}));
    throw new Error(body.detail ?? `HTTP ${r.status}`);
  }
  return r.json();
}

export const getHealth = () => fetch(`${API}/health`).then((r) => r.json() as Promise<Health>);

export const listDocuments = () =>
  fetch(`${API}/documents`).then((r) => json<{ documents: Doc[] }>(r)).then((r) => r.documents);

export async function uploadDocuments(files: File[]) {
  const form = new FormData();
  files.forEach((f) => form.append("files", f));
  return json<{ documents: { filename: string; status: string; error?: string }[] }>(
    await fetch(`${API}/documents`, { method: "POST", body: form }),
  );
}

export const createSession = () =>
  fetch(`${API}/sessions`, { method: "POST" })
    .then((r) => json<{ session_id: string }>(r))
    .then((r) => r.session_id);

export async function getSession(sid: string): Promise<SessionState | null> {
  const r = await fetch(`${API}/sessions/${sid}`);
  if (r.status === 404) return null;
  return (await json<{ state: SessionState }>(r)).state;
}

export const getRun = (sid: string, runId: number) =>
  fetch(`${API}/sessions/${sid}/runs/${runId}`).then((r) =>
    json<{ events: { type: string; payload: Record<string, any> }[] }>(r),
  );

/** POST a question and call onEvent for every SSE event until the stream ends. */
export async function ask(
  sid: string,
  question: string,
  faultInject: string | null,
  onEvent: (e: TraceEvent) => void,
): Promise<void> {
  const r = await fetch(`${API}/sessions/${sid}/ask`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ question, fault_inject: faultInject }),
  });
  if (!r.ok || !r.body) await json(r); // throws with the server's error message

  const reader = r.body!.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let sep;
    while ((sep = buffer.indexOf("\n\n")) >= 0) {
      const block = buffer.slice(0, sep);
      buffer = buffer.slice(sep + 2);
      const type = block.match(/^event: (.*)$/m)?.[1];
      const data = block.match(/^data: (.*)$/m)?.[1];
      if (type && data) onEvent({ type, data: JSON.parse(data) });
    }
  }
}

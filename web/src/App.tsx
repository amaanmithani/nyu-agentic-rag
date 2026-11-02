import { useEffect, useState } from "react";
import {
  ask,
  createSession,
  getHealth,
  getRun,
  getSession,
  type Health,
  type TraceEvent,
} from "./api/client";
import Chat, { type Turn } from "./components/Chat";
import DocumentsPanel from "./components/DocumentsPanel";
import TracePanel from "./components/TracePanel";

const SESSION_KEY = "rag.session_id";
// Hidden demo switch, never shown in the normal UI: open /?fault=search_documents:error:9 to
// inject a tool failure into every question (the server also requires ALLOW_FAULT_INJECTION).
const DEMO_FAULT = new URLSearchParams(window.location.search).get("fault");

export default function App() {
  const [health, setHealth] = useState<Health | null>(null);
  const [sid, setSid] = useState<string | null>(null);
  const [turns, setTurns] = useState<Turn[]>([]);
  const [traces, setTraces] = useState<Record<number, TraceEvent[]>>({}); // by turn index
  const [selected, setSelected] = useState<number | null>(null);
  const [evidence, setEvidence] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Health badge, refreshed every 10 s (shows "redis degraded" during an outage).
  useEffect(() => {
    const load = () => getHealth().then(setHealth).catch(() => setHealth(null));
    load();
    const t = setInterval(load, 10_000);
    return () => clearInterval(t);
  }, []);

  // Resume the session from a previous visit (state lives server-side), else start one.
  useEffect(() => {
    (async () => {
      const saved = localStorage.getItem(SESSION_KEY);
      const state = saved ? await getSession(saved).catch(() => null) : null;
      if (saved && state) {
        setSid(saved);
        setEvidence(state.evidence);
        setTurns(
          state.turns.map((t, i, all) => ({
            runId: t.run_id,
            question: t.question,
            answer: t.answer,
            status: t.status,
            clarification: i === all.length - 1 ? state.pending_clarification : null,
          })),
        );
      } else {
        newSession();
      }
    })();
  }, []);

  async function newSession() {
    const id = await createSession();
    localStorage.setItem(SESSION_KEY, id);
    setSid(id);
    setTurns([]);
    setTraces({});
    setSelected(null);
    setEvidence({});
  }

  async function selectTurn(i: number) {
    setSelected(i);
    const runId = turns[i]?.runId;
    if (traces[i] || !sid || !runId) return;
    const run = await getRun(sid, runId); // saved trace from Postgres
    setTraces((t) => ({ ...t, [i]: run.events.map((e) => ({ type: e.type, data: e.payload })) }));
  }

  async function onAsk(question: string) {
    if (!sid) return;
    const index = turns.length;
    setTurns((t) => [...t, { question, running: true }]);
    setTraces((t) => ({ ...t, [index]: [] }));
    setSelected(index);
    setBusy(true);
    setError(null);
    const update = (patch: Partial<Turn>) =>
      setTurns((ts) => ts.map((t, i) => (i === index ? { ...t, ...patch } : t)));

    try {
      await ask(sid, question, DEMO_FAULT, (e) => {
        setTraces((t) => ({ ...t, [index]: [...(t[index] ?? []), e] }));
        if (e.type === "run_created") update({ runId: e.data.run_id });
        if (e.type === "clarification") update({ clarification: e.data as Turn["clarification"] });
        if (e.type === "run_finished")
          update({ answer: e.data.answer, status: e.data.status, report: e.data.citations, running: false });
        if (e.type === "error") update({ status: "error", answer: `Server error: ${e.data.error}`, running: false });
      });
      const state = await getSession(sid);
      if (state) setEvidence(state.evidence);
    } catch (e) {
      update({ status: "error", answer: (e as Error).message, running: false });
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  const traceEvents = selected !== null ? traces[selected] ?? [] : [];

  return (
    <div className="flex h-screen flex-col bg-slate-50 text-slate-900">
      <header className="flex items-center justify-between border-b border-slate-200 bg-white px-4 py-2">
        <div>
          <h1 className="text-base font-semibold">AI Risk & Governance Assistant</h1>
          <p className="text-[11px] text-slate-500">
            Agentic RAG over NIST AI frameworks, federal AI policy and AI governance documents · every answer cites a verified page.
          </p>
        </div>
        <div className="flex items-center gap-3 text-[11px]">
          {DEMO_FAULT && (
            <span className="rounded bg-red-100 px-2 py-0.5 font-mono text-red-800" title="Demo link: tool failures are injected">
              demo fault: {DEMO_FAULT}
            </span>
          )}
          <HealthBadge health={health} />
          <button onClick={newSession} className="rounded border border-slate-300 px-2 py-1 hover:bg-slate-100" disabled={busy}>
            New session
          </button>
        </div>
      </header>
      {error && <div className="bg-red-50 px-4 py-1 text-xs text-red-700">{error}</div>}

      <main className="grid min-h-0 flex-1 grid-cols-1 gap-4 overflow-y-auto p-4 lg:grid-cols-[260px_minmax(0,1fr)_400px] lg:overflow-hidden">
        <aside className="flex min-h-0 flex-col">
          <DocumentsPanel />
        </aside>
        <div className="flex min-h-[60vh] flex-col lg:min-h-0">
          <Chat turns={turns} selected={selected} evidence={evidence} busy={busy} onAsk={onAsk} onSelect={selectTurn} />
        </div>
        <aside className="flex min-h-0 flex-col gap-2">
          <h2 className="text-sm font-semibold text-slate-700">
            Execution trace {selected !== null && <span className="font-normal text-slate-500">· question {selected + 1}</span>}
          </h2>
          <TracePanel events={traceEvents} running={busy && selected === turns.length - 1} />
        </aside>
      </main>
    </div>
  );
}

function HealthBadge({ health }: { health: Health | null }) {
  if (!health) return <span className="rounded bg-red-100 px-2 py-0.5 text-red-800">API unreachable</span>;
  const part = (name: string, value: string) => (
    <span className={value === "ok" || value === "ready" ? "text-emerald-700" : "text-amber-700"}>
      {name}: {value}
    </span>
  );
  return (
    <span className="flex gap-2 rounded bg-slate-100 px-2 py-0.5">
      {part("db", health.db)}
      {part("redis", health.redis)}
      {part("models", health.models)}
    </span>
  );
}

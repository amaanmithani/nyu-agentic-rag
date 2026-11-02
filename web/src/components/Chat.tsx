import { useEffect, useRef, useState } from "react";
import type { CitationReport, Clarification } from "../api/client";
import Answer from "./Answer";

export type Turn = {
  runId?: number;
  question: string;
  answer?: string;
  status?: string; // answered | clarification | partial | error
  report?: CitationReport;
  clarification?: Clarification | null;
  running?: boolean;
};

const FAULTS: { label: string; value: string | null }[] = [
  { label: "No fault", value: null },
  { label: "Search times out once (retry recovers)", value: "search_documents:timeout:1" },
  { label: "Search fails every time", value: "search_documents:error:9" },
  { label: "Page reads time out", value: "read_document_section:timeout:9" },
];

type Props = {
  turns: Turn[];
  selected: number | null;
  evidence: Record<string, string>;
  busy: boolean;
  onAsk: (question: string, fault: string | null) => void;
  onSelect: (index: number) => void;
};

export default function Chat({ turns, selected, evidence, busy, onAsk, onSelect }: Props) {
  const [text, setText] = useState("");
  const [fault, setFault] = useState<string | null>(null);
  const bottom = useRef<HTMLDivElement>(null);
  const pending = turns.at(-1)?.status === "clarification" ? turns.at(-1)?.clarification : null;

  // Block body on purpose: an effect must return nothing or a cleanup function, and newer
  // browsers return a value from scrollIntoView, which React would then try to call.
  useEffect(() => {
    bottom.current?.scrollIntoView({ behavior: "smooth" });
  }, [turns]);

  const submit = (q: string) => {
    if (!q.trim() || busy) return;
    onAsk(q.trim(), fault);
    setText("");
  };

  return (
    <section className="flex min-h-0 flex-1 flex-col">
      <div className="min-h-0 flex-1 space-y-4 overflow-y-auto pb-4">
        {!turns.length && (
          <div className="mt-10 text-center text-sm text-slate-500">
            <p className="mb-3">Ask about AI risk management, federal AI policy, or generative-AI security. Try:</p>
            {[
              "How quickly must agencies designate a Chief AI Officer under the 2024 and 2025 OMB memos?",
              "Which risks does NIST say are unique to generative AI, and how does it describe prompt injection?",
              "What do I have to do before deploying my AI system?",
            ].map((q) => (
              <button
                key={q}
                onClick={() => submit(q)}
                className="mx-auto mb-1.5 block rounded-full border border-slate-300 bg-white px-3 py-1 text-xs hover:border-indigo-400"
              >
                {q}
              </button>
            ))}
          </div>
        )}

        {turns.map((t, i) => (
          <div key={i} className="space-y-2">
            <div className="flex justify-end">
              <p className="max-w-[85%] rounded-2xl rounded-br-sm bg-indigo-600 px-3 py-2 text-sm text-white">
                {t.question}
              </p>
            </div>
            <div
              onClick={() => onSelect(i)}
              className={`cursor-pointer rounded-2xl rounded-bl-sm border bg-white px-3 py-2 shadow-sm ${
                selected === i ? "border-indigo-400 ring-2 ring-indigo-100" : "border-slate-200"
              }`}
              title="Click to show this answer's trace"
            >
              {t.running && !t.answer && <p className="text-sm text-slate-500 animate-pulse">Thinking and searching…</p>}
              {t.status && t.status !== "answered" && <StatusTag status={t.status} />}
              {t.answer && t.status !== "clarification" && (
                <Answer text={t.answer} report={t.report} evidence={evidence} />
              )}
              {t.status === "clarification" && t.clarification && (
                <p className="text-sm text-slate-800">{t.clarification.question}</p>
              )}
              {t.report && t.report.total > 0 && (
                <p className="mt-2 text-[11px] text-slate-500">
                  Citations verified: {t.report.supported}/{t.report.total}
                  {t.report.unsupported > 0 && ` · ${t.report.unsupported} unsupported`}
                  {t.report.not_retrieved > 0 && ` · ${t.report.not_retrieved} not retrieved`}
                </p>
              )}
            </div>
          </div>
        ))}

        {pending && !busy && (
          <div className="flex flex-wrap gap-1.5">
            {pending.options.map((o) => (
              <button
                key={o}
                onClick={() => submit(o)}
                className="rounded-full border border-indigo-300 bg-indigo-50 px-3 py-1 text-xs text-indigo-800 hover:bg-indigo-100"
              >
                {o}
              </button>
            ))}
          </div>
        )}
        <div ref={bottom} />
      </div>

      <form
        className="flex flex-col gap-2 border-t border-slate-200 pt-3"
        onSubmit={(e) => {
          e.preventDefault();
          submit(text);
        }}
      >
        <div className="flex gap-2">
          <input
            value={text}
            onChange={(e) => setText(e.target.value)}
            placeholder={pending ? "Answer the question above, or type your own reply…" : "Ask about AI risk, policy or governance…"}
            className="flex-1 rounded-lg border border-slate-300 px-3 py-2 text-sm focus:border-indigo-500 focus:outline-none"
            disabled={busy}
          />
          <button
            disabled={busy || !text.trim()}
            className="rounded-lg bg-indigo-600 px-4 py-2 text-sm font-medium text-white disabled:opacity-40"
          >
            {busy ? "…" : "Ask"}
          </button>
        </div>
        <label className="flex items-center gap-2 text-[11px] text-slate-500">
          Demo: simulate tool failure
          <select
            value={fault ?? ""}
            onChange={(e) => setFault(e.target.value || null)}
            className="rounded border border-slate-300 bg-white px-1 py-0.5"
          >
            {FAULTS.map((f) => (
              <option key={f.label} value={f.value ?? ""}>
                {f.label}
              </option>
            ))}
          </select>
        </label>
      </form>
    </section>
  );
}

function StatusTag({ status }: { status: string }) {
  const style: Record<string, string> = {
    clarification: "bg-indigo-100 text-indigo-800",
    partial: "bg-amber-100 text-amber-800",
    error: "bg-red-100 text-red-800",
  };
  const label: Record<string, string> = {
    clarification: "Needs clarification",
    partial: "Partial answer (limit reached or tool failed)",
    error: "Error",
  };
  return (
    <span className={`mb-1.5 inline-block rounded px-1.5 py-px text-[10px] font-semibold ${style[status] ?? ""}`}>
      {label[status] ?? status}
    </span>
  );
}

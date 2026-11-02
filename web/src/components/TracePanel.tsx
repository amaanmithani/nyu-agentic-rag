import type { TraceEvent } from "../api/client";

// Execution trace for one run: every LLM decision and tool call, with inputs, status,
// result summary, citations, timing and tokens. Tool calls are paired with their results.

const STATUS_STYLE: Record<string, string> = {
  ok: "bg-emerald-100 text-emerald-800",
  weak: "bg-amber-100 text-amber-800",
  empty: "bg-slate-200 text-slate-700",
  error: "bg-red-100 text-red-800",
  timeout: "bg-red-100 text-red-800",
  running: "bg-indigo-100 text-indigo-700 animate-pulse",
};

export default function TracePanel({ events, running }: { events: TraceEvent[]; running: boolean }) {
  if (!events.length) {
    return <p className="text-sm text-slate-500">Ask a question to see how the agent searches the governance library, step by step.</p>;
  }
  const results = new Map(events.filter((e) => e.type === "tool_result").map((e) => [e.data.id, e.data]));
  const finished = events.find((e) => e.type === "run_finished")?.data;
  const started = events.find((e) => e.type === "run_started")?.data;

  return (
    <div className="flex min-h-0 flex-col gap-2">
      {started && (
        <p className="text-[11px] text-slate-500">
          limits: {started.limits.max_steps} steps · {started.limits.max_tokens.toLocaleString()} tokens ·{" "}
          {started.limits.deadline_s}s · tool timeout {started.limits.tool_timeout_s}s ×{" "}
          {started.limits.tool_max_retries + 1}
          {started.fault_inject && (
            <span className="ml-1 rounded bg-red-100 px-1 text-red-700">
              fault injection: {JSON.stringify(started.fault_inject)}
            </span>
          )}
        </p>
      )}

      <ol className="min-h-0 flex-1 space-y-1.5 overflow-y-auto pr-1">
        {events.map((e, i) => {
          if (e.type === "llm_call") return <LlmRow key={i} d={e.data} />;
          if (e.type === "tool_call") return <ToolRow key={i} call={e.data} result={results.get(e.data.id)} />;
          if (e.type === "limit_reached")
            return (
              <Banner key={i} tone="amber">
                Limit reached: {e.data.reason.replace("_", " ")}. Writing a partial answer from the evidence so far.
              </Banner>
            );
          if (e.type === "llm_error")
            return <Banner key={i} tone="red">LLM error at step {e.data.step}: {e.data.error}</Banner>;
          if (e.type === "clarification")
            return <Banner key={i} tone="indigo">Stopped to ask the user: “{e.data.question}”</Banner>;
          return null;
        })}
        {running && <li className="text-xs text-indigo-600 animate-pulse">working…</li>}
      </ol>

      {finished && (
        <div className="grid grid-cols-3 gap-1 rounded-lg bg-slate-100 p-2 text-center text-[11px] text-slate-700">
          <Stat label="status" value={`${finished.status}`} />
          <Stat label="steps / tools" value={`${finished.steps} / ${finished.tool_calls}`} />
          <Stat label="latency" value={`${(finished.latency_ms / 1000).toFixed(1)} s`} />
          <Stat label="tokens" value={finished.tokens.total.toLocaleString()} />
          <Stat label="cached in" value={finished.tokens.cache_read.toLocaleString()} />
          <Stat label="est. cost" value={`$${finished.cost_usd.toFixed(4)}`} />
          <Stat
            label="citations"
            value={`${finished.citations.supported}/${finished.citations.total} verified`}
            wide
          />
        </div>
      )}
    </div>
  );
}

function LlmRow({ d }: { d: Record<string, any> }) {
  const decision = d.tool_calls.length ? `→ ${d.tool_calls.join(", ")}` : `→ ${d.stop_reason === "end_turn" ? "answer" : d.stop_reason}`;
  return (
    <li className="flex items-center justify-between rounded border border-slate-200 bg-white px-2 py-1 text-[11px] text-slate-600">
      <span>
        <span className="font-semibold text-slate-800">LLM</span> step {d.step} {decision}
      </span>
      <span className="font-mono">
        {d.ms} ms · {d.tokens.total.toLocaleString()} tok
        {d.tokens.cache_read > 0 && <span className="text-emerald-700"> ({d.tokens.cache_read.toLocaleString()} cached)</span>}
      </span>
    </li>
  );
}

function ToolRow({ call, result }: { call: Record<string, any>; result?: Record<string, any> }) {
  const status = result?.status ?? "running";
  const { purpose, ...args } = call.args ?? {};
  return (
    <li className="rounded border border-slate-200 bg-white px-2 py-1.5 text-xs">
      <div className="flex items-center justify-between gap-2">
        <span className="font-mono font-semibold text-slate-800">{call.tool}</span>
        <span className="flex items-center gap-1">
          {result && result.attempts > 1 && (
            <span className="rounded bg-orange-100 px-1 text-[10px] text-orange-800">{result.attempts} attempts</span>
          )}
          <span className={`rounded px-1.5 py-px text-[10px] font-semibold uppercase ${STATUS_STYLE[status] ?? ""}`}>
            {status}
          </span>
        </span>
      </div>
      {purpose && <div className="mt-0.5 text-[11px] italic text-indigo-700">sub-question: {purpose}</div>}
      {Object.keys(args).length > 0 && (
        <pre className="mt-1 overflow-x-auto whitespace-pre-wrap break-all rounded bg-slate-50 px-1.5 py-1 font-mono text-[11px] text-slate-700">
          {JSON.stringify(args)}
        </pre>
      )}
      {result && (
        <div className="mt-1 text-[11px] text-slate-600">
          {result.summary.replace(/\s*\[.*\]$/, "")}
          <span className="ml-1 text-slate-400">({result.ms} ms)</span>
          {result.citations.length > 0 && (
            <div className="mt-0.5 flex flex-wrap gap-1">
              {result.citations.map((c: string) => (
                <span key={c} className="rounded bg-slate-100 px-1 font-mono text-[10px] text-slate-600">
                  {c}
                </span>
              ))}
            </div>
          )}
          {result.injection_flags?.length > 0 && (
            <div className="mt-1 rounded bg-red-50 px-1.5 py-0.5 text-[11px] text-red-700">
              ⚠ possible prompt injection in {result.injection_flags.join(", ")} (treated as untrusted data)
            </div>
          )}
        </div>
      )}
    </li>
  );
}

function Banner({ tone, children }: { tone: "amber" | "red" | "indigo"; children: React.ReactNode }) {
  const style = { amber: "bg-amber-50 text-amber-800", red: "bg-red-50 text-red-800", indigo: "bg-indigo-50 text-indigo-800" }[tone];
  return <li className={`rounded px-2 py-1 text-xs ${style}`}>{children}</li>;
}

function Stat({ label, value, wide }: { label: string; value: string; wide?: boolean }) {
  return (
    <div className={wide ? "col-span-3" : ""}>
      <div className="font-semibold text-slate-900">{value}</div>
      <div className="text-slate-500">{label}</div>
    </div>
  );
}

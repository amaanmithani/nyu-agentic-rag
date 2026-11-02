import type { ReactNode } from "react";
import type { CitationReport } from "../api/client";

// Renders the model's answer: headings, bullets, **bold**, and [doc:page] citation chips.
// Deliberately tiny and text-only (no HTML injection): the answer is LLM output.

const CITATION = /\[([a-z0-9][a-z0-9-]*:\d+(?:-\d+)?(?:\s*[,;]\s*[a-z0-9][a-z0-9-]*:\d+(?:-\d+)?)*)\]/g;

type Props = { text: string; report?: CitationReport; evidence?: Record<string, string> };

export default function Answer({ text, report, evidence }: Props) {
  const status = new Map<string, { status: string; reason: string }>();
  for (const c of report?.checks ?? []) {
    // A key cited in several sentences shows its worst status.
    const prev = status.get(c.key);
    if (!prev || prev.status === "supported") status.set(c.key, c);
  }

  const inline = (line: string): ReactNode[] => {
    const out: ReactNode[] = [];
    let last = 0;
    for (const m of line.matchAll(CITATION)) {
      out.push(...bold(line.slice(last, m.index)));
      for (const key of m[1].split(/\s*[,;]\s*/)) {
        out.push(<Chip key={`${m.index}-${key}`} citeKey={key} check={status.get(key)} snippet={evidence?.[key]} />);
      }
      last = (m.index ?? 0) + m[0].length;
    }
    out.push(...bold(line.slice(last)));
    return out;
  };

  return (
    <div className="space-y-1.5 text-sm leading-relaxed text-slate-800">
      {text.split("\n").map((raw, i) => {
        const line = raw.trimEnd();
        if (!line.trim()) return null;
        const heading = line.match(/^#{1,4}\s+(.*)/);
        if (heading) return <p key={i} className="pt-1 font-semibold">{inline(heading[1])}</p>;
        const bullet = line.match(/^(\s*)[-*•]\s+(.*)/);
        if (bullet)
          return (
            <p key={i} className="flex gap-2" style={{ paddingLeft: bullet[1].length * 8 }}>
              <span className="text-slate-400">•</span>
              <span>{inline(bullet[2])}</span>
            </p>
          );
        return <p key={i}>{inline(line)}</p>;
      })}
    </div>
  );
}

function bold(s: string): ReactNode[] {
  return s.split(/(\*\*[^*]+\*\*)/g).map((part, i) =>
    part.startsWith("**") && part.endsWith("**") ? <strong key={i}>{part.slice(2, -2)}</strong> : part,
  );
}

function Chip({
  citeKey,
  check,
  snippet,
}: {
  citeKey: string;
  check?: { status: string; reason: string };
  snippet?: string;
}) {
  const style =
    check?.status === "supported"
      ? "bg-emerald-50 text-emerald-800 ring-emerald-200"
      : check?.status === "unsupported"
        ? "bg-amber-50 text-amber-800 ring-amber-300"
        : check?.status === "not_retrieved"
          ? "bg-red-50 text-red-700 ring-red-300"
          : "bg-slate-50 text-slate-700 ring-slate-200";
  const label = check ? `${check.status}: ${check.reason}` : "not verified";
  return (
    <span
      className={`mx-0.5 inline-block cursor-help rounded px-1.5 py-px align-baseline font-mono text-[11px] ring-1 ${style}`}
      title={`${citeKey}\n${label}${snippet ? `\n\n“${snippet}…”` : ""}`}
    >
      {citeKey}
      {check && check.status !== "supported" && " ⚠"}
    </span>
  );
}

import { useEffect, useRef, useState } from "react";
import { listDocuments, uploadDocuments, type Doc } from "../api/client";

export default function DocumentsPanel() {
  const [docs, setDocs] = useState<Doc[]>([]);
  const [message, setMessage] = useState<string | null>(null);
  const [uploading, setUploading] = useState(false);
  const input = useRef<HTMLInputElement>(null);

  const refresh = () => listDocuments().then(setDocs).catch(() => setDocs([]));

  useEffect(() => {
    refresh();
  }, []);

  // Poll while anything is still being ingested.
  useEffect(() => {
    if (!docs.some((d) => d.status === "processing")) return;
    const t = setInterval(refresh, 2000);
    return () => clearInterval(t);
  }, [docs]);

  async function onFiles(files: FileList | null) {
    if (!files?.length) return;
    setUploading(true);
    setMessage(null);
    try {
      const res = await uploadDocuments(Array.from(files));
      const rejected = res.documents.filter((d) => d.status === "rejected");
      const dupes = res.documents.filter((d: any) => d.duplicate);
      setMessage(
        [
          `${res.documents.length - rejected.length - dupes.length} uploaded`,
          dupes.length ? `${dupes.length} already in corpus` : "",
          ...rejected.map((r) => r.error),
        ]
          .filter(Boolean)
          .join(" · "),
      );
      refresh();
    } catch (e) {
      setMessage(`Upload failed: ${(e as Error).message}`);
    } finally {
      setUploading(false);
      if (input.current) input.current.value = "";
    }
  }

  const pages = docs.reduce((n, d) => n + d.page_count, 0);

  return (
    <section className="flex min-h-0 flex-col gap-3">
      <div className="flex items-baseline justify-between">
        <h2 className="text-sm font-semibold text-slate-700">Governance library</h2>
        <span className="text-xs text-slate-500">
          {docs.length} docs · {pages} pages
        </span>
      </div>

      <label
        className="cursor-pointer rounded-lg border-2 border-dashed border-slate-300 p-3 text-center text-sm text-slate-600 hover:border-indigo-400 hover:bg-indigo-50"
        onDragOver={(e) => e.preventDefault()}
        onDrop={(e) => {
          e.preventDefault();
          onFiles(e.dataTransfer.files);
        }}
      >
        {uploading ? "Uploading…" : "Drop policy or framework PDFs here, or click to upload"}
        <input
          ref={input}
          type="file"
          accept="application/pdf"
          multiple
          className="hidden"
          onChange={(e) => onFiles(e.target.files)}
        />
      </label>
      {message && <p className="text-xs text-slate-600">{message}</p>}

      <ul className="min-h-0 flex-1 space-y-1 overflow-y-auto">
        {docs.map((d) => (
          <li key={d.id} className="rounded-md bg-white px-2 py-1.5 text-xs shadow-sm" title={d.error ?? d.filename}>
            <div className="flex items-center justify-between gap-2">
              <span className="truncate font-medium text-slate-800">{d.title}</span>
              <StatusDot status={d.status} />
            </div>
            <div className="font-mono text-[11px] text-slate-500">
              {d.id} · {d.page_count || "?"} p · {d.chunk_count} chunks
            </div>
          </li>
        ))}
      </ul>
    </section>
  );
}

function StatusDot({ status }: { status: Doc["status"] }) {
  const color = { ready: "bg-emerald-500", processing: "bg-amber-400 animate-pulse", failed: "bg-red-500" }[
    status
  ];
  return <span className={`h-2 w-2 shrink-0 rounded-full ${color}`} title={status} />;
}

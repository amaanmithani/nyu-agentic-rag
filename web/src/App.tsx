import { useEffect, useState } from "react";
import { getHealth, type Health } from "./api/client";

export default function App() {
  const [health, setHealth] = useState<Health | null>(null);

  useEffect(() => {
    getHealth().then(setHealth).catch(() => setHealth(null));
  }, []);

  return (
    <div className="min-h-screen bg-slate-50 p-6 text-slate-900">
      <h1 className="text-xl font-semibold">Agentic RAG</h1>
      <p className="mt-2 text-sm text-slate-600">
        Backend: {health ? `${health.status} (db ${health.db}, redis ${health.redis})` : "unreachable"}
      </p>
    </div>
  );
}

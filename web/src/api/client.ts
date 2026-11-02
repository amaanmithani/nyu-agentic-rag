// All backend calls go through /api, which nginx (or the Vite dev proxy) forwards to FastAPI.
export const API = "/api";

export type Health = { status: string; db: string; redis: string };

export async function getHealth(): Promise<Health> {
  const r = await fetch(`${API}/health`);
  return r.json();
}

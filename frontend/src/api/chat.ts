const API_BASE = import.meta.env.VITE_API_URL ?? "";

export interface ChatResponse {
  response: string;
  sources: Array<{ doc: string; page: number; distance: number }>;
}

export async function sendChatMessage(query: string): Promise<ChatResponse> {
  const res = await fetch(`${API_BASE}/api/chat`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ query }),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ error: "Unknown error" }));
    throw new Error(err.error ?? `HTTP ${res.status}`);
  }
  return res.json();
}

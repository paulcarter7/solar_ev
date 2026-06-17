import React, { useEffect, useRef, useState } from "react";
import { sendChatMessage } from "../api/chat";

interface Message {
  role: "user" | "assistant";
  text: string;
  sources?: Array<{ doc: string; page: number; distance: number }>;
}

const EXAMPLE_QUERIES = [
  "Am I on the optimal PG&E rate plan given my usage?",
  "How does NEM 3.0 affect my solar export credits?",
  "What is my battery's maximum discharge rate?",
];

export default function ChatPanel() {
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const bottomRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, loading]);

  async function submit(query: string) {
    const q = query.trim();
    if (!q || loading) return;

    setInput("");
    setError(null);
    setMessages((prev) => [...prev, { role: "user", text: q }]);
    setLoading(true);

    try {
      const result = await sendChatMessage(q);
      setMessages((prev) => [
        ...prev,
        { role: "assistant", text: result.response, sources: result.sources },
      ]);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Something went wrong");
    } finally {
      setLoading(false);
    }
  }

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    submit(input);
  }

  const formatSources = (sources: Message["sources"]) => {
    const seen = new Set<string>();
    return (sources ?? [])
      .filter((s) => { const k = `${s.doc}:${s.page}`; return seen.has(k) ? false : (seen.add(k), true); })
      .map((s) => `${s.doc} p.${s.page}`)
      .join(", ");
  };

  return (
    <div className="rounded-2xl bg-gray-800 border border-gray-700 p-5 flex flex-col h-[28rem]">
      <h2 className="text-sm font-semibold text-gray-200 mb-3">
        Ask about your energy docs
      </h2>

      <div className="flex-1 overflow-y-auto space-y-3 mb-3 pr-1">
        {messages.length === 0 && (
          <div className="space-y-2 mt-2">
            <p className="text-xs text-gray-500">Try asking:</p>
            {EXAMPLE_QUERIES.map((q) => (
              <button
                key={q}
                onClick={() => submit(q)}
                className="block w-full text-left text-xs text-gray-400 hover:text-gray-200 bg-gray-700/50 hover:bg-gray-700 px-3 py-2 rounded-lg transition-colors"
              >
                {q}
              </button>
            ))}
          </div>
        )}

        {messages.map((msg, i) => (
          <div
            key={i}
            className={`flex ${msg.role === "user" ? "justify-end" : "justify-start"}`}
          >
            <div
              className={`max-w-[85%] rounded-xl px-3 py-2 text-sm ${
                msg.role === "user"
                  ? "bg-blue-600 text-white"
                  : "bg-gray-700 text-gray-100"
              }`}
            >
              <p className="whitespace-pre-wrap leading-relaxed">{msg.text}</p>
              {msg.sources && msg.sources.length > 0 && (
                <p className="text-xs mt-1.5 opacity-50">
                  Source: {formatSources(msg.sources)}
                </p>
              )}
            </div>
          </div>
        ))}

        {loading && (
          <div className="flex justify-start">
            <div className="bg-gray-700 rounded-xl px-3 py-2 text-sm text-gray-400 animate-pulse">
              Thinking…
            </div>
          </div>
        )}

        <div ref={bottomRef} />
      </div>

      {error && (
        <p className="text-xs text-red-400 mb-2 px-1">{error}</p>
      )}

      <form onSubmit={handleSubmit} className="flex gap-2">
        <input
          type="text"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          placeholder="Ask about your rate plan, battery, NEM 3.0…"
          disabled={loading}
          className="flex-1 text-sm bg-gray-700 border border-gray-600 text-gray-100 placeholder-gray-500
            rounded-xl px-3 py-2 focus:outline-none focus:ring-2 focus:ring-blue-500 disabled:opacity-50"
        />
        <button
          type="submit"
          disabled={loading || !input.trim()}
          className="px-4 py-2 bg-blue-600 text-white text-sm rounded-xl hover:bg-blue-700
            disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
        >
          Send
        </button>
      </form>
    </div>
  );
}

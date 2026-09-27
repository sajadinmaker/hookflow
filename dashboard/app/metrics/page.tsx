import { getMetrics } from "@/lib/hookflow";

export const dynamic = "force-dynamic";

/** Parse Prometheus text exposition into rows we can render. */
function parse(text: string) {
  const rows: { name: string; help?: string; value: number }[] = [];
  const help: Record<string, string> = {};
  for (const line of text.split("\n")) {
    if (line.startsWith("# HELP ")) {
      const [, , name, ...rest] = line.split(" ");
      help[name] = rest.join(" ");
    }
    if (line.startsWith("#") || !line.trim()) continue;
    const [name, value] = line.trim().split(/\s+/);
    rows.push({ name, help: help[name], value: Number(value) });
  }
  return rows;
}

export default async function MetricsPage() {
  let text: string;
  try {
    const res = await getMetrics();
    if (!res.ok) throw new Error(`metrics endpoint returned ${res.status}`);
    text = await res.text();
  } catch (error) {
    return (
      <div className="space-y-3">
        <h1 className="text-xl font-medium">Metrics</h1>
        <p className="text-sm text-rose-400">
          {error instanceof Error ? error.message : String(error)}
        </p>
      </div>
    );
  }

  const rows = parse(text);

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-medium">Metrics</h1>
        <p className="mt-1 text-sm text-neutral-500">
          Prometheus text exposition from <code className="font-mono">/metrics</code>.
          Scrape this endpoint directly for alerting; this view is for reading.
        </p>
      </div>

      {rows.length === 0 ? (
        <p className="text-sm text-neutral-500">No metrics reported yet.</p>
      ) : (
        <div className="space-y-3">
          {rows.map((m) => (
            <div
              key={m.name}
              className="rounded-lg border border-neutral-800 bg-neutral-900/40 p-4"
            >
              <div className="flex items-baseline justify-between gap-4">
                <span className="font-mono text-sm text-neutral-200">{m.name}</span>
                <span className="font-mono text-lg text-neutral-100">{m.value}</span>
              </div>
              {m.help ? (
                <p className="mt-1 text-xs text-neutral-500">{m.help}</p>
              ) : null}
            </div>
          ))}
        </div>
      )}

      <details className="rounded-lg border border-neutral-800 p-4">
        <summary className="cursor-pointer font-mono text-xs text-neutral-400">
          raw exposition
        </summary>
        <pre className="mt-3 overflow-x-auto font-mono text-xs text-neutral-400">
          {text}
        </pre>
      </details>
    </div>
  );
}

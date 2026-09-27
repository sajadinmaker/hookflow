import { getDeliveries, getEndpoints, getReady, getStats } from "@/lib/hookflow";
import { ErrorNote, Stat, statusTone, Table } from "@/components/ui";

export const dynamic = "force-dynamic";

export default async function OverviewPage() {
  try {
    const [stats, endpoints, recent, ready] = await Promise.all([
      getStats(),
      getEndpoints(),
      getDeliveries(),
      getReady(),
    ]);

    const byStatus = stats.by_status ?? {};
    const failedLatencies = recent.items
      .filter((d) => d.latency_ms != null)
      .map((d) => d.latency_ms as number)
      .sort((a, b) => a - b);
    const p95 =
      failedLatencies.length > 0
        ? failedLatencies[Math.floor(failedLatencies.length * 0.95)]
        : null;

    return (
      <div className="space-y-8">
        <div>
          <h1 className="text-xl font-medium">Overview</h1>
          <p className="mt-1 text-sm text-neutral-500">
            Live delivery state for the authenticated tenant.
          </p>
        </div>

        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          <Stat
            label="Queue depth"
            value={stats.queue_depth}
            tone={stats.queue_depth > 100 ? "warn" : "neutral"}
            hint="queued + retrying"
          />
          <Stat
            label="Succeeded"
            value={byStatus.success ?? 0}
            tone="good"
          />
          <Stat
            label="Retrying"
            value={byStatus.retrying ?? 0}
            tone={byStatus.retrying ? "warn" : "neutral"}
          />
          <Stat
            label="Dead lettered"
            value={byStatus.dlq ?? 0}
            tone={byStatus.dlq ? "bad" : "good"}
            hint="needs replay"
          />
        </div>

        <div className="grid gap-3 sm:grid-cols-3">
          <Stat label="Endpoints" value={endpoints.items.length} />
          <Stat
            label="p95 delivery latency"
            value={p95 == null ? "—" : `${p95}ms`}
            hint="last 100 deliveries"
          />
          <Stat
            label="Redis"
            value={stats.redis}
            tone={stats.redis === "configured" ? "good" : "neutral"}
            hint="optional wake-up hint"
          />
        </div>

        <section className="space-y-3">
          <h2 className="text-sm uppercase tracking-wide text-neutral-500">
            Readiness
          </h2>
          <Table head={["check", "status"]}>
            {Object.entries(ready).map(([k, v]) => (
              <tr key={k}>
                <td className="px-4 py-2 font-mono text-xs text-neutral-400">
                  {k}
                </td>
                <td
                  className={`px-4 py-2 font-mono text-xs ${
                    v === "ok" ? "text-emerald-400" : "text-amber-400"
                  }`}
                >
                  {v}
                </td>
              </tr>
            ))}
          </Table>
        </section>

        <section className="space-y-3">
          <h2 className="text-sm uppercase tracking-wide text-neutral-500">
            Recent deliveries
          </h2>
          <Table head={["id", "status", "attempts", "latency", "last error"]}>
            {recent.items.slice(0, 15).map((d) => (
              <tr key={d.id}>
                <td className="px-4 py-2 font-mono text-xs">{d.id.slice(0, 12)}</td>
                <td
                  className={`px-4 py-2 font-mono text-xs ${statusTone(d.status)}`}
                >
                  {d.status}
                </td>
                <td className="px-4 py-2 font-mono text-xs">{d.attempts}</td>
                <td className="px-4 py-2 font-mono text-xs">
                  {d.latency_ms == null ? "—" : `${d.latency_ms}ms`}
                </td>
                <td className="max-w-xs truncate px-4 py-2 font-mono text-xs text-neutral-500">
                  {d.last_error ?? "—"}
                </td>
              </tr>
            ))}
          </Table>
        </section>
      </div>
    );
  } catch (error) {
    return <ErrorNote error={error} />;
  }
}

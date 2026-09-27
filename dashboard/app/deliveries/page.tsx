import { getDeliveries } from "@/lib/hookflow";
import { ReplayButton } from "@/components/ReplayButton";
import { ErrorNote, Table, statusTone } from "@/components/ui";

export const dynamic = "force-dynamic";

export default async function DeliveriesPage({
  searchParams,
}: {
  searchParams: Promise<{ status?: string }>;
}) {
  const { status } = await searchParams;
  const filter = status ?? "";

  try {
    const { items } = await getDeliveries(filter || undefined);
    const tabs = [
      { key: "", label: "all" },
      { key: "queued", label: "queued" },
      { key: "retrying", label: "retrying" },
      { key: "success", label: "success" },
    ];

    return (
      <div className="space-y-6">
        <div>
          <h1 className="text-xl font-medium">Deliveries</h1>
          <p className="mt-1 text-sm text-neutral-500">
            Replay resets the attempt counter and re-queues the delivery.
            Successful deliveries are refused, because replaying one would
            duplicate a side effect at the receiver.
          </p>
        </div>

        <div className="flex gap-2">
          {tabs.map((t) => (
            <a
              key={t.key}
              href={t.key ? `/deliveries?status=${t.key}` : "/deliveries"}
              className={`rounded border px-3 py-1 font-mono text-xs transition-colors ${
                filter === t.key
                  ? "border-neutral-500 text-neutral-100"
                  : "border-neutral-800 text-neutral-500 hover:text-neutral-300"
              }`}
            >
              {t.label}
            </a>
          ))}
        </div>

        <Table
          head={["id", "status", "attempts", "next attempt", "code", "latency", "error", ""]}
        >
          {items.map((d) => (
            <tr key={d.id}>
              <td className="px-4 py-2 font-mono text-xs">{d.id.slice(0, 12)}</td>
              <td className={`px-4 py-2 font-mono text-xs ${statusTone(d.status)}`}>
                {d.status}
              </td>
              <td className="px-4 py-2 font-mono text-xs">{d.attempts}</td>
              <td className="px-4 py-2 font-mono text-xs text-neutral-500">
                {d.next_attempt_at
                  ? new Date(d.next_attempt_at).toISOString().slice(5, 19).replace("T", " ")
                  : "—"}
              </td>
              <td className="px-4 py-2 font-mono text-xs">
                {d.last_status_code ?? "—"}
              </td>
              <td className="px-4 py-2 font-mono text-xs">
                {d.latency_ms == null ? "—" : `${d.latency_ms}ms`}
              </td>
              <td className="max-w-xs truncate px-4 py-2 font-mono text-xs text-neutral-500">
                {d.last_error ?? "—"}
              </td>
              <td className="px-4 py-2">
                <ReplayButton
                  deliveryId={d.id}
                  disabled={d.status === "success"}
                />
              </td>
            </tr>
          ))}
        </Table>
        {items.length === 0 ? (
          <p className="text-sm text-neutral-500">No deliveries match this filter.</p>
        ) : null}
      </div>
    );
  } catch (error) {
    return <ErrorNote error={error} />;
  }
}

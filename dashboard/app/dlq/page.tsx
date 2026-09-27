import { getDeliveries } from "@/lib/hookflow";
import { ReplayButton } from "@/components/ReplayButton";
import { ErrorNote, Table } from "@/components/ui";

export const dynamic = "force-dynamic";

export default async function DlqPage() {
  try {
    const { items } = await getDeliveries("dlq");
    return (
      <div className="space-y-6">
        <div>
          <h1 className="text-xl font-medium">Dead letter queue</h1>
          <p className="mt-1 text-sm text-neutral-500">
            Deliveries that exhausted the retry schedule. Nothing is dropped
            automatically: they wait here until replayed or investigated.
          </p>
        </div>

        {items.length === 0 ? (
          <p className="text-sm text-neutral-500">
            The dead letter queue is empty.
          </p>
        ) : (
          <Table head={["id", "endpoint", "attempts", "code", "error", ""]}>
            {items.map((d) => (
              <tr key={d.id}>
                <td className="px-4 py-2 font-mono text-xs">{d.id.slice(0, 12)}</td>
                <td className="px-4 py-2 font-mono text-xs">
                  {d.endpoint_id.slice(0, 12)}
                </td>
                <td className="px-4 py-2 font-mono text-xs">{d.attempts}</td>
                <td className="px-4 py-2 font-mono text-xs text-rose-400">
                  {d.last_status_code ?? "—"}
                </td>
                <td className="max-w-sm truncate px-4 py-2 font-mono text-xs text-neutral-500">
                  {d.last_error ?? "—"}
                </td>
                <td className="px-4 py-2">
                  <ReplayButton deliveryId={d.id} />
                </td>
              </tr>
            ))}
          </Table>
        )}
      </div>
    );
  } catch (error) {
    return <ErrorNote error={error} />;
  }
}

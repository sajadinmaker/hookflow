import { getEndpoints } from "@/lib/hookflow";
import { ErrorNote, Table } from "@/components/ui";

export const dynamic = "force-dynamic";

export default async function EndpointsPage() {
  try {
    const { items } = await getEndpoints();
    return (
      <div className="space-y-6">
        <div>
          <h1 className="text-xl font-medium">Endpoints</h1>
          <p className="mt-1 text-sm text-neutral-500">
            Destinations registered by this tenant. Secrets are encrypted at rest
            and are never returned after creation.
          </p>
        </div>
        <Table head={["id", "url", "rate limit", "created"]}>
          {items.map((e) => (
            <tr key={e.id}>
              <td className="px-4 py-2 font-mono text-xs">{e.id.slice(0, 12)}</td>
              <td className="px-4 py-2 font-mono text-xs text-neutral-300">
                {e.url}
              </td>
              <td className="px-4 py-2 font-mono text-xs">
                {e.rate_limit_per_minute}/min
              </td>
              <td className="px-4 py-2 font-mono text-xs text-neutral-500">
                {new Date(e.created_at).toISOString().slice(0, 19).replace("T", " ")}
              </td>
            </tr>
          ))}
        </Table>
      </div>
    );
  } catch (error) {
    return <ErrorNote error={error} />;
  }
}

import { getApiKeys } from "@/lib/hookflow";
import { ErrorNote, Table } from "@/components/ui";

export const dynamic = "force-dynamic";

export default async function KeysPage() {
  const tenantId = process.env.HOOKFLOW_TENANT_ID;

  if (!tenantId) {
    return (
      <div className="space-y-3">
        <h1 className="text-xl font-medium">API keys</h1>
        <p className="text-sm text-neutral-500">
          Set <code className="font-mono text-neutral-300">HOOKFLOW_TENANT_ID</code>{" "}
          and <code className="font-mono text-neutral-300">HOOKFLOW_ADMIN_TOKEN</code>{" "}
          in <code className="font-mono text-neutral-300">dashboard/.env.local</code>{" "}
          to list this tenant&apos;s keys.
        </p>
      </div>
    );
  }

  try {
    const keys = await getApiKeys(tenantId);
    if (!keys) {
      return (
        <p className="text-sm text-neutral-500">
          Admin access is not configured, so keys cannot be listed. The API
          refuses admin routes with 503 when no admin token is set.
        </p>
      );
    }

    return (
      <div className="space-y-6">
        <div>
          <h1 className="text-xl font-medium">API keys</h1>
          <p className="mt-1 text-sm text-neutral-500">
            Only a SHA-256 digest is stored. The plaintext key is shown once, at
            creation, and cannot be recovered.
          </p>
        </div>
        <Table head={["prefix", "label", "created", "last used", "state"]}>
          {keys.map((k) => (
            <tr key={k.id}>
              <td className="px-4 py-2 font-mono text-xs">
                hf_{k.key_prefix}_…
              </td>
              <td className="px-4 py-2 text-xs text-neutral-300">
                {k.label || "—"}
              </td>
              <td className="px-4 py-2 font-mono text-xs text-neutral-500">
                {new Date(k.created_at).toISOString().slice(0, 10)}
              </td>
              <td className="px-4 py-2 font-mono text-xs text-neutral-500">
                {k.last_used_at
                  ? new Date(k.last_used_at).toISOString().slice(0, 19).replace("T", " ")
                  : "never"}
              </td>
              <td
                className={`px-4 py-2 font-mono text-xs ${
                  k.revoked_at ? "text-rose-400" : "text-emerald-400"
                }`}
              >
                {k.revoked_at ? "revoked" : "active"}
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

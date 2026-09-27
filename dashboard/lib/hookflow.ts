/**
 * Typed client for the HookFlow API.
 *
 * The API key is read from the server environment and attached here, so it
 * stays in the Node process. Nothing in this module may be imported into a
 * client component: that would inline the key into the browser bundle, where
 * anyone can read it from devtools. Every page and action that needs it is a
 * server component or a server action.
 */

const BASE = process.env.HOOKFLOW_API_URL ?? "http://localhost:8000";

export class HookflowError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
    this.name = "HookflowError";
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const apiKey = process.env.HOOKFLOW_API_KEY;
  if (!apiKey) {
    throw new HookflowError(
      "HOOKFLOW_API_KEY is not set. The dashboard runs server-side only; " +
        "set it in dashboard/.env.local and restart.",
      500,
    );
  }

  const res = await fetch(`${BASE}${path}`, {
    ...init,
    // Server components must not cache operational data: queue depth and DLQ
    // counts are the entire point of this view.
    cache: "no-store",
    headers: {
      Authorization: `Bearer ${apiKey}`,
      "Content-Type": "application/json",
      ...(init?.headers ?? {}),
    },
  });

  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = body?.detail ?? detail;
    } catch {
      /* non-JSON error body; keep statusText */
    }
    throw new HookflowError(`${res.status} ${detail}`, res.status);
  }

  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

export type Endpoint = {
  id: string;
  url: string;
  rate_limit_per_minute: number;
  created_at: string;
};

export type Delivery = {
  id: string;
  event_id: string;
  endpoint_id: string;
  status: "queued" | "success" | "retrying" | "dlq";
  attempts: number;
  next_attempt_at: string | null;
  last_status_code: number | null;
  last_error: string | null;
  latency_ms: number | null;
};

export type Stats = {
  by_status: Record<string, number>;
  queue_depth: number;
  redis: string;
};

export type ApiKey = {
  id: string;
  tenant_id: string;
  label: string;
  key_prefix: string;
  created_at: string;
  last_used_at: string | null;
  revoked_at: string | null;
};

export const getEndpoints = () =>
  request<{ items: Endpoint[] }>("/v1/endpoints?limit=200");

export const getDeliveries = (status?: string) => {
  const q = new URLSearchParams({ limit: "100" });
  if (status) q.set("status", status);
  return request<{ items: Delivery[] }>(`/v1/deliveries?${q.toString()}`);
};

export const getStats = () => request<Stats>("/v1/stats");

export const getReady = () =>
  request<Record<string, string>>("/ready").catch((e) => ({ error: String(e) }));

export const getMetrics = () => fetch(`${BASE}/metrics`, { cache: "no-store" });

export const getApiKeys = async (tenantId: string) => {
  const adminToken = process.env.HOOKFLOW_ADMIN_TOKEN;
  if (!adminToken) return null;
  const res = await fetch(`${BASE}/v1/admin/tenants/${tenantId}/keys`, {
    cache: "no-store",
    headers: { "X-Admin-Token": adminToken },
  });
  if (!res.ok) return null;
  return (await res.json()) as ApiKey[];
};

export const requeue = (deliveryId: string) =>
  request<Delivery>(`/v1/deliveries/${deliveryId}/requeue`, { method: "POST" });

"use server";

import { revalidatePath } from "next/cache";
import { requeue } from "@/lib/hookflow";

/**
 * Replay a delivery.
 *
 * A server action, not a client fetch, so the API key is never serialised into
 * the browser. The UI submits this and the affected views are revalidated.
 */
export async function replayDelivery(deliveryId: string) {
  try {
    await requeue(deliveryId);
    revalidatePath("/");
    revalidatePath("/deliveries");
    revalidatePath("/dlq");
    return { ok: true };
  } catch (error) {
    return {
      ok: false,
      error: error instanceof Error ? error.message : String(error),
    };
  }
}

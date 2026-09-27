"use client";

import { useState, useTransition } from "react";
import { replayDelivery } from "@/app/actions";

export function ReplayButton({
  deliveryId,
  disabled,
}: {
  deliveryId: string;
  disabled?: boolean;
}) {
  const [pending, startTransition] = useTransition();
  const [error, setError] = useState<string | null>(null);

  return (
    <span className="inline-flex items-center gap-2">
      <button
        disabled={disabled || pending}
        onClick={() =>
          startTransition(async () => {
            setError(null);
            const result = await replayDelivery(deliveryId);
            if (!result.ok) setError(result.error ?? "replay failed");
          })
        }
        className="rounded border border-neutral-700 px-2 py-1 font-mono text-xs text-neutral-300 transition-colors hover:border-neutral-500 hover:text-neutral-100 disabled:cursor-not-allowed disabled:opacity-40"
      >
        {pending ? "replaying…" : "replay"}
      </button>
      {error ? (
        <span className="font-mono text-xs text-rose-400">{error}</span>
      ) : null}
    </span>
  );
}

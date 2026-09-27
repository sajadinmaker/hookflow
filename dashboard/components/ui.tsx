export function Stat({
  label,
  value,
  tone = "neutral",
  hint,
}: {
  label: string;
  value: string | number;
  tone?: "neutral" | "good" | "warn" | "bad";
  hint?: string;
}) {
  const toneClass = {
    neutral: "text-neutral-100",
    good: "text-emerald-400",
    warn: "text-amber-400",
    bad: "text-rose-400",
  }[tone];

  return (
    <div className="rounded-lg border border-neutral-800 bg-neutral-900/40 p-4">
      <div className="text-xs uppercase tracking-wide text-neutral-500">
        {label}
      </div>
      <div className={`mt-1 font-mono text-2xl ${toneClass}`}>{value}</div>
      {hint ? <div className="mt-1 text-xs text-neutral-500">{hint}</div> : null}
    </div>
  );
}

export function statusTone(status: string) {
  switch (status) {
    case "success":
      return "text-emerald-400";
    case "retrying":
      return "text-amber-400";
    case "dlq":
      return "text-rose-400";
    default:
      return "text-neutral-300";
  }
}

export function Table({
  head,
  children,
}: {
  head: string[];
  children: React.ReactNode;
}) {
  return (
    <div className="overflow-x-auto rounded-lg border border-neutral-800">
      <table className="w-full text-left text-sm">
        <thead className="bg-neutral-900/60 text-xs uppercase tracking-wide text-neutral-500">
          <tr>
            {head.map((h) => (
              <th key={h} className="px-4 py-2 font-medium">
                {h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody className="divide-y divide-neutral-800">{children}</tbody>
      </table>
    </div>
  );
}

export function ErrorNote({ error }: { error: unknown }) {
  return (
    <div className="rounded-lg border border-rose-900 bg-rose-950/40 p-4 text-sm text-rose-300">
      <div className="font-medium">Could not reach the HookFlow API</div>
      <pre className="mt-2 overflow-x-auto whitespace-pre-wrap font-mono text-xs text-rose-200/80">
        {error instanceof Error ? error.message : String(error)}
      </pre>
      <p className="mt-2 text-xs text-rose-200/70">
        Is the API running, and is HOOKFLOW_API_KEY set in dashboard/.env.local?
      </p>
    </div>
  );
}

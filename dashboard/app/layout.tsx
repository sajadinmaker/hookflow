import Link from "next/link";

const nav = [
  { href: "/", label: "Overview" },
  { href: "/endpoints", label: "Endpoints" },
  { href: "/deliveries", label: "Deliveries" },
  { href: "/dlq", label: "DLQ" },
  { href: "/keys", label: "API keys" },
  { href: "/metrics", label: "Metrics" },
];

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en">
      <body className="min-h-screen bg-neutral-950 text-neutral-200 antialiased">
        <header className="border-b border-neutral-800">
          <div className="mx-auto flex max-w-6xl flex-wrap items-center gap-x-6 gap-y-2 px-6 py-4">
            <Link href="/" className="font-mono text-sm text-neutral-100">
              hookflow
            </Link>
            <nav className="flex flex-wrap gap-4 text-sm">
              {nav.map((item) => (
                <Link
                  key={item.href}
                  href={item.href}
                  className="text-neutral-400 transition-colors hover:text-neutral-100"
                >
                  {item.label}
                </Link>
              ))}
            </nav>
          </div>
        </header>
        <main className="mx-auto max-w-6xl px-6 py-8">{children}</main>
      </body>
    </html>
  );
}

import type { Metadata } from "next";
import Link from "next/link";

import "./globals.css";

export const metadata: Metadata = {
  title: "The Ledger Command Center",
  description: "A streamlined operator interface for ledger timelines, audit evidence, and human review."
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" suppressHydrationWarning>
      <body suppressHydrationWarning>
        <div className="page-shell">
          <header className="site-header">
            <Link href="/" className="brand-mark">
              <span className="brand-chip">TL</span>
              <div>
                <strong>The Ledger</strong>
                <span>Command Center</span>
              </div>
            </Link>

            <div className="header-copy">
              <p>Audit-ready application operations.</p>
            </div>
          </header>

          <main>{children}</main>
        </div>
      </body>
    </html>
  );
}

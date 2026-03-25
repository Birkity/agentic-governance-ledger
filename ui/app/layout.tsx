import type { Metadata } from "next";
import Link from "next/link";

import { PrimaryNav } from "../components/PrimaryNav";
import "./globals.css";

export const metadata: Metadata = {
  title: "The Ledger Review Workspace",
  description: "A workspace for reviewing application state, evidence, and operational signals from the ledger."
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" suppressHydrationWarning>
      <body suppressHydrationWarning>
        <div className="page-shell">
          <header className="site-header">
            <div className="site-header-main">
              <Link href="/" className="brand-mark">
                <span className="brand-chip">TL</span>
                <div>
                  <strong>The Ledger</strong>
                  <span>Review Workspace</span>
                </div>
              </Link>

              <PrimaryNav />
            </div>

            <div className="header-copy">
              <p>Application state, evidence, and review signals in one auditable workspace.</p>
            </div>
          </header>

          <main>{children}</main>
        </div>
      </body>
    </html>
  );
}

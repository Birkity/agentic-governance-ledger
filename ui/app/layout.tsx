import type { Metadata } from "next";
import Link from "next/link";

import { PrimaryNav } from "../components/PrimaryNav";
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
            <div className="site-header-main">
              <Link href="/" className="brand-mark">
                <span className="brand-chip">TL</span>
                <div>
                  <strong>The Ledger</strong>
                  <span>Command Center</span>
                </div>
              </Link>

              <PrimaryNav />
            </div>

            <div className="header-copy">
              <p>Durable lending operations, evidence, and oversight.</p>
            </div>
          </header>

          <main>{children}</main>
        </div>
      </body>
    </html>
  );
}

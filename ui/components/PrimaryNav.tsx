"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

const NAV_ITEMS = [
  { href: "/", label: "Overview", match: (pathname: string) => pathname === "/" },
  {
    href: "/queues/open",
    label: "Queues",
    match: (pathname: string) => pathname.startsWith("/queues") || pathname.startsWith("/applications")
  },
  { href: "/operations", label: "Operations", match: (pathname: string) => pathname.startsWith("/operations") }
];

export function PrimaryNav() {
  const pathname = usePathname();

  return (
    <nav className="primary-nav" aria-label="Primary navigation">
      {NAV_ITEMS.map((item) => {
        const active = item.match(pathname);
        return (
          <Link key={item.href} href={item.href} className={`primary-nav-link ${active ? "primary-nav-link-active" : ""}`}>
            {item.label}
          </Link>
        );
      })}
    </nav>
  );
}

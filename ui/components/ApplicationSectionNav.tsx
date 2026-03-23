"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

interface ApplicationSectionNavProps {
  applicationId: string;
}

const NAV_ITEMS = [
  { key: "overview", label: "Overview", href: (applicationId: string) => `/applications/${applicationId}` },
  { key: "timeline", label: "Timeline", href: (applicationId: string) => `/applications/${applicationId}/timeline` },
  { key: "evidence", label: "Evidence", href: (applicationId: string) => `/applications/${applicationId}/evidence` },
  { key: "oversight", label: "Oversight", href: (applicationId: string) => `/applications/${applicationId}/oversight` },
  { key: "agents", label: "Agents", href: (applicationId: string) => `/applications/${applicationId}/agents` }
];

export function ApplicationSectionNav({ applicationId }: ApplicationSectionNavProps) {
  const pathname = usePathname();

  return (
    <nav className="section-nav" aria-label="Application sections">
      {NAV_ITEMS.map((item) => {
        const href = item.href(applicationId);
        const active = pathname === href;
        return (
          <Link key={item.key} href={href} className={`section-nav-link ${active ? "section-nav-link-active" : ""}`}>
            {item.label}
          </Link>
        );
      })}
    </nav>
  );
}

"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

import type { QueueLane } from "../lib/queues";
import { QUEUE_LABELS, QUEUE_ORDER, queueHref } from "../lib/queues";

interface QueueNavProps {
  counts: Record<QueueLane, number>;
}

export function QueueNav({ counts }: QueueNavProps) {
  const pathname = usePathname();

  return (
    <nav className="queue-nav" aria-label="Application queues">
      {QUEUE_ORDER.map((lane) => {
        const href = queueHref(lane);
        const active = pathname === href;
        return (
          <Link key={lane} href={href} className={`queue-nav-link ${active ? "queue-nav-link-active" : ""}`}>
            <span>{QUEUE_LABELS[lane]}</span>
            <strong>{counts[lane]}</strong>
          </Link>
        );
      })}
    </nav>
  );
}

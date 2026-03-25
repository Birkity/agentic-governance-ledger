import Link from "next/link";

import type { QueueLane } from "../lib/queues";
import { QUEUE_DESCRIPTIONS, QUEUE_LABELS, QUEUE_ORDER, queueHref } from "../lib/queues";

interface QueueIndexProps {
  counts: Record<QueueLane, number>;
}

export function QueueIndex({ counts }: QueueIndexProps) {
  return (
    <section className="panel stack-lg">
      <div className="section-header">
        <div>
          <p className="eyebrow">Application Queues</p>
          <h2>Enter a dedicated workspace</h2>
          <p className="muted-copy">Start from the business lane you need instead of scanning the entire portfolio at once.</p>
        </div>
      </div>

      <div className="queue-index-grid">
        {QUEUE_ORDER.map((lane) => (
          <Link key={lane} href={queueHref(lane)} className="queue-index-card">
            <span className="queue-label">{QUEUE_LABELS[lane]}</span>
            <strong>{counts[lane]}</strong>
            <p>{QUEUE_DESCRIPTIONS[lane]}</p>
          </Link>
        ))}
      </div>
    </section>
  );
}

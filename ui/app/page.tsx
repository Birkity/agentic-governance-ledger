import Link from "next/link";

import { ApplicationLaunchPanel } from "../components/ApplicationLaunchPanel";
import { QueueIndex } from "../components/QueueIndex";
import { getCompanyCatalogData, getDashboardData } from "../lib/ledger-data";
import { QUEUE_LABELS, getQueueCounts, queueHref, type QueueLane } from "../lib/queues";

export const dynamic = "force-dynamic";

function share(part: number, total: number): number {
  if (total === 0) {
    return 0;
  }
  return Math.round((part / total) * 100);
}

export default async function HomePage() {
  const dashboard = await getDashboardData();
  const companies = await getCompanyCatalogData();
  const counts = getQueueCounts(dashboard.applications);
  const queueSignals: QueueLane[] = ["human", "open", "approved", "declined"];
  const primaryLane = queueSignals.reduce((best, lane) => (counts[lane] > counts[best] ? lane : best), "open");

  return (
    <div className="stack-xl">
      <section className="front-hero">
        <div className="front-hero-copy stack-lg">
          <div className="hero-copy">
            <p className="eyebrow">The Ledger</p>
            <h1>Review Workspace</h1>
            <p className="hero-body">Track applications, evidence, review status, and final decisions in one auditable workspace.</p>
          </div>

          <div className="hero-action-row">
            <Link href={queueHref(primaryLane)} className="hero-button hero-button-primary">
              Open {QUEUE_LABELS[primaryLane]}
            </Link>
            <Link href="/operations" className="hero-button hero-button-secondary">
              View Operations
            </Link>
          </div>

          <div className="hero-signal-strip">
            <article className="hero-signal-card">
              <span className="queue-label">Human review load</span>
              <strong>{share(counts.human, counts.all)}%</strong>
              <p>{counts.human} client-visible applications currently touch the manual review path.</p>
            </article>

            <article className="hero-signal-card">
              <span className="queue-label">Decision mix</span>
              <strong>{counts.approved + counts.declined}</strong>
              <p>Closed applications already split across approval and decline.</p>
            </article>
          </div>
        </div>

        <div className="front-hero-panel stack-lg">
          <div className="hero-panel-head">
            <div>
              <p className="eyebrow">Today at a glance</p>
              <h2>Portfolio pulse</h2>
            </div>
            <span className="status-pill status-pill-neutral">
              {dashboard.mode === "database" ? "Live database" : "Seed replay"}
            </span>
          </div>

          <div className="hero-metrics minimal-metrics">
            <div className="metric-card">
              <span>Applications</span>
              <strong>{dashboard.totals.applications}</strong>
            </div>
            <div className="metric-card">
              <span>Seeded</span>
              <strong>{dashboard.totals.seededApplications}</strong>
            </div>
            <div className="metric-card">
              <span>Live workflow</span>
              <strong>{dashboard.totals.liveApplications}</strong>
            </div>
            <div className="metric-card">
              <span>Human review</span>
              <strong>{dashboard.totals.humanReview}</strong>
            </div>
          </div>

          <div className="hero-lane-stack">
            {queueSignals.map((lane) => (
              <div key={lane} className="hero-lane-row">
                <div className="hero-lane-copy">
                  <span>{QUEUE_LABELS[lane]}</span>
                  <strong>{counts[lane]}</strong>
                </div>
                <div className="hero-lane-meter" aria-hidden="true">
                  <span style={{ width: `${share(counts[lane], counts.all)}%` }} />
                </div>
              </div>
            ))}
          </div>
        </div>
      </section>

      <section className="front-ribbon">
        <article className="ribbon-card">
          <span className="queue-label">Primary lane</span>
          <strong>{QUEUE_LABELS[primaryLane]}</strong>
          <p>The busiest workspace right now based on tracked application volume.</p>
        </article>

        <article className="ribbon-card">
          <span className="queue-label">Portfolio origin</span>
          <strong>
            {dashboard.totals.seededApplications} seeded / {dashboard.totals.liveApplications} live
          </strong>
          <p>Every visible application now shows whether it came from the seeded portfolio or live workflow activity.</p>
        </article>

        <article className="ribbon-card">
          <span className="queue-label">Focus</span>
          <strong>{counts.open} active applications</strong>
          <p>Active pipeline work stays separate from manual review and final outcomes.</p>
        </article>
      </section>

      <ApplicationLaunchPanel companies={companies} canLaunch={dashboard.mode === "database"} />

      <QueueIndex counts={counts} />
    </div>
  );
}

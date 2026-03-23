import { QueueIndex } from "../components/QueueIndex";
import { getDashboardData } from "../lib/ledger-data";
import { getQueueCounts } from "../lib/queues";

export const dynamic = "force-dynamic";

function summarizeArtifact(report: string): string[] {
  return report
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean)
    .slice(0, 4);
}

export default async function HomePage() {
  const dashboard = await getDashboardData();
  const counts = getQueueCounts(dashboard.applications);
  const lagHighlights = summarizeArtifact(dashboard.operations.projectionLagReport);
  const occHighlights = summarizeArtifact(dashboard.operations.concurrencyReport);

  return (
    <div className="stack-xl">
      <section className="overview-shell">
        <div className="hero-copy">
          <p className="eyebrow">The Ledger</p>
          <h1>Command Center</h1>
          <p className="hero-body">
            A quieter operator view for tracing applications, evidence, audit signals, concurrency guardrails, and human
            review from one place.
          </p>
        </div>

        <div className="hero-metrics minimal-metrics">
          <div className="metric-card">
            <span>Applications</span>
            <strong>{dashboard.totals.applications}</strong>
          </div>
          <div className="metric-card">
            <span>Approved</span>
            <strong>{dashboard.totals.finalApproved}</strong>
          </div>
          <div className="metric-card">
            <span>Declined</span>
            <strong>{dashboard.totals.finalDeclined}</strong>
          </div>
          <div className="metric-card">
            <span>Human review</span>
            <strong>{dashboard.totals.humanReview}</strong>
          </div>
        </div>
      </section>

      <section className="ops-strip">
        <article className="panel compact-panel stack-md">
          <div className="section-header">
            <div>
              <p className="eyebrow">Projection Health</p>
              <h2>Read freshness</h2>
            </div>
          </div>
          <ul className="bullet-list">
            {lagHighlights.map((line) => (
              <li key={line}>{line}</li>
            ))}
          </ul>
          <details className="artifact-details">
            <summary>Open full report</summary>
            <pre className="artifact-panel">{dashboard.operations.projectionLagReport}</pre>
          </details>
        </article>

        <article className="panel compact-panel stack-md">
          <div className="section-header">
            <div>
              <p className="eyebrow">Concurrency Guardrail</p>
              <h2>Conflict handling</h2>
            </div>
          </div>
          <ul className="bullet-list">
            {occHighlights.map((line) => (
              <li key={line}>{line}</li>
            ))}
          </ul>
          <details className="artifact-details">
            <summary>Open full report</summary>
            <pre className="artifact-panel">{dashboard.operations.concurrencyReport}</pre>
          </details>
        </article>
      </section>

      <QueueIndex counts={counts} />
    </div>
  );
}

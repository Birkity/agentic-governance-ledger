import { getDashboardData } from "../../lib/ledger-data";

export const dynamic = "force-dynamic";

function summarizeArtifact(report: string): string[] {
  return report
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean)
    .slice(0, 6);
}

function extractGeneratedAt(report: string): string | null {
  const match = report.match(/Generated at:\s*(.+)/);
  return match?.[1] ?? null;
}

function formatArtifactTimestamp(value: string | null): string {
  if (!value) {
    return "Artifact available";
  }

  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) {
    return value;
  }

  return new Intl.DateTimeFormat("en-US", {
    dateStyle: "medium",
    timeStyle: "short"
  }).format(parsed);
}

export default async function OperationsPage() {
  const dashboard = await getDashboardData();
  const lagHighlights = summarizeArtifact(dashboard.operations.projectionLagReport);
  const concurrencyHighlights = summarizeArtifact(dashboard.operations.concurrencyReport);
  const generatedAt = extractGeneratedAt(dashboard.operations.projectionLagReport);

  return (
    <div className="stack-xl">
      <section className="hero-panel operations-hero">
        <div className="hero-copy">
          <p className="eyebrow">Operations</p>
          <h1>System health and guardrails</h1>
          <p className="hero-body">
            Inspect projection freshness, rebuild evidence, and concurrency protection without crowding the application
            workspace.
          </p>
        </div>

        <div className="hero-metrics operations-metrics">
          <div className="metric-card">
            <span>Source mode</span>
            <strong>{dashboard.operations.sourceMode === "database" ? "Live database" : "Seed replay"}</strong>
          </div>
          <div className="metric-card">
            <span>Last projection report</span>
            <strong>{formatArtifactTimestamp(generatedAt)}</strong>
          </div>
        </div>
      </section>

      <section className="operations-grid">
        <article className="panel stack-lg">
          <div className="section-header">
            <div>
              <p className="eyebrow">Projection Health</p>
              <h2>Read freshness and rebuild evidence</h2>
              <p className="muted-copy">Lag SLO checks, concurrent submission coverage, and rebuild validation live here.</p>
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

        <article className="panel stack-lg">
          <div className="section-header">
            <div>
              <p className="eyebrow">Concurrency Guardrail</p>
              <h2>Optimistic conflict handling</h2>
              <p className="muted-copy">The dedicated collision test and its assertions stay available for operator review.</p>
            </div>
          </div>

          <ul className="bullet-list">
            {concurrencyHighlights.map((line) => (
              <li key={line}>{line}</li>
            ))}
          </ul>

          <details className="artifact-details">
            <summary>Open full report</summary>
            <pre className="artifact-panel">{dashboard.operations.concurrencyReport}</pre>
          </details>
        </article>
      </section>
    </div>
  );
}

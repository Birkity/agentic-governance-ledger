import { SectionDataList } from "../../components/SectionDataList";
import { getOperationsData, type ProjectionLiveHealth } from "../../lib/ledger-data";

export const dynamic = "force-dynamic";

function summarizeArtifact(report: string): string[] {
  return report
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean)
    .slice(0, 6);
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

function formatLag(value: number): string {
  if (value <= 0) {
    return "0 ms";
  }
  if (value < 1000) {
    return `${value} ms`;
  }
  return `${(value / 1000).toFixed(1)} s`;
}

function formatRelativeMinutes(value: number | null): string {
  if (value === null) {
    return "None pending";
  }
  if (value < 1) {
    return "<1 min";
  }
  if (value < 60) {
    return `${value} min`;
  }
  const hours = Math.floor(value / 60);
  const minutes = value % 60;
  return minutes === 0 ? `${hours} hr` : `${hours} hr ${minutes} min`;
}

function projectionStatusLabel(status: ProjectionLiveHealth["status"]): string {
  switch (status) {
    case "healthy":
      return "Healthy";
    case "lagging":
      return "Lagging";
    case "stalled":
      return "Stalled";
    case "idle":
      return "Idle";
    case "not_started":
      return "Not started";
    default:
      return status;
  }
}

function projectionStatusClass(status: ProjectionLiveHealth["status"]): string {
  switch (status) {
    case "healthy":
      return "status-pill status-pill-success";
    case "lagging":
      return "status-pill status-pill-warning";
    case "stalled":
      return "status-pill status-pill-danger";
    default:
      return "status-pill status-pill-neutral";
  }
}

export default async function OperationsPage() {
  const operations = await getOperationsData();
  const lagHighlights = summarizeArtifact(operations.projectionLagReport);
  const concurrencyHighlights = summarizeArtifact(operations.concurrencyReport);
  const healthyProjectionCount = operations.liveTelemetry?.projections.filter((projection) => projection.status === "healthy").length ?? 0;
  const totalProjectionCount = operations.liveTelemetry?.projections.length ?? 0;

  return (
    <div className="stack-xl">
      <section className="hero-panel operations-hero">
        <div className="hero-copy">
          <p className="eyebrow">Operations</p>
          <h1>Read-side health and guardrails</h1>
          <p className="hero-body">
            Inspect live database freshness, projection checkpoints, outbox backlog, rebuild evidence, and concurrency
            protection without crowding the application workspace. These panels combine live telemetry with submission
            artifacts instead of treating one as a substitute for the other.
          </p>
        </div>

        <div className="hero-metrics operations-metrics">
          <div className="metric-card">
            <span>Source mode</span>
            <strong>{operations.sourceMode === "database" ? "Live database" : "Seed replay"}</strong>
          </div>
          <div className="metric-card">
            <span>Latest ledger event</span>
            <strong>{formatArtifactTimestamp(operations.liveTelemetry?.eventStore.latestRecordedAt ?? null)}</strong>
          </div>
          <div className="metric-card">
            <span>Pending outbox</span>
            <strong>{operations.outboxPending ?? "N/A"}</strong>
          </div>
          <div className="metric-card">
            <span>Projection watch</span>
            <strong>{operations.liveTelemetry ? `${healthyProjectionCount}/${totalProjectionCount} healthy` : "Artifact only"}</strong>
          </div>
        </div>
      </section>

      <section className="panel stack-lg">
        <div className="section-header">
          <div>
            <p className="eyebrow">Live Telemetry</p>
            <h2>Current database observability</h2>
            <p className="muted-copy">
              This section is computed from the live PostgreSQL tables on every request, including projection
              checkpoints, event-store freshness, and outbox backlog age.
            </p>
          </div>
        </div>

        {operations.liveTelemetry ? (
          <>
            <SectionDataList
              columns={3}
              items={[
                { label: "Captured at", value: formatArtifactTimestamp(operations.liveTelemetry.capturedAt) },
                { label: "Events", value: operations.liveTelemetry.eventStore.totalEvents },
                { label: "Streams", value: operations.liveTelemetry.eventStore.totalStreams },
                { label: "Archived streams", value: operations.liveTelemetry.eventStore.archivedStreams },
                { label: "Aggregate snapshots", value: operations.liveTelemetry.eventStore.aggregateSnapshots },
                {
                  label: "Latest global position",
                  value: operations.liveTelemetry.eventStore.latestGlobalPosition ?? "Not recorded"
                }
              ]}
            />

            <div className="queue-index-grid">
              {operations.liveTelemetry.projections.map((projection) => (
                <article key={projection.projectionName} className="queue-index-card stack-md">
                  <div className="section-header">
                    <div>
                      <span className="eyebrow">{projection.projectionName.replace(/_/g, " ")}</span>
                      <strong>{projectionStatusLabel(projection.status)}</strong>
                    </div>
                    <span className={projectionStatusClass(projection.status)}>{projectionStatusLabel(projection.status)}</span>
                  </div>

                  <SectionDataList
                    items={[
                      { label: "Lag", value: formatLag(projection.lagMs) },
                      { label: "Lag positions", value: projection.lagPositions },
                      { label: "SLO target", value: formatLag(projection.sloTargetMs) },
                      { label: "Checkpoint", value: projection.checkpointPosition },
                      { label: "Last processed", value: formatArtifactTimestamp(projection.lastProcessedAt) },
                      { label: "Rows", value: projection.rowCount ?? "Not recorded" }
                    ]}
                    columns={2}
                  />

                  {projection.historyRowCount !== null ? (
                    <p className="muted-copy">History snapshots: {projection.historyRowCount}</p>
                  ) : null}
                </article>
              ))}
            </div>
          </>
        ) : (
          <p className="muted-copy">
            Live projection telemetry is unavailable in seed mode or when the database connection is not available.
          </p>
        )}
      </section>

      <section className="operations-grid">
        <article className="panel stack-lg">
          <div className="section-header">
            <div>
              <p className="eyebrow">Outbox Telemetry</p>
              <h2>Delivery backlog and age</h2>
              <p className="muted-copy">
                The outbox is the write-to-publish safety boundary. These numbers show whether downstream publication is
                draining normally or accumulating risk.
              </p>
            </div>
          </div>

          {operations.liveTelemetry ? (
            <>
              <SectionDataList
                items={[
                  { label: "Pending records", value: operations.liveTelemetry.outbox.pending },
                  { label: "Oldest pending", value: formatArtifactTimestamp(operations.liveTelemetry.outbox.oldestPendingAt) },
                  { label: "Backlog age", value: formatRelativeMinutes(operations.liveTelemetry.outbox.oldestPendingAgeMinutes) },
                  { label: "Max attempts", value: operations.liveTelemetry.outbox.maxAttempts ?? "Not recorded" }
                ]}
                columns={2}
              />

              <ul className="bullet-list">
                {operations.liveTelemetry.outbox.destinations.length > 0 ? (
                  operations.liveTelemetry.outbox.destinations.map((destination) => (
                    <li key={destination.destination}>
                      {destination.destination}: {destination.pending} pending
                    </li>
                  ))
                ) : (
                  <li>No unpublished outbox records currently detected.</li>
                )}
              </ul>
            </>
          ) : (
            <p className="muted-copy">Outbox telemetry is only available in live database mode.</p>
          )}
        </article>

        <article className="panel stack-lg">
          <div className="section-header">
            <div>
              <p className="eyebrow">Projection Evidence</p>
              <h2>Benchmark and rebuild artifact</h2>
              <p className="muted-copy">
                This is the submission artifact for benchmark lag and seed rebuild correctness. It complements the live
                telemetry above rather than replacing it.
              </p>
            </div>
          </div>

          <ul className="bullet-list">
            {lagHighlights.map((line) => (
              <li key={line}>{line}</li>
            ))}
          </ul>

          <details className="artifact-details">
            <summary>Open full report</summary>
            <pre className="artifact-panel">{operations.projectionLagReport}</pre>
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
            <pre className="artifact-panel">{operations.concurrencyReport}</pre>
          </details>
        </article>
      </section>
    </div>
  );
}

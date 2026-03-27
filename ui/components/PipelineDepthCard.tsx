import type { PipelineStageSnapshot } from "../lib/ledger-data";
import { formatDateTime } from "../lib/presenters";

interface PipelineDepthCardProps {
  stages: PipelineStageSnapshot[];
}

function badgeClass(status: PipelineStageSnapshot["status"]): string {
  if (status === "complete") {
    return "status-pill-success";
  }
  if (status === "current") {
    return "status-pill-neutral";
  }
  return "status-pill-warning";
}

function badgeLabel(status: PipelineStageSnapshot["status"]): string {
  if (status === "complete") {
    return "Complete";
  }
  if (status === "current") {
    return "Current";
  }
  return "Upcoming";
}

export function PipelineDepthCard({ stages }: PipelineDepthCardProps) {
  return (
    <div className="pipeline-stage-grid">
      {stages.map((stage) => (
        <article key={stage.key} className={`stage-card pipeline-stage-card pipeline-stage-${stage.status}`}>
          <div className="pipeline-stage-header">
            <div>
              <span className="fact-label">{stage.label}</span>
              <strong>{stage.summary}</strong>
            </div>
            <span className={`status-pill ${badgeClass(stage.status)}`}>{badgeLabel(stage.status)}</span>
          </div>

          <div className="pipeline-stage-meta">
            <span>Latest durable output</span>
            <strong>{formatDateTime(stage.recordedAt)}</strong>
          </div>

          <dl className="pipeline-stage-facts">
            {stage.highlights.map((highlight) => (
              <div key={`${stage.key}-${highlight.label}`}>
                <dt>{highlight.label}</dt>
                <dd>{highlight.value}</dd>
              </div>
            ))}
          </dl>
        </article>
      ))}
    </div>
  );
}

import Link from "next/link";

import type { ApplicationDetail } from "../lib/ledger-data";
import { formatCurrency, formatDateTime } from "../lib/presenters";
import { ApplicationSectionNav } from "./ApplicationSectionNav";

interface ApplicationHeaderProps {
  detail: ApplicationDetail;
}

function statusTone(state: string): string {
  if (state === "FINAL_APPROVED") {
    return "status-pill-success";
  }
  if (state === "FINAL_DECLINED" || state === "DECLINED_COMPLIANCE") {
    return "status-pill-danger";
  }
  if (state.includes("HUMAN")) {
    return "status-pill-warning";
  }
  return "status-pill-neutral";
}

export function ApplicationHeader({ detail }: ApplicationHeaderProps) {
  return (
    <section className="application-shell-header">
      <div className="application-shell-topline">
        <Link href="/" className="ghost-link">
          Back to queues
        </Link>
        <span className="status-pill status-pill-neutral">
          Source {detail.sourceMode === "database" ? "live database" : "seed replay"}
        </span>
      </div>

      <div className="application-shell-main">
        <div className="hero-copy">
          <p className="eyebrow">{detail.item.applicationId}</p>
          <h1>{detail.item.companyName}</h1>
          <p className="application-subtle">
            {detail.company?.industry ?? detail.item.industry ?? "Unknown industry"} ·{" "}
            {detail.company?.jurisdiction ?? detail.item.jurisdiction ?? "No jurisdiction"}
          </p>
        </div>

        <div className="application-shell-metrics">
          <div className="mini-metric">
            <span>Status</span>
            <strong>{detail.item.state.replaceAll("_", " ")}</strong>
          </div>
          <div className="mini-metric">
            <span>Requested</span>
            <strong>{formatCurrency(detail.item.requestedAmountUsd)}</strong>
          </div>
          <div className="mini-metric">
            <span>Decision</span>
            <strong>{detail.item.decision ?? "Pending"}</strong>
          </div>
          <div className="mini-metric">
            <span>Compliance</span>
            <strong>{detail.item.complianceStatus ?? "In progress"}</strong>
          </div>
          <div className="mini-metric">
            <span>Updated</span>
            <strong>{formatDateTime(detail.item.lastEventAt)}</strong>
          </div>
          <div className="mini-metric">
            <span>Human review</span>
            <strong>{detail.review.requested || detail.review.completed ? "Involved" : "Not required"}</strong>
          </div>
          <div className="mini-metric mini-metric-pill">
            <span className={`status-pill ${statusTone(detail.item.state)}`}>{detail.item.state.replaceAll("_", " ")}</span>
          </div>
        </div>
      </div>

      <ApplicationSectionNav applicationId={detail.item.applicationId} />
    </section>
  );
}

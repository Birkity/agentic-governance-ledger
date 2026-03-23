"use client";

import Link from "next/link";
import { useState } from "react";

import type { ApplicationListItem, DataSourceMode } from "../lib/ledger-data";
import type { QueueLane } from "../lib/queues";
import { formatCurrency, formatDateTime } from "../lib/presenters";
import { QUEUE_DESCRIPTIONS, QUEUE_LABELS, getQueueCounts, matchesQueue } from "../lib/queues";
import { QueueNav } from "./QueueNav";

interface ApplicationExplorerProps {
  applications: ApplicationListItem[];
  sourceMode: DataSourceMode;
  queue: QueueLane;
}

function statusTone(item: ApplicationListItem): string {
  if (item.state === "FINAL_APPROVED") {
    return "status-pill-success";
  }
  if (item.state === "FINAL_DECLINED" || item.state === "DECLINED_COMPLIANCE") {
    return "status-pill-danger";
  }
  if (item.hasHumanReview || item.state.includes("HUMAN")) {
    return "status-pill-warning";
  }
  return "status-pill-neutral";
}

export function ApplicationExplorer({ applications, sourceMode, queue }: ApplicationExplorerProps) {
  const [query, setQuery] = useState("");
  const counts = getQueueCounts(applications);

  const lowered = query.trim().toLowerCase();
  const filtered = applications.filter((item) => {
    if (!matchesQueue(item, queue)) {
      return false;
    }
    if (!lowered) {
      return true;
    }
    return [
      item.applicationId,
      item.companyName,
      item.applicantId,
      item.state,
      item.decision,
      item.complianceStatus,
      item.riskTier
    ]
      .filter(Boolean)
      .some((value) => String(value).toLowerCase().includes(lowered));
  });

  return (
    <section className="panel stack-lg">
      <div className="section-header">
        <div>
          <p className="eyebrow">Application Queue</p>
          <h2>{QUEUE_LABELS[queue]}</h2>
          <p className="muted-copy">{QUEUE_DESCRIPTIONS[queue]}</p>
        </div>
        <span className="status-pill status-pill-neutral">
          Source {sourceMode === "database" ? "live database" : "seed replay"}
        </span>
      </div>

      <div className="toolbar-grid">
        <QueueNav counts={counts} />

        <label className="field-shell search-shell">
          <span className="field-label">Search applications</span>
          <input
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="Company, application id, compliance, risk tier..."
            className="text-input"
          />
        </label>
      </div>

      <div className="results-meta">
        <span>{filtered.length} in {QUEUE_LABELS[queue].toLowerCase()}</span>
        <span>{applications.length} tracked overall</span>
      </div>

      <div className="application-grid">
        {filtered.map((item) => (
          <Link key={item.applicationId} href={`/applications/${item.applicationId}`} className="application-card">
            <div className="application-card-top">
              <div>
                <p className="card-kicker">{item.applicationId}</p>
                <h3>{item.companyName}</h3>
                <p className="application-subtle">
                  {[item.industry ?? "Unknown industry", item.jurisdiction ?? "No jurisdiction"].join(" / ")}
                </p>
              </div>
              <span className={`status-pill ${statusTone(item)}`}>{item.state.replaceAll("_", " ")}</span>
            </div>

            <div className="compact-facts">
              <div>
                <span className="fact-label">Requested</span>
                <strong>{formatCurrency(item.requestedAmountUsd)}</strong>
              </div>
              <div>
                <span className="fact-label">Decision</span>
                <strong>{item.decision ?? "Pending"}</strong>
              </div>
              <div>
                <span className="fact-label">Compliance</span>
                <strong>{item.complianceStatus ?? "In progress"}</strong>
              </div>
              <div>
                <span className="fact-label">Risk tier</span>
                <strong>{item.riskTier ?? "Not recorded"}</strong>
              </div>
            </div>

            <div className="meta-row">
              {item.hasHumanReview || item.state.includes("HUMAN") ? <span>Human review path</span> : <span>Automated path</span>}
              <span>{item.eventCount} events</span>
              <span>{formatDateTime(item.lastEventAt)}</span>
            </div>
          </Link>
        ))}
      </div>

      {filtered.length === 0 ? <p className="muted-copy">No applications matched the current search.</p> : null}
    </section>
  );
}

"use client";

import Link from "next/link";
import { useState } from "react";

import type { ApplicationListItem, DataSourceMode } from "../lib/ledger-data";
import { formatCurrency, formatDateTime } from "../lib/presenters";

type FilterKey = "human" | "open" | "approved" | "declined" | "all";

interface ApplicationExplorerProps {
  applications: ApplicationListItem[];
  sourceMode: DataSourceMode;
}

const FILTER_LABELS: Record<FilterKey, string> = {
  human: "Human Review",
  open: "In Flight",
  approved: "Approved",
  declined: "Declined",
  all: "All"
};

function matchesFilter(item: ApplicationListItem, filter: FilterKey): boolean {
  if (filter === "human") {
    return item.hasHumanReview || item.state.includes("HUMAN");
  }
  if (filter === "open") {
    return !item.state.startsWith("FINAL") && !item.state.startsWith("DECLINED_");
  }
  if (filter === "approved") {
    return item.state === "FINAL_APPROVED";
  }
  if (filter === "declined") {
    return item.state === "FINAL_DECLINED" || item.state === "DECLINED_COMPLIANCE";
  }
  return true;
}

function describeQueue(filter: FilterKey): string {
  if (filter === "human") {
    return "Applications that need, or already passed through, manual intervention.";
  }
  if (filter === "open") {
    return "Active work in progress before a final outcome is locked.";
  }
  if (filter === "approved") {
    return "Applications that reached a final approval outcome.";
  }
  if (filter === "declined") {
    return "Applications that ended in decline, including compliance hard blocks.";
  }
  return "A searchable view across the full application ledger.";
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

export function ApplicationExplorer({ applications, sourceMode }: ApplicationExplorerProps) {
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState<FilterKey>("human");

  const counts = {
    human: applications.filter((item) => matchesFilter(item, "human")).length,
    open: applications.filter((item) => matchesFilter(item, "open")).length,
    approved: applications.filter((item) => matchesFilter(item, "approved")).length,
    declined: applications.filter((item) => matchesFilter(item, "declined")).length,
    all: applications.length
  };

  const lowered = query.trim().toLowerCase();
  const filtered = applications.filter((item) => {
    if (!matchesFilter(item, filter)) {
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
          <p className="eyebrow">Application Queues</p>
          <h2>Select a lane, then open an application</h2>
          <p className="muted-copy">{describeQueue(filter)}</p>
        </div>
        <span className="status-pill status-pill-neutral">
          Source {sourceMode === "database" ? "live database" : "seed replay"}
        </span>
      </div>

      <div className="toolbar-grid">
        <div className="queue-grid" role="tablist" aria-label="Application queues">
          {(Object.keys(FILTER_LABELS) as FilterKey[]).map((value) => (
            <button
              key={value}
              type="button"
              className={`queue-card ${filter === value ? "queue-card-active" : ""}`}
              onClick={() => setFilter(value)}
            >
              <span className="queue-label">{FILTER_LABELS[value]}</span>
              <strong>{counts[value]}</strong>
            </button>
          ))}
        </div>

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
        <span>{filtered.length} in this queue</span>
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
                  {item.industry ?? "Unknown industry"} · {item.jurisdiction ?? "No jurisdiction"}
                </p>
              </div>
              <span className={`status-pill ${statusTone(item)}`}>
                {item.state.replaceAll("_", " ")}
              </span>
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

      {filtered.length === 0 ? <p className="muted-copy">No applications matched the current search and filter.</p> : null}
    </section>
  );
}

"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

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

const PAGE_SIZE = 8;

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
  const [page, setPage] = useState(1);
  const counts = getQueueCounts(applications);

  useEffect(() => {
    setPage(1);
  }, [queue]);

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

  const totalPages = Math.max(1, Math.ceil(filtered.length / PAGE_SIZE));
  const currentPage = Math.min(page, totalPages);
  const startIndex = (currentPage - 1) * PAGE_SIZE;
  const visibleItems = filtered.slice(startIndex, startIndex + PAGE_SIZE);
  const pageStart = filtered.length === 0 ? 0 : startIndex + 1;
  const pageEnd = filtered.length === 0 ? 0 : startIndex + visibleItems.length;

  useEffect(() => {
    setPage((value) => Math.min(value, totalPages));
  }, [totalPages]);

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
            onChange={(event) => {
              setQuery(event.target.value);
              setPage(1);
            }}
            placeholder="Company, application id, compliance, risk tier..."
            className="text-input"
          />
        </label>
      </div>

      <div className="results-meta">
        <span>{filtered.length} in {QUEUE_LABELS[queue].toLowerCase()}</span>
        <span>{applications.length} tracked overall</span>
        <span>
          Showing {pageStart}-{pageEnd}
        </span>
      </div>

      <div className="application-list">
        {visibleItems.map((item) => (
          <article key={item.applicationId} className="application-row">
            <div className="application-row-main">
              <div>
                <p className="card-kicker">{item.applicationId}</p>
                <h3 className="row-title">{item.companyName}</h3>
                <p className="application-subtle">
                  {[item.industry ?? "Unknown industry", item.jurisdiction ?? "No jurisdiction"].join(" / ")}
                </p>
              </div>
            </div>

            <div className="application-row-status">
              <span className={`status-pill ${statusTone(item)}`}>{item.state.replaceAll("_", " ")}</span>
              <span className="tag">{item.hasHumanReview || item.state.includes("HUMAN") ? "Human review path" : "Automated path"}</span>
            </div>

            <div className="application-row-metrics">
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

            <div className="application-row-meta">
              <span>{item.eventCount} events</span>
              <span>{formatDateTime(item.lastEventAt)}</span>
            </div>

            <div className="application-row-action">
              <Link href={`/applications/${item.applicationId}`} className="ghost-link">
                Open application
              </Link>
            </div>
          </article>
        ))}
      </div>

      {filtered.length === 0 ? <p className="muted-copy">No applications matched the current search.</p> : null}

      {filtered.length > PAGE_SIZE ? (
        <div className="pagination-bar" aria-label="Queue pagination">
          <button
            type="button"
            className="pagination-button"
            onClick={() => setPage((value) => Math.max(1, value - 1))}
            disabled={currentPage === 1}
          >
            Previous
          </button>

          <div className="pagination-summary">
            <strong>Page {currentPage}</strong>
            <span>of {totalPages}</span>
          </div>

          <button
            type="button"
            className="pagination-button"
            onClick={() => setPage((value) => Math.min(totalPages, value + 1))}
            disabled={currentPage === totalPages}
          >
            Next
          </button>
        </div>
      ) : null}
    </section>
  );
}

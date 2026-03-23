"use client";

import { useState } from "react";

import type { TimelineEvent } from "../lib/ledger-data";
import { formatDateTime, streamFamilyLabel } from "../lib/presenters";

interface TimelineExplorerProps {
  timeline: TimelineEvent[];
}

function isRiskyEvent(eventType: string): boolean {
  return ["Declined", "Failed", "Integrity", "Tamper"].some((word) => eventType.includes(word));
}

export function TimelineExplorer({ timeline }: TimelineExplorerProps) {
  const [query, setQuery] = useState("");
  const [family, setFamily] = useState("all");

  const families = ["all", ...Array.from(new Set(timeline.map((event) => event.streamFamily)))];
  const lowered = query.trim().toLowerCase();
  const filtered = timeline.filter((event) => {
    if (family !== "all" && event.streamFamily !== family) {
      return false;
    }
    if (!lowered) {
      return true;
    }

    return [
      event.eventType,
      event.streamId,
      event.payload.summary,
      event.payload.output_summary,
      event.metadata.correlation_id,
      event.metadata.causation_id
    ]
      .filter(Boolean)
      .some((value) => String(value).toLowerCase().includes(lowered));
  });

  return (
    <section className="panel stack-lg">
      <div className="section-header">
        <div>
          <p className="eyebrow">Full Timeline</p>
          <h2>Every durable event, in order</h2>
        </div>
        <span className="status-pill status-pill-neutral">{timeline.length} events</span>
      </div>

      <div className="toolbar-grid">
        <label className="field-shell">
          <span className="field-label">Search logs</span>
          <input
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="decision, compliance, correlation id..."
            className="text-input"
          />
        </label>

        <label className="field-shell">
          <span className="field-label">Stream family</span>
          <select value={family} onChange={(event) => setFamily(event.target.value)} className="select-input">
            {families.map((value) => (
              <option key={value} value={value}>
                {value === "all" ? "All streams" : streamFamilyLabel(value)}
              </option>
            ))}
          </select>
        </label>
      </div>

      <div className="timeline-list">
        {filtered.map((event) => (
          <article
            key={event.id}
            className={`timeline-card ${isRiskyEvent(event.eventType) ? "timeline-card-alert" : ""}`}
          >
            <div className="timeline-card-top">
              <div>
                <p className="card-kicker">{streamFamilyLabel(event.streamFamily)}</p>
                <h3>{event.eventType}</h3>
              </div>
              <div className="timeline-position">
                <span>g:{event.globalPosition ?? "?"}</span>
                <span>s:{event.streamPosition ?? "?"}</span>
              </div>
            </div>

            <div className="meta-row">
              <span>{formatDateTime(event.recordedAt)}</span>
              <span>{event.streamId}</span>
              <span>v{event.eventVersion}</span>
            </div>

            <div className="tag-row">
              {event.metadata.correlation_id ? <span className="tag">corr {String(event.metadata.correlation_id)}</span> : null}
              {event.metadata.causation_id ? <span className="tag">cause {String(event.metadata.causation_id)}</span> : null}
            </div>

            <details className="json-panel">
              <summary>Payload</summary>
              <pre>{JSON.stringify(event.payload, null, 2)}</pre>
            </details>

            {Object.keys(event.metadata).length > 0 ? (
              <details className="json-panel">
                <summary>Metadata</summary>
                <pre>{JSON.stringify(event.metadata, null, 2)}</pre>
              </details>
            ) : null}
          </article>
        ))}
      </div>
    </section>
  );
}

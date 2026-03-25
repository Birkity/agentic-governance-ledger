"use client";

import { startTransition, useState } from "react";
import { useRouter } from "next/navigation";

import type { CompanyCatalogItem } from "../lib/ledger-data";

interface ApplicationLaunchPanelProps {
  companies: CompanyCatalogItem[];
  canLaunch: boolean;
}

export function ApplicationLaunchPanel({ companies, canLaunch }: ApplicationLaunchPanelProps) {
  const router = useRouter();
  const [applicationId, setApplicationId] = useState("");
  const [companyId, setCompanyId] = useState(companies[0]?.companyId ?? "");
  const [phase, setPhase] = useState<"document" | "credit" | "full">("full");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const selectedCompany = companies.find((company) => company.companyId === companyId) ?? null;

  async function submit() {
    setSubmitting(true);
    setError(null);
    try {
      const response = await fetch("/api/applications", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          applicationId: applicationId.trim() || undefined,
          companyId,
          phase
        })
      });
      const payload = (await response.json()) as { ok?: boolean; error?: string; application_id?: string };
      if (!response.ok || payload.ok === false || !payload.application_id) {
        throw new Error(payload.error ?? "Unable to start the application");
      }
      startTransition(() => {
        router.push(`/applications/${payload.application_id}`);
        router.refresh();
      });
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Unable to start the application");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <section className="panel stack-lg">
      <div className="section-header">
        <div>
          <p className="eyebrow">Start Application</p>
          <h2>Launch From The Document Corpus</h2>
          <p className="muted-copy">Choose a company package, start a new application, and route it into the ledger workflow.</p>
        </div>
        <span className="status-pill status-pill-neutral">{canLaunch ? "Live workflow enabled" : "Live database required"}</span>
      </div>

      {!canLaunch ? (
        <p className="muted-copy">Starting new applications from the workspace is available when the UI is connected to a live database.</p>
      ) : null}

      <div className="toolbar-grid">
        <label className="field-shell">
          <span className="field-label">Company package</span>
          <select className="text-input" value={companyId} onChange={(event) => setCompanyId(event.target.value)} disabled={!canLaunch || submitting}>
            {companies.map((company) => (
              <option key={company.companyId} value={company.companyId}>
                {company.companyId} · {company.name}
              </option>
            ))}
          </select>
        </label>

        <label className="field-shell">
          <span className="field-label">Pipeline depth</span>
          <select className="text-input" value={phase} onChange={(event) => setPhase(event.target.value as "document" | "credit" | "full")} disabled={!canLaunch || submitting}>
            <option value="document">Document package only</option>
            <option value="credit">Through credit analysis</option>
            <option value="full">Full pipeline to manual review</option>
          </select>
        </label>

        <label className="field-shell">
          <span className="field-label">Application id</span>
          <input
            className="text-input"
            value={applicationId}
            onChange={(event) => setApplicationId(event.target.value)}
            placeholder="Leave blank to auto-generate"
            disabled={!canLaunch || submitting}
          />
        </label>
      </div>

      {selectedCompany ? (
        <div className="shortcut-grid">
          <article className="shortcut-card">
            <span className="fact-label">Company</span>
            <strong>{selectedCompany.name}</strong>
            <p>{selectedCompany.industry} / {selectedCompany.jurisdiction}</p>
          </article>
          <article className="shortcut-card">
            <span className="fact-label">Risk segment</span>
            <strong>{selectedCompany.riskSegment}</strong>
            <p>Trajectory {selectedCompany.trajectory.toLowerCase()}.</p>
          </article>
          <article className="shortcut-card">
            <span className="fact-label">Document package</span>
            <strong>{selectedCompany.documentCount} files</strong>
            <p>{selectedCompany.hasCompletePackage ? "Full package present." : "Package is incomplete."}</p>
          </article>
        </div>
      ) : null}

      {error ? <p className="muted-copy">{error}</p> : null}

      <div className="hero-action-row">
        <button type="button" className="hero-button hero-button-primary" disabled={!canLaunch || submitting || !companyId} onClick={submit}>
          {submitting ? "Starting..." : "Start application"}
        </button>
      </div>
    </section>
  );
}

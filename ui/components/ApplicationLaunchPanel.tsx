"use client";

import { startTransition, useState } from "react";
import { useRouter } from "next/navigation";

import type { CompanyCatalogItem } from "../lib/ledger-data";

interface ApplicationLaunchPanelProps {
  companies: CompanyCatalogItem[];
  canLaunch: boolean;
}

type ApplicationStartPhase = "document" | "credit" | "fraud" | "compliance" | "decision" | "full";

const PHASE_COPY: Record<ApplicationStartPhase, { title: string; description: string }> = {
  document: {
    title: "Document package only",
    description: "Parse the five-file company package and stop after the evidence is ready for analysis."
  },
  credit: {
    title: "Through credit analysis",
    description: "Process documents, submit the case, and stop after credit analysis is recorded."
  },
  fraud: {
    title: "Through fraud screening",
    description: "Continue from credit, record fraud screening, and stop before compliance begins."
  },
  compliance: {
    title: "Through compliance check",
    description: "Run documents, credit, fraud, and compliance, then stop before decision orchestration."
  },
  decision: {
    title: "Through decision recommendation",
    description: "Generate the automated recommendation and stop before a human-review request is written."
  },
  full: {
    title: "Full pipeline to manual review",
    description: "Run documents, credit, fraud, compliance, and decisioning, then hand off for human review."
  }
};

export function ApplicationLaunchPanel({ companies, canLaunch }: ApplicationLaunchPanelProps) {
  const router = useRouter();
  const [applicationId, setApplicationId] = useState("");
  const [companyId, setCompanyId] = useState(companies[0]?.companyId ?? "");
  const [phase, setPhase] = useState<ApplicationStartPhase>("full");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const selectedCompany = companies.find((company) => company.companyId === companyId) ?? null;
  const selectedPhase = PHASE_COPY[phase];

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
          <select className="text-input" value={phase} onChange={(event) => setPhase(event.target.value as ApplicationStartPhase)} disabled={!canLaunch || submitting}>
            <option value="document">Document package only</option>
            <option value="credit">Through credit analysis</option>
            <option value="fraud">Through fraud screening</option>
            <option value="compliance">Through compliance check</option>
            <option value="decision">Through decision recommendation</option>
            <option value="full">Full pipeline to manual review</option>
          </select>
          <p className="muted-copy">{selectedPhase.description}</p>
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
          <article className="shortcut-card">
            <span className="fact-label">Pipeline depth</span>
            <strong>{selectedPhase.title}</strong>
            <p>{selectedPhase.description}</p>
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

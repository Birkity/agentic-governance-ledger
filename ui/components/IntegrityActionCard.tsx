"use client";

import { startTransition, useState } from "react";
import { useRouter } from "next/navigation";

interface IntegrityActionCardProps {
  applicationId: string;
  sourceMode: "database" | "seed";
  latestCheckAt: string | null;
  chainValid: boolean | null;
}

export function IntegrityActionCard({
  applicationId,
  sourceMode,
  latestCheckAt,
  chainValid
}: IntegrityActionCardProps) {
  const router = useRouter();
  const [working, setWorking] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function runIntegrity() {
    setWorking(true);
    setError(null);
    try {
      const response = await fetch(`/api/applications/${applicationId}/integrity`, {
        method: "POST"
      });
      const payload = (await response.json()) as { ok?: boolean; error?: string };
      if (!response.ok || payload.ok === false) {
        throw new Error(payload.error ?? "Unable to run the integrity check");
      }
      startTransition(() => {
        router.refresh();
      });
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Unable to run the integrity check");
    } finally {
      setWorking(false);
    }
  }

  if (sourceMode !== "database") {
    return (
      <div className="stack-md">
        <div>
          <p className="eyebrow">Integrity Action</p>
          <h3>Live Database Required</h3>
          <p className="muted-copy">Integrity checks can be triggered from the workspace when the UI is connected to the live ledger.</p>
        </div>
      </div>
    );
  }

  return (
    <div className="stack-lg">
      <div>
        <p className="eyebrow">Integrity Action</p>
        <h3>Run Audit Check</h3>
        <p className="muted-copy">Append a fresh audit integrity record for this application without mutating the source stream.</p>
      </div>

      <div className="shortcut-grid">
        <article className="shortcut-card">
          <span className="fact-label">Last check</span>
          <strong>{latestCheckAt ?? "Not recorded"}</strong>
          <p>Most recent audit attestation captured in the ledger.</p>
        </article>
        <article className="shortcut-card">
          <span className="fact-label">Current chain status</span>
          <strong>{chainValid === null ? "Not checked" : chainValid ? "Healthy" : "Attention"}</strong>
          <p>Integrity validation is append-only and never rewrites source events.</p>
        </article>
      </div>

      {error ? <p className="muted-copy">{error}</p> : null}

      <div className="hero-action-row">
        <button type="button" className="hero-button hero-button-secondary" onClick={runIntegrity} disabled={working}>
          {working ? "Running..." : "Run integrity check"}
        </button>
      </div>
    </div>
  );
}

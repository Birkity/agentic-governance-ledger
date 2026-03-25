"use client";

import { startTransition, useState } from "react";
import { useRouter } from "next/navigation";

interface ApplicationActionsPanelProps {
  applicationId: string;
  companyId: string | null;
  state: string;
  reviewState: "not_required" | "pending" | "completed";
}

function canProcess(state: string, reviewState: "not_required" | "pending" | "completed"): boolean {
  if (!state || reviewState !== "not_required") {
    return false;
  }
  if (state.startsWith("FINAL") || state.startsWith("DECLINED_")) {
    return false;
  }
  return true;
}

export function ApplicationActionsPanel({ applicationId, companyId, state, reviewState }: ApplicationActionsPanelProps) {
  const router = useRouter();
  const [working, setWorking] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function processApplication() {
    if (!companyId) {
      return;
    }
    setWorking(true);
    setError(null);
    try {
      const response = await fetch(`/api/applications/${applicationId}/process`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          companyId,
          phase: "full"
        })
      });
      const payload = (await response.json()) as { ok?: boolean; error?: string };
      if (!response.ok || payload.ok === false) {
        throw new Error(payload.error ?? "Unable to continue the pipeline");
      }
      startTransition(() => {
        router.refresh();
      });
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Unable to continue the pipeline");
    } finally {
      setWorking(false);
    }
  }

  return (
    <div className="stack-lg">
      <div>
        <p className="eyebrow">Application Actions</p>
        <h3>Continue Workflow</h3>
        <p className="muted-copy">Use the authoritative runtime path to continue this application from its current state.</p>
      </div>

      {error ? <p className="muted-copy">{error}</p> : null}

      <div className="hero-action-row">
        <button
          type="button"
          className="hero-button hero-button-secondary"
          onClick={processApplication}
          disabled={!companyId || !canProcess(state, reviewState) || working}
        >
          {working ? "Processing..." : "Process to manual review"}
        </button>
      </div>

      {!companyId ? <p className="muted-copy">Applicant context is required before the pipeline can continue from the workspace.</p> : null}
      {companyId && !canProcess(state, reviewState) ? <p className="muted-copy">This application is either already final or is currently waiting on manual review.</p> : null}
    </div>
  );
}

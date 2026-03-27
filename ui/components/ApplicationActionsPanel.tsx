"use client";

import { startTransition, useState } from "react";
import { useRouter } from "next/navigation";

interface ApplicationActionsPanelProps {
  applicationId: string;
  companyId: string | null;
  state: string;
  reviewState: "not_required" | "pending" | "completed";
}

function actionForState(
  state: string,
  reviewState: "not_required" | "pending" | "completed"
): {
  canProcess: boolean;
  buttonLabel: string;
  description: string;
  reason?: string;
} {
  if (!state) {
    return {
      canProcess: false,
      buttonLabel: "Continue workflow",
      description: "The authoritative runtime path resumes from the next valid stage.",
      reason: "Current application state is not available yet."
    };
  }
  if (reviewState !== "not_required") {
    return {
      canProcess: false,
      buttonLabel: "Awaiting manual review",
      description: "This application has already reached the human-review stage.",
      reason: "Record the human review instead of continuing the automated workflow."
    };
  }
  if (state.startsWith("FINAL") || state.startsWith("DECLINED_")) {
    return {
      canProcess: false,
      buttonLabel: "Workflow complete",
      description: "This application has already reached a final recorded outcome.",
      reason: "No further automated processing is available from the workspace."
    };
  }

  switch (state) {
    case "SUBMITTED":
    case "DOCUMENTS_PENDING":
    case "DOCUMENTS_UPLOADED":
    case "DOCUMENTS_PROCESSED":
    case "AWAITING_ANALYSIS":
      return {
        canProcess: true,
        buttonLabel: "Continue to credit analysis",
        description: "Resume from intake and move the application through document readiness and credit."
      };
    case "ANALYSIS_COMPLETE":
      return {
        canProcess: true,
        buttonLabel: "Continue to fraud screening",
        description: "Credit is already complete. The next authoritative step is fraud screening."
      };
    case "COMPLIANCE_REVIEW":
      return {
        canProcess: true,
        buttonLabel: "Continue to compliance review",
        description: "Fraud is already complete. The next authoritative step is compliance."
      };
    case "PENDING_DECISION":
      return {
        canProcess: true,
        buttonLabel: "Generate recommendation",
        description: "All prerequisite analyses are complete. The next authoritative step is decision orchestration."
      };
    default:
      return {
        canProcess: true,
        buttonLabel: "Continue workflow",
        description: "Use the authoritative runtime path to resume from the next valid stage."
      };
  }
}

export function ApplicationActionsPanel({ applicationId, companyId, state, reviewState }: ApplicationActionsPanelProps) {
  const router = useRouter();
  const [working, setWorking] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);
  const action = actionForState(state, reviewState);

  async function processApplication() {
    if (!companyId) {
      return;
    }
    setWorking(true);
    setError(null);
    setSuccess(null);
    try {
      const response = await fetch(`/api/applications/${applicationId}/process`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          companyId,
          phase: "full"
        })
      });
      const payload = (await response.json()) as {
        ok?: boolean;
        error?: string;
        final_event_type?: string;
      };
      if (!response.ok || payload.ok === false) {
        throw new Error(payload.error ?? "Unable to continue the pipeline");
      }
      setSuccess(`Workflow advanced successfully. Latest recorded event: ${payload.final_event_type ?? "updated"}. Refreshing the workspace.`);
      startTransition(() => {
        router.refresh();
      });
    } catch (cause) {
      setSuccess(null);
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
        <p className="muted-copy">{action.description}</p>
        <p className="muted-copy">Current recorded state: {state}</p>
      </div>

      {error ? (
        <p className="muted-copy" role="alert">
          {error}
        </p>
      ) : null}
      {success ? (
        <p className="muted-copy" role="status">
          {success}
        </p>
      ) : null}

      <div className="hero-action-row">
        <button
          type="button"
          className="hero-button hero-button-secondary"
          onClick={processApplication}
          disabled={!companyId || !action.canProcess || working}
        >
          {working ? "Processing..." : action.buttonLabel}
        </button>
      </div>

      {!companyId ? <p className="muted-copy">Applicant context is required before the pipeline can continue from the workspace.</p> : null}
      {companyId && !action.canProcess ? <p className="muted-copy">{action.reason}</p> : null}
    </div>
  );
}

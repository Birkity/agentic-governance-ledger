"use client";

import { startTransition, useState } from "react";
import { useRouter } from "next/navigation";

interface HumanReviewActionCardProps {
  applicationId: string;
  currentRecommendation: string | null;
  approvedAmountUsd: string | null;
  reviewPending: boolean;
}

export function HumanReviewActionCard({
  applicationId,
  currentRecommendation,
  approvedAmountUsd,
  reviewPending
}: HumanReviewActionCardProps) {
  const router = useRouter();
  const [reviewerId, setReviewerId] = useState("loan-ops");
  const [finalDecision, setFinalDecision] = useState<"APPROVE" | "DECLINE">(
    currentRecommendation === "DECLINE" ? "DECLINE" : "APPROVE"
  );
  const [approvedAmount, setApprovedAmount] = useState(approvedAmountUsd ?? "");
  const [interestRate, setInterestRate] = useState("8.75");
  const [termMonths, setTermMonths] = useState("36");
  const [overrideReason, setOverrideReason] = useState("");
  const [declineReasons, setDeclineReasons] = useState(currentRecommendation === "DECLINE" ? "Manual decline confirmed" : "");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);

  const override = currentRecommendation ? currentRecommendation !== finalDecision : false;

  function validateSubmission(): string | null {
    if (!reviewerId.trim()) {
      return "Enter the reviewer ID before recording the review.";
    }
    if (override && !overrideReason.trim()) {
      return "Explain why the final decision differs from the automated recommendation.";
    }
    if (finalDecision === "APPROVE") {
      if (!approvedAmount.trim()) {
        return "Enter the approved amount for a manual approval.";
      }
      if (!interestRate.trim() || Number.isNaN(Number(interestRate))) {
        return "Enter a valid interest rate percentage.";
      }
      if (!termMonths.trim() || Number.isNaN(Number(termMonths))) {
        return "Enter a valid loan term in months.";
      }
      return null;
    }
    if (
      declineReasons
        .split(",")
        .map((item) => item.trim())
        .filter(Boolean).length === 0
    ) {
      return "Add at least one decline reason before recording a decline.";
    }
    return null;
  }

  async function submit() {
    const validationError = validateSubmission();
    if (validationError) {
      setError(validationError);
      setSuccess(null);
      return;
    }
    setSubmitting(true);
    setError(null);
    setSuccess(null);
    try {
      const response = await fetch(`/api/applications/${applicationId}/review`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          reviewerId,
          finalDecision,
          override,
          overrideReason: overrideReason.trim() || undefined,
          approvedAmountUsd: finalDecision === "APPROVE" ? approvedAmount : undefined,
          interestRatePct: finalDecision === "APPROVE" ? Number(interestRate) : undefined,
          termMonths: finalDecision === "APPROVE" ? Number(termMonths) : undefined,
          declineReasons:
            finalDecision === "DECLINE"
              ? declineReasons
                  .split(",")
                  .map((item) => item.trim())
                  .filter(Boolean)
              : [],
          adverseActionCodes: finalDecision === "DECLINE" ? ["MANUAL-REVIEW"] : []
        })
      });
      const payload = (await response.json()) as {
        ok?: boolean;
        error?: string;
        final_decision?: string;
        reviewer_id?: string;
      };
      if (!response.ok || payload.ok === false) {
        throw new Error(payload.error ?? "Unable to record human review");
      }
      setSuccess(
        `Review recorded by ${payload.reviewer_id ?? reviewerId}. Final decision: ${payload.final_decision ?? finalDecision}. Refreshing the application snapshot.`
      );
      startTransition(() => {
        router.refresh();
      });
    } catch (cause) {
      setSuccess(null);
      setError(cause instanceof Error ? cause.message : "Unable to record human review");
    } finally {
      setSubmitting(false);
    }
  }

  if (!reviewPending) {
    return (
      <div className="stack-md">
        <div>
          <p className="eyebrow">Manual Review Action</p>
          <h3>Review Not Pending</h3>
          <p className="muted-copy">This workspace opens the review form only when the application is awaiting a reviewer decision.</p>
        </div>
      </div>
    );
  }

  return (
    <div className="stack-lg">
      <div>
        <p className="eyebrow">Manual Review Action</p>
        <h3>Resolve Pending Review</h3>
        <p className="muted-copy">Confirm the recommendation or override it through the normal human-review command path.</p>
        {override ? (
          <p className="muted-copy">This choice changes the automated recommendation, so an override reason is required.</p>
        ) : null}
      </div>

      <div className="toolbar-grid">
        <label className="field-shell">
          <span className="field-label">Reviewer</span>
          <input className="text-input" value={reviewerId} onChange={(event) => setReviewerId(event.target.value)} disabled={submitting} />
        </label>
        <label className="field-shell">
          <span className="field-label">Final decision</span>
          <select className="text-input" value={finalDecision} onChange={(event) => setFinalDecision(event.target.value as "APPROVE" | "DECLINE")} disabled={submitting}>
            <option value="APPROVE">Approve</option>
            <option value="DECLINE">Decline</option>
          </select>
        </label>
      </div>

      {finalDecision === "APPROVE" ? (
        <div className="toolbar-grid">
          <label className="field-shell">
            <span className="field-label">Approved amount</span>
            <input className="text-input" value={approvedAmount} onChange={(event) => setApprovedAmount(event.target.value)} disabled={submitting} />
          </label>
          <label className="field-shell">
            <span className="field-label">Interest rate %</span>
            <input className="text-input" value={interestRate} onChange={(event) => setInterestRate(event.target.value)} disabled={submitting} />
          </label>
          <label className="field-shell">
            <span className="field-label">Term months</span>
            <input className="text-input" value={termMonths} onChange={(event) => setTermMonths(event.target.value)} disabled={submitting} />
          </label>
        </div>
      ) : (
        <label className="field-shell">
          <span className="field-label">Decline reasons</span>
          <input
            className="text-input"
            value={declineReasons}
            onChange={(event) => setDeclineReasons(event.target.value)}
            placeholder="Comma-separated reasons"
            disabled={submitting}
          />
        </label>
      )}

      <label className="field-shell">
        <span className="field-label">Override reason</span>
        <input
          className="text-input"
          value={overrideReason}
          onChange={(event) => setOverrideReason(event.target.value)}
          placeholder={override ? "Explain why the recommendation changed" : "Optional when confirming the recommendation"}
          disabled={submitting}
        />
      </label>

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
        <button type="button" className="hero-button hero-button-primary" onClick={submit} disabled={submitting || !reviewerId.trim()}>
          {submitting ? "Saving review..." : "Record human review"}
        </button>
      </div>
    </div>
  );
}

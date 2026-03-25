const STREAM_LABELS: Record<string, string> = {
  loan: "Loan",
  docpkg: "Documents",
  credit: "Credit",
  fraud: "Fraud",
  compliance: "Compliance",
  agent: "Agent Session",
  audit: "Audit"
};

const STATE_LABELS: Record<string, string> = {
  NEW: "New",
  SUBMITTED: "Submitted",
  DOCUMENTS_PENDING: "Awaiting documents",
  DOCUMENTS_UPLOADED: "Documents received",
  DOCUMENTS_PROCESSED: "Ready for credit",
  AWAITING_ANALYSIS: "Credit in progress",
  ANALYSIS_COMPLETE: "Ready for compliance",
  COMPLIANCE_REVIEW: "Compliance in progress",
  PENDING_DECISION: "Awaiting recommendation",
  APPROVED_PENDING_HUMAN: "Recommended approve",
  DECLINED_PENDING_HUMAN: "Recommended decline",
  PENDING_HUMAN_REVIEW: "Awaiting review",
  FINAL_APPROVED: "Approved",
  FINAL_DECLINED: "Declined",
  DECLINED_COMPLIANCE: "Declined - compliance"
};

const COMPLIANCE_LABELS: Record<string, string> = {
  CLEAR: "Clear",
  CONDITIONAL: "Conditional",
  BLOCKED: "Blocked"
};

const ORIGIN_LABELS = {
  seeded: "Seeded portfolio",
  live: "Live workflow"
} as const;

export function formatCurrency(value: string | null): string {
  if (!value) {
    return "Not recorded";
  }

  const numeric = Number(value);
  if (Number.isNaN(numeric)) {
    return value;
  }

  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    maximumFractionDigits: numeric % 1 === 0 ? 0 : 2
  }).format(numeric);
}

export function formatDateTime(value: string | null): string {
  if (!value) {
    return "Not recorded";
  }

  return new Intl.DateTimeFormat("en-US", {
    dateStyle: "medium",
    timeStyle: "short"
  }).format(new Date(value));
}

export function streamFamilyLabel(family: string): string {
  return STREAM_LABELS[family] ?? family;
}

export function formatCompactNumber(value: number | null): string {
  if (value === null || Number.isNaN(value)) {
    return "Not recorded";
  }

  return new Intl.NumberFormat("en-US", {
    notation: "compact",
    maximumFractionDigits: 1
  }).format(value);
}

export function formatStateLabel(state: string | null): string {
  if (!state) {
    return "Not recorded";
  }

  return STATE_LABELS[state] ?? state.replaceAll("_", " ");
}

export function formatDecisionLabel(decision: string | null, state?: string | null): string {
  if (!decision) {
    return "Pending";
  }

  if (decision === "APPROVE") {
    return state === "FINAL_APPROVED" ? "Approved" : "Recommended approve";
  }

  if (decision === "DECLINE") {
    return state === "FINAL_DECLINED" || state === "DECLINED_COMPLIANCE" ? "Declined" : "Recommended decline";
  }

  if (decision === "REFER") {
    return "Refer to review";
  }

  return decision;
}

export function formatComplianceLabel(status: string | null): string {
  if (!status) {
    return "In progress";
  }

  return COMPLIANCE_LABELS[status] ?? status.replaceAll("_", " ");
}

export function formatOriginLabel(origin: "seeded" | "live"): string {
  return ORIGIN_LABELS[origin] ?? origin;
}

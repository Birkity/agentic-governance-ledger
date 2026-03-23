const STREAM_LABELS: Record<string, string> = {
  loan: "Loan",
  docpkg: "Documents",
  credit: "Credit",
  fraud: "Fraud",
  compliance: "Compliance",
  agent: "Agent Session",
  audit: "Audit"
};

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

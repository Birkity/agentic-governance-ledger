import type { ApplicationListItem } from "./ledger-data";

export type QueueLane = "human" | "open" | "approved" | "declined" | "all";

export const QUEUE_ORDER: QueueLane[] = ["human", "open", "approved", "declined", "all"];

export const QUEUE_LABELS: Record<QueueLane, string> = {
  human: "Human Review",
  open: "In Flight",
  approved: "Approved",
  declined: "Declined",
  all: "All Applications"
};

export const QUEUE_DESCRIPTIONS: Record<QueueLane, string> = {
  human: "Applications that need, or already passed through, manual intervention.",
  open: "Active work in progress before a final outcome is locked.",
  approved: "Applications that reached a final approval outcome.",
  declined: "Applications that ended in decline, including compliance hard blocks.",
  all: "A searchable view across the full application ledger."
};

export function isQueueLane(value: string): value is QueueLane {
  return QUEUE_ORDER.includes(value as QueueLane);
}

export function matchesQueue(item: ApplicationListItem, lane: QueueLane): boolean {
  if (lane === "human") {
    return item.hasHumanReview || item.state.includes("HUMAN");
  }
  if (lane === "open") {
    return !item.state.startsWith("FINAL") && !item.state.startsWith("DECLINED_");
  }
  if (lane === "approved") {
    return item.state === "FINAL_APPROVED";
  }
  if (lane === "declined") {
    return item.state === "FINAL_DECLINED" || item.state === "DECLINED_COMPLIANCE";
  }
  return true;
}

export function getQueueCounts(applications: ApplicationListItem[]): Record<QueueLane, number> {
  return {
    human: applications.filter((item) => matchesQueue(item, "human")).length,
    open: applications.filter((item) => matchesQueue(item, "open")).length,
    approved: applications.filter((item) => matchesQueue(item, "approved")).length,
    declined: applications.filter((item) => matchesQueue(item, "declined")).length,
    all: applications.length
  };
}

export function queueHref(lane: QueueLane): string {
  return `/queues/${lane}`;
}

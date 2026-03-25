import { revalidatePath } from "next/cache";

import { runWorkflowCommand } from "../../../../../lib/workflow-bridge";

export const dynamic = "force-dynamic";

export async function POST(
  request: Request,
  { params }: { params: Promise<{ applicationId: string }> }
) {
  try {
    const { applicationId } = await params;
    const body = (await request.json()) as {
      reviewerId?: string;
      finalDecision?: "APPROVE" | "DECLINE";
      override?: boolean;
      overrideReason?: string | null;
      approvedAmountUsd?: string | null;
      interestRatePct?: number | null;
      termMonths?: number | null;
      conditions?: string[];
      declineReasons?: string[];
      adverseActionCodes?: string[];
    };

    if (!body.reviewerId || !body.finalDecision) {
      return Response.json({ ok: false, error: "reviewerId and finalDecision are required" }, { status: 400 });
    }

    const args = [
      "record-human-review",
      "--application-id",
      applicationId,
      "--reviewer-id",
      body.reviewerId,
      "--final-decision",
      body.finalDecision
    ];
    if (body.override) {
      args.push("--override");
    }
    if (body.overrideReason) {
      args.push("--override-reason", body.overrideReason);
    }
    if (body.approvedAmountUsd) {
      args.push("--approved-amount-usd", body.approvedAmountUsd);
    }
    if (body.interestRatePct !== null && body.interestRatePct !== undefined) {
      args.push("--interest-rate-pct", String(body.interestRatePct));
    }
    if (body.termMonths !== null && body.termMonths !== undefined) {
      args.push("--term-months", String(body.termMonths));
    }
    args.push("--conditions-json", JSON.stringify(body.conditions ?? []));
    args.push("--decline-reasons-json", JSON.stringify(body.declineReasons ?? []));
    args.push("--adverse-action-codes-json", JSON.stringify(body.adverseActionCodes ?? []));

    const result = await runWorkflowCommand(args);
    revalidatePath("/");
    revalidatePath("/queues/human");
    revalidatePath("/queues/approved");
    revalidatePath("/queues/declined");
    revalidatePath(`/applications/${applicationId}`);
    return Response.json(result);
  } catch (error) {
    return Response.json(
      { ok: false, error: error instanceof Error ? error.message : "Unable to record review" },
      { status: 500 }
    );
  }
}

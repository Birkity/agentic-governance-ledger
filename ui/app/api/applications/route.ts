import { revalidatePath } from "next/cache";

import { runWorkflowCommand } from "../../../lib/workflow-bridge";

export const dynamic = "force-dynamic";

export async function POST(request: Request) {
  try {
    const body = (await request.json()) as {
      applicationId?: string;
      companyId?: string;
      phase?: "document" | "credit" | "fraud" | "compliance" | "decision" | "full";
      requestedAmountUsd?: string | null;
      autoFinalizeHumanReview?: boolean;
    };

    if (!body.companyId) {
      return Response.json({ ok: false, error: "companyId is required" }, { status: 400 });
    }

    const args = ["start-application", "--company-id", body.companyId, "--phase", body.phase ?? "full"];
    if (body.applicationId) {
      args.push("--application-id", body.applicationId);
    }
    if (body.requestedAmountUsd) {
      args.push("--requested-amount-usd", body.requestedAmountUsd);
    }
    if (body.autoFinalizeHumanReview) {
      args.push("--auto-finalize-human-review");
    }

    const result = await runWorkflowCommand(args);
    const applicationId = String(result.application_id ?? body.applicationId ?? "");
    revalidatePath("/");
    revalidatePath("/queues/open");
    revalidatePath("/queues/human");
    if (applicationId) {
      revalidatePath(`/applications/${applicationId}`);
    }
    return Response.json(result);
  } catch (error) {
    return Response.json(
      { ok: false, error: error instanceof Error ? error.message : "Unable to start application" },
      { status: 500 }
    );
  }
}

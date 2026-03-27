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
      companyId?: string;
      phase?: "document" | "credit" | "full";
      autoFinalizeHumanReview?: boolean;
    };

    if (!body.companyId) {
      return Response.json({ ok: false, error: "companyId is required" }, { status: 400 });
    }

    const args = [
      "continue-application",
      "--application-id",
      applicationId
    ];
    if (body.companyId) {
      args.push("--company-id", body.companyId);
    }
    if (body.autoFinalizeHumanReview) {
      args.push("--auto-finalize-human-review");
    }

    const result = await runWorkflowCommand(args);
    if (result.ok === false) {
      return Response.json(result, { status: 400 });
    }
    revalidatePath("/");
    revalidatePath("/queues/open");
    revalidatePath("/queues/human");
    revalidatePath("/queues/approved");
    revalidatePath("/queues/declined");
    revalidatePath(`/applications/${applicationId}`);
    revalidatePath(`/applications/${applicationId}/timeline`);
    revalidatePath(`/applications/${applicationId}/oversight`);
    revalidatePath(`/applications/${applicationId}/agents`);
    return Response.json(result);
  } catch (error) {
    return Response.json(
      { ok: false, error: error instanceof Error ? error.message : "Unable to continue the pipeline" },
      { status: 500 }
    );
  }
}

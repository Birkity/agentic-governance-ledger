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
      "start-application",
      "--application-id",
      applicationId,
      "--company-id",
      body.companyId,
      "--phase",
      body.phase ?? "full"
    ];
    if (body.autoFinalizeHumanReview) {
      args.push("--auto-finalize-human-review");
    }

    const result = await runWorkflowCommand(args);
    revalidatePath("/");
    revalidatePath("/queues/open");
    revalidatePath("/queues/human");
    revalidatePath(`/applications/${applicationId}`);
    return Response.json(result);
  } catch (error) {
    return Response.json(
      { ok: false, error: error instanceof Error ? error.message : "Unable to continue the pipeline" },
      { status: 500 }
    );
  }
}

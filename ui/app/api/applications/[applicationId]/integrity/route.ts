import { revalidatePath } from "next/cache";

import { runWorkflowCommand } from "../../../../../lib/workflow-bridge";

export const dynamic = "force-dynamic";

export async function POST(
  request: Request,
  { params }: { params: Promise<{ applicationId: string }> }
) {
  try {
    const { applicationId } = await params;
    const _ = await request.text();
    const result = await runWorkflowCommand(["run-integrity", "--application-id", applicationId]);
    revalidatePath("/");
    revalidatePath(`/applications/${applicationId}`);
    revalidatePath(`/applications/${applicationId}/oversight`);
    return Response.json(result);
  } catch (error) {
    return Response.json(
      { ok: false, error: error instanceof Error ? error.message : "Unable to run integrity check" },
      { status: 500 }
    );
  }
}

import { existsSync } from "fs";
import { promises as fs } from "fs";
import path from "path";

export const dynamic = "force-dynamic";

const MIME_TYPES: Record<string, string> = {
  ".csv": "text/csv; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".pdf": "application/pdf",
  ".txt": "text/plain; charset=utf-8",
  ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
};

function resolveProjectRoot(): string {
  const candidates = [process.cwd(), path.resolve(process.cwd(), ".."), path.resolve(process.cwd(), "../..")];
  for (const candidate of candidates) {
    if (existsSync(path.join(candidate, "data")) && existsSync(path.join(candidate, "documents"))) {
      return candidate;
    }
  }
  return path.resolve(process.cwd(), "..");
}

const PROJECT_ROOT = resolveProjectRoot();

const ROOTS: Record<string, string> = {
  artifacts: path.join(PROJECT_ROOT, "artifacts"),
  documents: path.join(PROJECT_ROOT, "documents")
};

function safeResolve(base: string, segments: string[]): string | null {
  const resolved = path.resolve(base, ...segments);
  return resolved.startsWith(base) ? resolved : null;
}

export async function GET(
  _request: Request,
  { params }: { params: { segments: string[] } }
) {
  const [scope, ...rest] = params.segments ?? [];
  if (!scope || rest.length === 0) {
    return new Response("Missing asset path", { status: 400 });
  }

  const base = ROOTS[scope];
  if (!base) {
    return new Response("Unknown asset scope", { status: 404 });
  }

  const target = safeResolve(base, rest);
  if (!target) {
    return new Response("Invalid asset path", { status: 403 });
  }

  try {
    const stat = await fs.stat(target);
    if (!stat.isFile()) {
      return new Response("Asset not found", { status: 404 });
    }

    const body = await fs.readFile(target);
    const extension = path.extname(target).toLowerCase();
    return new Response(body, {
      headers: {
        "Content-Length": String(stat.size),
        "Content-Type": MIME_TYPES[extension] ?? "application/octet-stream"
      }
    });
  } catch {
    return new Response("Asset not found", { status: 404 });
  }
}

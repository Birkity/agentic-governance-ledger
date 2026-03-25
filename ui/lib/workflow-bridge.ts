import "server-only";

import { execFile } from "child_process";
import { existsSync } from "fs";
import path from "path";
import { promisify } from "util";

const execFileAsync = promisify(execFile);

function resolveProjectRoot(): string {
  const candidates = [process.cwd(), path.resolve(process.cwd(), ".."), path.resolve(process.cwd(), "../..")];
  for (const candidate of candidates) {
    if (existsSync(path.join(candidate, "scripts", "ui_workflow.py")) && existsSync(path.join(candidate, "data"))) {
      return candidate;
    }
  }
  return path.resolve(process.cwd(), "..");
}

const PROJECT_ROOT = resolveProjectRoot();

function resolvePythonExecutable(): string {
  const candidates = [
    path.join(PROJECT_ROOT, ".venv", "Scripts", "python.exe"),
    path.join(PROJECT_ROOT, ".venv", "bin", "python"),
    "python"
  ];
  const found = candidates.find((candidate) => candidate === "python" || existsSync(candidate));
  return found ?? "python";
}

function scriptPath(): string {
  return path.join(PROJECT_ROOT, "scripts", "ui_workflow.py");
}

export async function runWorkflowCommand(args: string[]): Promise<Record<string, unknown>> {
  const dbUrl = process.env.DATABASE_URL;
  const commandArgs = [scriptPath()];
  if (dbUrl) {
    commandArgs.push("--db-url", dbUrl);
  }
  commandArgs.push(...args);

  const { stdout, stderr } = await execFileAsync(resolvePythonExecutable(), commandArgs, {
    cwd: PROJECT_ROOT,
    maxBuffer: 10 * 1024 * 1024
  });

  const output = stdout.trim() || stderr.trim();
  if (!output) {
    throw new Error("Workflow command returned no output");
  }
  try {
    return JSON.parse(output) as Record<string, unknown>;
  } catch {
    throw new Error(output);
  }
}

export { PROJECT_ROOT };

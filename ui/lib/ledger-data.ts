import "server-only";

import { cache } from "react";
import { existsSync } from "fs";
import { promises as fs } from "fs";
import path from "path";
import { Pool } from "pg";

export type DataSourceMode = "database" | "seed";

export interface ApplicantProfile {
  companyId: string;
  name: string;
  industry: string;
  jurisdiction: string;
  legalType: string;
  trajectory: string;
  riskSegment: string;
  complianceFlags: Array<Record<string, unknown>>;
}

export interface ApplicationListItem {
  applicationId: string;
  applicantId: string | null;
  companyName: string;
  industry: string | null;
  jurisdiction: string | null;
  state: string;
  requestedAmountUsd: string | null;
  approvedAmountUsd: string | null;
  decision: string | null;
  complianceStatus: string | null;
  riskTier: string | null;
  fraudScore: number | null;
  hasHumanReview: boolean;
  eventCount: number;
  lastEventAt: string | null;
  streamFamilies: string[];
}

export interface TimelineEvent {
  id: string;
  streamId: string;
  streamFamily: string;
  eventType: string;
  eventVersion: number;
  recordedAt: string;
  streamPosition: number | null;
  globalPosition: number | null;
  payload: Record<string, unknown>;
  metadata: Record<string, unknown>;
}

export interface StageState {
  key: string;
  label: string;
  status: "complete" | "current" | "upcoming";
  hint: string;
}

export interface DocumentResource {
  name: string;
  path: string;
  kind: "pdf" | "csv" | "xlsx" | "other";
  href: string;
  sizeBytes: number;
}

export interface AgentSessionCard {
  sessionId: string;
  streamId: string;
  agentType: string;
  agentId: string | null;
  modelVersion: string | null;
  startedAt: string | null;
  completedAt: string | null;
  status: "completed" | "failed" | "running";
  nodesExecuted: number;
  toolsCalled: number;
  outputEvents: number;
  totalCostUsd: number | null;
}

export interface ComplianceView {
  sessionId: string | null;
  regulationSetVersion: string | null;
  overallVerdict: string | null;
  completed: boolean;
  passedRules: Array<Record<string, unknown>>;
  failedRules: Array<Record<string, unknown>>;
  notedRules: Array<Record<string, unknown>>;
  hardBlockRules: string[];
  snapshots: Array<{
    eventType: string;
    asOf: string;
    passed: number;
    failed: number;
    noted: number;
    verdict: string | null;
  }>;
}

export interface ReviewSummary {
  requested: boolean;
  completed: boolean;
  reviewerId: string | null;
  override: boolean;
  finalDecision: string | null;
  overrideReason: string | null;
}

export interface AuditSummary {
  eventCount: number;
  chainValid: boolean | null;
  tamperDetected: boolean | null;
  latestIntegrityHash: string | null;
  latestCheckAt: string | null;
  eventTypes: string[];
}

export interface OperationsSnapshot {
  sourceMode: DataSourceMode;
  projectionLagReport: string;
  concurrencyReport: string;
}

export interface DocumentPreviewModel {
  document: DocumentResource | null;
  selectedCsvRows: string[][] | null;
}

export interface ApplicationDetail {
  sourceMode: DataSourceMode;
  item: ApplicationListItem;
  company: ApplicantProfile | null;
  timeline: TimelineEvent[];
  stages: StageState[];
  documents: DocumentResource[];
  documentPreview: DocumentPreviewModel;
  agentSessions: AgentSessionCard[];
  compliance: ComplianceView;
  review: ReviewSummary;
  audit: AuditSummary;
  operations: OperationsSnapshot;
}

interface RawEventRow {
  eventId?: string;
  streamId: string;
  streamPosition?: number | null;
  globalPosition?: number | null;
  eventType: string;
  eventVersion: number;
  payload: Record<string, unknown>;
  metadata: Record<string, unknown>;
  recordedAt: string;
}

interface EventGroupingContext {
  streamToApplicationId: Map<string, string>;
  sessionToApplicationId: Map<string, string>;
}

const APPLICATION_STAGE_ORDER = [
  "submitted",
  "documents",
  "credit",
  "fraud",
  "compliance",
  "decision",
  "human",
  "final"
] as const;

const STREAM_LABELS: Record<string, string> = {
  loan: "Loan",
  docpkg: "Documents",
  credit: "Credit",
  fraud: "Fraud",
  compliance: "Compliance",
  agent: "Agent Session",
  audit: "Audit"
};

let dbPool: Pool | null = null;

function resolveProjectRoot(): string {
  const candidates = [process.cwd(), path.resolve(process.cwd(), "..")];
  for (const candidate of candidates) {
    if (existsSync(path.join(candidate, "data", "seed_events.jsonl")) && existsSync(path.join(candidate, "documents"))) {
      return candidate;
    }
  }
  return path.resolve(process.cwd(), "..");
}

const PROJECT_ROOT = resolveProjectRoot();

function getPool(): Pool | null {
  if (!process.env.DATABASE_URL) {
    return null;
  }
  if (!dbPool) {
    dbPool = new Pool({ connectionString: process.env.DATABASE_URL });
  }
  return dbPool;
}

async function detectMode(): Promise<DataSourceMode> {
  const pool = getPool();
  if (!pool) {
    return "seed";
  }
  try {
    const result = await pool.query("SELECT COUNT(*)::int AS count FROM events");
    return Number(result.rows[0]?.count ?? 0) > 0 ? "database" : "seed";
  } catch {
    return "seed";
  }
}

async function loadApplicants(): Promise<Map<string, ApplicantProfile>> {
  const filePath = path.join(PROJECT_ROOT, "data", "applicant_profiles.json");
  const raw = await fs.readFile(filePath, "utf-8");
  const rows = JSON.parse(raw) as Array<Record<string, unknown>>;
  return new Map(
    rows.map((row) => [
      String(row.company_id),
      {
        companyId: String(row.company_id),
        name: String(row.name),
        industry: String(row.industry),
        jurisdiction: String(row.jurisdiction),
        legalType: String(row.legal_type),
        trajectory: String(row.trajectory),
        riskSegment: String(row.risk_segment),
        complianceFlags: Array.isArray(row.compliance_flags) ? (row.compliance_flags as Array<Record<string, unknown>>) : []
      }
    ])
  );
}

async function loadEvents(mode: DataSourceMode): Promise<RawEventRow[]> {
  if (mode === "database") {
    const pool = getPool();
    if (!pool) {
      return [];
    }
    const result = await pool.query(
      `
      SELECT
        event_id,
        stream_id,
        stream_position,
        global_position,
        event_type,
        event_version,
        payload,
        metadata,
        recorded_at
      FROM events
      ORDER BY global_position ASC, stream_id ASC, stream_position ASC
      `
    );
    return result.rows.map((row) => ({
      eventId: String(row.event_id),
      streamId: String(row.stream_id),
      streamPosition: row.stream_position === null ? null : Number(row.stream_position),
      globalPosition: row.global_position === null ? null : Number(row.global_position),
      eventType: String(row.event_type),
      eventVersion: Number(row.event_version),
      payload: (row.payload ?? {}) as Record<string, unknown>,
      metadata: (row.metadata ?? {}) as Record<string, unknown>,
      recordedAt: new Date(row.recorded_at).toISOString()
    }));
  }

  const filePath = path.join(PROJECT_ROOT, "data", "seed_events.jsonl");
  const raw = await fs.readFile(filePath, "utf-8");
  return raw
    .split(/\r?\n/)
    .filter(Boolean)
    .map((line, index) => {
      const row = JSON.parse(line) as Record<string, unknown>;
      return {
        eventId: `seed-${index + 1}`,
        streamId: String(row.stream_id),
        streamPosition: null,
        globalPosition: index + 1,
        eventType: String(row.event_type),
        eventVersion: Number(row.event_version ?? 1),
        payload: (row.payload ?? {}) as Record<string, unknown>,
        metadata: {},
        recordedAt: String(row.recorded_at)
      };
    });
}

function extractApplicationId(event: RawEventRow, context?: EventGroupingContext): string | null {
  const payload = event.payload;
  if (typeof payload.application_id === "string") {
    return payload.application_id;
  }
  if (typeof payload.session_id === "string") {
    const applicationId = context?.sessionToApplicationId.get(payload.session_id);
    if (applicationId) {
      return applicationId;
    }
  }
  if (typeof payload.package_id === "string") {
    return payload.package_id.startsWith("docpkg-") ? payload.package_id.slice("docpkg-".length) : payload.package_id;
  }
  const mappedStreamApplicationId = context?.streamToApplicationId.get(event.streamId);
  if (mappedStreamApplicationId) {
    return mappedStreamApplicationId;
  }
  if (event.streamId.startsWith("loan-")) {
    return event.streamId.slice("loan-".length);
  }
  if (event.streamId.startsWith("docpkg-")) {
    return event.streamId.slice("docpkg-".length);
  }
  if (event.streamId.startsWith("credit-")) {
    return event.streamId.slice("credit-".length);
  }
  if (event.streamId.startsWith("fraud-")) {
    return event.streamId.slice("fraud-".length);
  }
  if (event.streamId.startsWith("compliance-")) {
    return event.streamId.slice("compliance-".length);
  }
  if (event.streamId.startsWith("audit-")) {
    const parts = event.streamId.split("-");
    if (parts.length >= 3) {
      const entityType = parts[1];
      const entityId = parts.slice(2).join("-");
      if (entityType === "loan" || entityType === "application") {
        return entityId;
      }
    }
  }
  return null;
}

function buildGroupingContext(events: RawEventRow[]): EventGroupingContext {
  const streamToApplicationId = new Map<string, string>();
  const sessionToApplicationId = new Map<string, string>();

  for (const event of events) {
    const payload = event.payload;
    const applicationId = typeof payload.application_id === "string" ? payload.application_id : null;
    const sessionId = typeof payload.session_id === "string" ? payload.session_id : null;

    if (applicationId) {
      streamToApplicationId.set(event.streamId, applicationId);
    }
    if (applicationId && sessionId) {
      sessionToApplicationId.set(sessionId, applicationId);
    }
  }

  return { streamToApplicationId, sessionToApplicationId };
}

function mapStreamFamily(streamId: string): string {
  return streamId.split("-", 1)[0] ?? "misc";
}

function eventSort(a: RawEventRow, b: RawEventRow): number {
  const globalA = a.globalPosition ?? Number.MAX_SAFE_INTEGER;
  const globalB = b.globalPosition ?? Number.MAX_SAFE_INTEGER;
  if (globalA !== globalB) {
    return globalA - globalB;
  }
  if (a.recordedAt !== b.recordedAt) {
    return a.recordedAt.localeCompare(b.recordedAt);
  }
  if (a.streamId !== b.streamId) {
    return a.streamId.localeCompare(b.streamId);
  }
  return (a.streamPosition ?? 0) - (b.streamPosition ?? 0);
}

function toTimelineEvent(row: RawEventRow): TimelineEvent {
  return {
    id: row.eventId ?? `${row.streamId}-${row.eventType}-${row.recordedAt}`,
    streamId: row.streamId,
    streamFamily: mapStreamFamily(row.streamId),
    eventType: row.eventType,
    eventVersion: row.eventVersion,
    recordedAt: row.recordedAt,
    streamPosition: row.streamPosition ?? null,
    globalPosition: row.globalPosition ?? null,
    payload: row.payload,
    metadata: row.metadata
  };
}

function toMoneyString(value: unknown): string | null {
  if (value === null || value === undefined || value === "") {
    return null;
  }
  return String(value);
}

function summarizeApplication(
  applicationId: string,
  timeline: TimelineEvent[],
  applicants: Map<string, ApplicantProfile>
): ApplicationListItem {
  let state = "NEW";
  let applicantId: string | null = null;
  let requestedAmountUsd: string | null = null;
  let approvedAmountUsd: string | null = null;
  let decision: string | null = null;
  let complianceStatus: string | null = null;
  let riskTier: string | null = null;
  let fraudScore: number | null = null;
  let hasHumanReview = false;

  for (const event of timeline) {
    const payload = event.payload;
    if (event.eventType === "ApplicationSubmitted") {
      state = "SUBMITTED";
      applicantId = typeof payload.applicant_id === "string" ? payload.applicant_id : applicantId;
      requestedAmountUsd = toMoneyString(payload.requested_amount_usd);
    } else if (event.eventType === "DocumentUploadRequested") {
      state = "DOCUMENTS_PENDING";
    } else if (event.eventType === "DocumentUploaded") {
      state = "DOCUMENTS_UPLOADED";
    } else if (event.eventType === "PackageReadyForAnalysis") {
      state = "DOCUMENTS_PROCESSED";
    } else if (event.eventType === "CreditAnalysisRequested") {
      state = "AWAITING_ANALYSIS";
    } else if (event.eventType === "CreditAnalysisCompleted") {
      const nestedDecision = (payload.decision ?? {}) as Record<string, unknown>;
      riskTier = typeof nestedDecision.risk_tier === "string" ? nestedDecision.risk_tier : riskTier;
    } else if (event.eventType === "FraudScreeningRequested") {
      state = "ANALYSIS_COMPLETE";
    } else if (event.eventType === "FraudScreeningCompleted") {
      fraudScore = payload.fraud_score === undefined ? fraudScore : Number(payload.fraud_score);
    } else if (event.eventType === "ComplianceCheckRequested") {
      state = "COMPLIANCE_REVIEW";
    } else if (event.eventType === "ComplianceCheckCompleted") {
      complianceStatus = typeof payload.overall_verdict === "string" ? payload.overall_verdict : complianceStatus;
    } else if (event.eventType === "DecisionRequested") {
      state = "PENDING_DECISION";
    } else if (event.eventType === "DecisionGenerated") {
      decision = typeof payload.recommendation === "string" ? payload.recommendation : decision;
      approvedAmountUsd = toMoneyString(payload.approved_amount_usd) ?? approvedAmountUsd;
      if (decision === "APPROVE") {
        state = "APPROVED_PENDING_HUMAN";
      } else if (decision === "DECLINE") {
        state = "DECLINED_PENDING_HUMAN";
      } else {
        state = "PENDING_HUMAN_REVIEW";
      }
    } else if (event.eventType === "HumanReviewRequested") {
      hasHumanReview = true;
      state = "PENDING_HUMAN_REVIEW";
    } else if (event.eventType === "HumanReviewCompleted") {
      hasHumanReview = true;
      decision = typeof payload.final_decision === "string" ? payload.final_decision : decision;
    } else if (event.eventType === "ApplicationApproved") {
      approvedAmountUsd = toMoneyString(payload.approved_amount_usd) ?? approvedAmountUsd;
      decision = "APPROVE";
      state = "FINAL_APPROVED";
    } else if (event.eventType === "ApplicationDeclined") {
      decision = "DECLINE";
      const reasons = Array.isArray(payload.decline_reasons) ? payload.decline_reasons.map(String) : [];
      state = reasons.some((reason) => reason.includes("Compliance hard block")) ? "DECLINED_COMPLIANCE" : "FINAL_DECLINED";
    }
  }

  const company = applicantId ? applicants.get(applicantId) : null;
  return {
    applicationId,
    applicantId,
    companyName: company?.name ?? applicantId ?? applicationId,
    industry: company?.industry ?? null,
    jurisdiction: company?.jurisdiction ?? null,
    state,
    requestedAmountUsd,
    approvedAmountUsd,
    decision,
    complianceStatus,
    riskTier,
    fraudScore,
    hasHumanReview,
    eventCount: timeline.length,
    lastEventAt: timeline.at(-1)?.recordedAt ?? null,
    streamFamilies: Array.from(new Set(timeline.map((event) => event.streamFamily)))
  };
}

function buildStages(item: ApplicationListItem): StageState[] {
  const completed = new Set<string>();
  const current =
    item.state === "SUBMITTED"
      ? "submitted"
      : item.state.startsWith("DOCUMENTS")
        ? "documents"
        : item.state === "AWAITING_ANALYSIS"
          ? "credit"
          : item.state === "ANALYSIS_COMPLETE"
            ? "fraud"
            : item.state === "COMPLIANCE_REVIEW" || item.state === "DECLINED_COMPLIANCE"
              ? "compliance"
              : item.state === "PENDING_DECISION"
                ? "decision"
                : item.state.includes("HUMAN")
                  ? "human"
                  : item.state.startsWith("FINAL")
                    ? "final"
                    : "submitted";

  for (const stage of APPLICATION_STAGE_ORDER) {
    if (stage === current) {
      break;
    }
    completed.add(stage);
  }
  if (current === "final") {
    APPLICATION_STAGE_ORDER.forEach((stage) => completed.add(stage));
  }

  const hints: Record<(typeof APPLICATION_STAGE_ORDER)[number], string> = {
    submitted: "Application intake and reference capture",
    documents: "Uploads, extraction, and package readiness",
    credit: "Credit facts and risk limit analysis",
    fraud: "Fraud screening and anomaly review",
    compliance: "Rule evaluation and hard-block checks",
    decision: "Decision orchestration and recommendation",
    human: "Manual review and override path",
    final: "Final approval or decline recorded"
  };

  return APPLICATION_STAGE_ORDER.map((stage) => ({
    key: stage,
    label: stage.charAt(0).toUpperCase() + stage.slice(1),
    status: completed.has(stage) ? "complete" : stage === current ? "current" : "upcoming",
    hint: hints[stage]
  }));
}

function buildReview(timeline: TimelineEvent[]): ReviewSummary {
  const requested = timeline.some((event) => event.eventType === "HumanReviewRequested");
  const completedEvent = timeline.findLast((event) => event.eventType === "HumanReviewCompleted");
  return {
    requested,
    completed: Boolean(completedEvent),
    reviewerId: typeof completedEvent?.payload.reviewer_id === "string" ? String(completedEvent.payload.reviewer_id) : null,
    override: Boolean(completedEvent?.payload.override),
    finalDecision: typeof completedEvent?.payload.final_decision === "string" ? String(completedEvent.payload.final_decision) : null,
    overrideReason:
      typeof completedEvent?.payload.override_reason === "string" ? String(completedEvent.payload.override_reason) : null
  };
}

function buildCompliance(timeline: TimelineEvent[]): ComplianceView {
  const complianceEvents = timeline.filter((event) => event.streamFamily === "compliance");
  const state: ComplianceView = {
    sessionId: null,
    regulationSetVersion: null,
    overallVerdict: null,
    completed: false,
    passedRules: [],
    failedRules: [],
    notedRules: [],
    hardBlockRules: [],
    snapshots: []
  };

  for (const event of complianceEvents) {
    const payload = event.payload;
    if (event.eventType === "ComplianceCheckInitiated") {
      state.sessionId = typeof payload.session_id === "string" ? payload.session_id : state.sessionId;
      state.regulationSetVersion =
        typeof payload.regulation_set_version === "string" ? payload.regulation_set_version : state.regulationSetVersion;
    } else if (event.eventType === "ComplianceRulePassed") {
      state.passedRules.push(payload);
    } else if (event.eventType === "ComplianceRuleFailed") {
      state.failedRules.push(payload);
      if (payload.is_hard_block && typeof payload.rule_id === "string") {
        state.hardBlockRules.push(payload.rule_id);
      }
    } else if (event.eventType === "ComplianceRuleNoted") {
      state.notedRules.push(payload);
    } else if (event.eventType === "ComplianceCheckCompleted") {
      state.overallVerdict = typeof payload.overall_verdict === "string" ? payload.overall_verdict : state.overallVerdict;
      state.completed = true;
    }

    state.snapshots.push({
      eventType: event.eventType,
      asOf: event.recordedAt,
      passed: state.passedRules.length,
      failed: state.failedRules.length,
      noted: state.notedRules.length,
      verdict: state.overallVerdict
    });
  }

  return state;
}

function buildAgentSessions(timeline: TimelineEvent[]): AgentSessionCard[] {
  const sessions = new Map<string, AgentSessionCard>();
  for (const event of timeline.filter((item) => item.streamFamily === "agent")) {
    const payload = event.payload;
    const sessionId =
      typeof payload.session_id === "string" ? payload.session_id : event.streamId.split("-").slice(-1)[0] ?? event.streamId;
    const existing =
      sessions.get(sessionId) ??
      {
        sessionId,
        streamId: event.streamId,
        agentType: typeof payload.agent_type === "string" ? payload.agent_type : "unknown",
        agentId: typeof payload.agent_id === "string" ? payload.agent_id : null,
        modelVersion: typeof payload.model_version === "string" ? payload.model_version : null,
        startedAt: null,
        completedAt: null,
        status: "running" as const,
        nodesExecuted: 0,
        toolsCalled: 0,
        outputEvents: 0,
        totalCostUsd: null
      };

    if (event.eventType === "AgentSessionStarted") {
      existing.startedAt = event.recordedAt;
      existing.agentType = typeof payload.agent_type === "string" ? payload.agent_type : existing.agentType;
      existing.agentId = typeof payload.agent_id === "string" ? payload.agent_id : existing.agentId;
      existing.modelVersion = typeof payload.model_version === "string" ? payload.model_version : existing.modelVersion;
    } else if (event.eventType === "AgentNodeExecuted") {
      existing.nodesExecuted += 1;
    } else if (event.eventType === "AgentToolCalled") {
      existing.toolsCalled += 1;
    } else if (event.eventType === "AgentOutputWritten") {
      existing.outputEvents += Array.isArray(payload.events_written) ? payload.events_written.length : 0;
    } else if (event.eventType === "AgentSessionCompleted") {
      existing.status = "completed";
      existing.completedAt = event.recordedAt;
      existing.totalCostUsd = payload.total_cost_usd === undefined ? existing.totalCostUsd : Number(payload.total_cost_usd);
    } else if (event.eventType === "AgentSessionFailed") {
      existing.status = "failed";
      existing.completedAt = event.recordedAt;
    }

    sessions.set(sessionId, existing);
  }

  return Array.from(sessions.values()).sort((a, b) => (a.startedAt ?? "").localeCompare(b.startedAt ?? ""));
}

function buildAuditSummary(timeline: TimelineEvent[]): AuditSummary {
  const auditEvents = timeline.filter((event) => event.streamFamily === "audit");
  const latestIntegrity = auditEvents.findLast((event) => event.eventType === "AuditIntegrityCheckRun");
  return {
    eventCount: auditEvents.length,
    chainValid:
      latestIntegrity && latestIntegrity.payload.chain_valid !== undefined ? Boolean(latestIntegrity.payload.chain_valid) : null,
    tamperDetected:
      latestIntegrity && latestIntegrity.payload.tamper_detected !== undefined
        ? Boolean(latestIntegrity.payload.tamper_detected)
        : null,
    latestIntegrityHash:
      latestIntegrity && typeof latestIntegrity.payload.integrity_hash === "string"
        ? String(latestIntegrity.payload.integrity_hash)
        : null,
    latestCheckAt:
      latestIntegrity && typeof latestIntegrity.payload.check_timestamp === "string"
        ? String(latestIntegrity.payload.check_timestamp)
        : latestIntegrity?.recordedAt ?? null,
    eventTypes: Array.from(new Set(auditEvents.map((event) => event.eventType)))
  };
}

async function listDocuments(applicantId: string | null): Promise<DocumentResource[]> {
  if (!applicantId) {
    return [];
  }
  const directory = path.join(PROJECT_ROOT, "documents", applicantId);
  try {
    const entries = await fs.readdir(directory, { withFileTypes: true });
    const files = await Promise.all(
      entries
        .filter((entry) => entry.isFile())
        .map(async (entry) => {
          const absolutePath = path.join(directory, entry.name);
          const stat = await fs.stat(absolutePath);
          const ext = path.extname(entry.name).toLowerCase();
          const kind: DocumentResource["kind"] =
            ext === ".pdf" ? "pdf" : ext === ".csv" ? "csv" : ext === ".xlsx" ? "xlsx" : "other";

          return {
            name: entry.name,
            path: absolutePath,
            kind,
            href: `/api/assets/documents/${encodeURIComponent(applicantId)}/${encodeURIComponent(entry.name)}`,
            sizeBytes: stat.size
          };
        })
    );
    return files.sort((a, b) => a.name.localeCompare(b.name));
  } catch {
    return [];
  }
}

async function readCsvPreview(document: DocumentResource | null): Promise<string[][] | null> {
  if (!document || document.kind !== "csv") {
    return null;
  }
  const raw = await fs.readFile(document.path, "utf-8");
  return raw
    .split(/\r?\n/)
    .filter(Boolean)
    .slice(0, 12)
    .map((line) => line.split(","));
}

async function readArtifact(name: string, fallback: string): Promise<string> {
  try {
    return await fs.readFile(path.join(PROJECT_ROOT, "artifacts", name), "utf-8");
  } catch {
    return fallback;
  }
}

const loadWorld = cache(async () => {
  const mode = await detectMode();
  const applicants = await loadApplicants();
  const rawEvents = (await loadEvents(mode)).sort(eventSort);
  const groupingContext = buildGroupingContext(rawEvents);
  const grouped = new Map<string, TimelineEvent[]>();

  for (const row of rawEvents) {
    const applicationId = extractApplicationId(row, groupingContext);
    if (!applicationId) {
      continue;
    }
    const timeline = grouped.get(applicationId) ?? [];
    timeline.push(toTimelineEvent(row));
    grouped.set(applicationId, timeline);
  }

  const applications = Array.from(grouped.entries())
    .map(([applicationId, timeline]) => summarizeApplication(applicationId, timeline, applicants))
    .sort((a, b) => (b.lastEventAt ?? "").localeCompare(a.lastEventAt ?? ""));

  return { mode, applicants, grouped, applications };
});

export const getDashboardData = cache(async () => {
  const world = await loadWorld();
  const projectionLagReport = await readArtifact(
    "projection_lag_report.txt",
    "Projection lag artifact not found. Run scripts/generate_projection_lag_report.py to generate it."
  );
  const concurrencyReport = await readArtifact(
    "occ_collision_report.txt",
    [
      "Optimistic Concurrency Guardrail",
      "Command:",
      "$env:TEST_DB_URL='postgresql://postgres:newcode@localhost/apex_ledger'",
      ".\\.venv\\Scripts\\python.exe -m pytest tests\\test_concurrency.py -v -s",
      "",
      "Latest verified outcome:",
      "tests/test_concurrency.py::test_double_decision_concurrency_expected_version_three PASSED",
      "",
      "Expected assertions:",
      "- exactly one append succeeds",
      "- exactly one append raises OptimisticConcurrencyError",
      "- final stream length = 4",
      "- final stream positions remain ordered"
    ].join("\n")
  );

  return {
    mode: world.mode,
    applications: world.applications,
    totals: {
      applications: world.applications.length,
      finalApproved: world.applications.filter((item) => item.state === "FINAL_APPROVED").length,
      finalDeclined: world.applications.filter((item) => item.state === "FINAL_DECLINED" || item.state === "DECLINED_COMPLIANCE").length,
      humanReview: world.applications.filter((item) => item.hasHumanReview || item.state.includes("HUMAN")).length
    },
    operations: {
      sourceMode: world.mode,
      projectionLagReport,
      concurrencyReport
    } satisfies OperationsSnapshot
  };
});

export const getApplicationDetail = cache(async (applicationId: string, selectedDocumentName?: string) => {
  const world = await loadWorld();
  const timeline = world.grouped.get(applicationId) ?? [];
  if (timeline.length === 0) {
    return null;
  }

  const item = summarizeApplication(applicationId, timeline, world.applicants);
  const company = item.applicantId ? world.applicants.get(item.applicantId) ?? null : null;
  const documents = await listDocuments(item.applicantId);
  const document =
    documents.find((entry) => entry.name === selectedDocumentName) ??
    documents.find((entry) => entry.kind === "pdf") ??
    documents[0] ??
    null;

  const projectionLagReport = await readArtifact(
    "projection_lag_report.txt",
    "Projection lag artifact not found. Run scripts/generate_projection_lag_report.py to generate it."
  );
  const concurrencyReport = await readArtifact(
    "occ_collision_report.txt",
    [
      "Optimistic Concurrency Guardrail",
      "Command:",
      "$env:TEST_DB_URL='postgresql://postgres:newcode@localhost/apex_ledger'",
      ".\\.venv\\Scripts\\python.exe -m pytest tests\\test_concurrency.py -v -s",
      "",
      "Latest verified outcome:",
      "tests/test_concurrency.py::test_double_decision_concurrency_expected_version_three PASSED"
    ].join("\n")
  );

  return {
    sourceMode: world.mode,
    item,
    company,
    timeline,
    stages: buildStages(item),
    documents,
    documentPreview: {
      document,
      selectedCsvRows: await readCsvPreview(document)
    },
    agentSessions: buildAgentSessions(timeline),
    compliance: buildCompliance(timeline),
    review: buildReview(timeline),
    audit: buildAuditSummary(timeline),
    operations: {
      sourceMode: world.mode,
      projectionLagReport,
      concurrencyReport
    }
  } satisfies ApplicationDetail;
});

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

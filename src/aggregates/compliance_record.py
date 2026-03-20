from __future__ import annotations

from dataclasses import dataclass, field

from src.models.events import ComplianceVerdict, DomainError, StoredEvent


NOTE_ONLY_RULES = {"REG-006"}


@dataclass
class ComplianceRecordAggregate:
    application_id: str
    version: int = -1
    session_id: str | None = None
    regulation_set_version: str | None = None
    required_rules: set[str] = field(default_factory=set)
    passed_rules: set[str] = field(default_factory=set)
    failed_rules: set[str] = field(default_factory=set)
    noted_rules: set[str] = field(default_factory=set)
    hard_block_rules: set[str] = field(default_factory=set)
    completed: bool = False
    overall_verdict: ComplianceVerdict | None = None
    events: list[StoredEvent] = field(default_factory=list)

    @classmethod
    async def load(cls, store, application_id: str) -> "ComplianceRecordAggregate":
        aggregate = cls(application_id=application_id)
        events = await store.load_stream(f"compliance-{application_id}")
        for event in events:
            aggregate._apply(event)
        return aggregate

    def _apply(self, event: StoredEvent) -> None:
        handler = getattr(self, f"_on_{event.event_type}", None)
        if handler is not None:
            handler(event)
        self.version = event.stream_position
        self.events.append(event)

    def _on_ComplianceCheckInitiated(self, event: StoredEvent) -> None:
        self.session_id = event.payload["session_id"]
        self.regulation_set_version = event.payload["regulation_set_version"]
        self.required_rules = set(event.payload.get("rules_to_evaluate", []))

    def _on_ComplianceRulePassed(self, event: StoredEvent) -> None:
        self.passed_rules.add(event.payload["rule_id"])

    def _on_ComplianceRuleFailed(self, event: StoredEvent) -> None:
        rule_id = event.payload["rule_id"]
        self.failed_rules.add(rule_id)
        if bool(event.payload["is_hard_block"]):
            self.hard_block_rules.add(rule_id)

    def _on_ComplianceRuleNoted(self, event: StoredEvent) -> None:
        self.noted_rules.add(event.payload["rule_id"])

    def _on_ComplianceCheckCompleted(self, event: StoredEvent) -> None:
        self.completed = True
        self.overall_verdict = ComplianceVerdict(event.payload["overall_verdict"])
        if bool(event.payload["has_hard_block"]):
            if not self.hard_block_rules and self.failed_rules:
                self.hard_block_rules.update(self.failed_rules)

    @property
    def has_hard_block(self) -> bool:
        return bool(self.hard_block_rules)

    def all_required_rules_evaluated(self) -> bool:
        if not self.required_rules:
            return False
        evaluated = self.passed_rules | self.failed_rules | self.noted_rules
        return self.required_rules.issubset(evaluated)

    def is_cleared_for_approval(self) -> bool:
        if not self.completed or self.has_hard_block:
            return False
        mandatory_rules = {rule for rule in self.required_rules if rule not in NOTE_ONLY_RULES}
        if not mandatory_rules.issubset(self.passed_rules):
            return False
        return self.overall_verdict == ComplianceVerdict.CLEAR

    def assert_cleared_for_approval(self) -> None:
        if not self.is_cleared_for_approval():
            raise DomainError("Compliance record is not fully clear for approval")

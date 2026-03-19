"""AgentSession aggregate with Gas Town bootstrap enforcement."""
from __future__ import annotations

from dataclasses import dataclass, field

from ledger.domain.errors import BusinessRuleViolation, InvalidStateTransition


@dataclass
class AgentSessionAggregate:
    agent_type: str
    session_id: str
    application_id: str | None = None
    agent_id: str | None = None
    model_version: str | None = None
    context_source: str | None = None
    context_loaded: bool = False
    completed: bool = False
    failed: bool = False
    recovered: bool = False
    last_node_sequence: int = 0
    version: int = -1
    events: list[dict] = field(default_factory=list)

    @classmethod
    async def load(cls, store, agent_type: str, session_id: str) -> "AgentSessionAggregate":
        aggregate = cls(agent_type=agent_type, session_id=session_id)
        stream_events = await store.load_stream(f"agent-{agent_type}-{session_id}")
        for event in stream_events:
            aggregate.apply(event)
        return aggregate

    def apply(self, event: dict) -> None:
        event_type = event.get("event_type")
        handler = getattr(self, f"_apply_{event_type}", None)
        if handler is not None:
            handler(event)

        self.version = event.get("stream_position", self.version + 1)
        self.events.append(dict(event))

    def assert_can_start(self) -> None:
        if self.version != -1:
            raise InvalidStateTransition(
                f"Agent session {self.session_id} already started"
            )

    def assert_context_loaded(self) -> None:
        if not self.context_loaded:
            raise BusinessRuleViolation(
                "Gas Town rule violated: session context must be loaded before work is recorded"
            )

    def assert_model_version(self, model_version: str) -> None:
        if self.model_version and self.model_version != model_version:
            raise BusinessRuleViolation(
                f"Model version lock violated: expected {self.model_version}, got {model_version}"
            )

    def assert_session_open(self) -> None:
        if not self.context_loaded:
            self.assert_context_loaded()
        if self.completed:
            raise InvalidStateTransition(
                f"Session {self.session_id} is already completed"
            )

    def _apply_AgentSessionStarted(self, event: dict) -> None:
        payload = event["payload"]
        self.application_id = payload.get("application_id")
        self.agent_id = payload.get("agent_id")
        self.model_version = payload.get("model_version")
        self.context_source = payload.get("context_source")
        self.context_loaded = True
        self.completed = False
        self.failed = False

    def _apply_AgentNodeExecuted(self, event: dict) -> None:
        payload = event["payload"]
        self.last_node_sequence = max(
            self.last_node_sequence, int(payload.get("node_sequence", 0))
        )

    def _apply_AgentSessionCompleted(self, event: dict) -> None:
        self.completed = True
        self.failed = False

    def _apply_AgentSessionFailed(self, event: dict) -> None:
        self.failed = True
        self.completed = False

    def _apply_AgentSessionRecovered(self, event: dict) -> None:
        self.recovered = True
        self.failed = False

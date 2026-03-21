from __future__ import annotations

from dataclasses import dataclass, field

from src.models.events import DomainError, StoredEvent


@dataclass
class AgentSessionAggregate:
    stream_id: str
    version: int = -1
    session_id: str | None = None
    agent_type: str | None = None
    agent_id: str | None = None
    application_id: str | None = None
    model_version: str | None = None
    context_source: str | None = None
    context_token_count: int | None = None
    started: bool = False
    recovered: bool = False
    completed: bool = False
    failed: bool = False
    output_events_written: list[dict] = field(default_factory=list)
    events: list[StoredEvent] = field(default_factory=list)

    @classmethod
    async def load(cls, store, agent_type: str, session_id: str) -> "AgentSessionAggregate":
        return await cls.load_stream(store, f"agent-{agent_type}-{session_id}")

    @classmethod
    async def load_stream(cls, store, stream_id: str) -> "AgentSessionAggregate":
        aggregate = cls(stream_id=stream_id)
        events = await store.load_stream(stream_id)
        for event in events:
            aggregate._apply(event)
        return aggregate

    def _apply(self, event: StoredEvent) -> None:
        handler = getattr(self, f"_on_{event.event_type}", None)
        if handler is not None:
            handler(event)
        self.version = event.stream_position
        self.events.append(event)

    def _on_AgentSessionStarted(self, event: StoredEvent) -> None:
        if self.version != -1:
            raise DomainError("AgentSessionStarted must be the first event in an agent session stream")
        self.started = True
        self.session_id = event.payload["session_id"]
        self.agent_type = event.payload["agent_type"]
        self.agent_id = event.payload["agent_id"]
        self.application_id = event.payload["application_id"]
        self.model_version = event.payload["model_version"]
        self.context_source = event.payload["context_source"]
        self.context_token_count = int(event.payload["context_token_count"])

    def _on_AgentSessionRecovered(self, event: StoredEvent) -> None:
        self.recovered = True

    def _on_AgentOutputWritten(self, event: StoredEvent) -> None:
        self.output_events_written.extend(list(event.payload.get("events_written", [])))

    def _on_AgentSessionCompleted(self, event: StoredEvent) -> None:
        self.completed = True

    def _on_AgentSessionFailed(self, event: StoredEvent) -> None:
        self.failed = True

    def assert_context_loaded(self, application_id: str | None = None) -> None:
        if not self.started:
            raise DomainError("Gas Town violation: session must start with AgentSessionStarted before any decision")
        if application_id is not None and self.application_id != application_id:
            raise DomainError(
                f"Agent session {self.stream_id} belongs to {self.application_id}, not {application_id}"
            )

    def assert_model_version_current(self, model_version: str) -> None:
        self.assert_context_loaded()
        if self.model_version and model_version != self.model_version:
            raise DomainError(
                f"Agent session {self.stream_id} is locked to model version {self.model_version}, not {model_version}"
            )

    def has_written_domain_event(self, event_type: str, application_id: str) -> bool:
        suffix = f"-{application_id}"
        for event in self.output_events_written:
            if event.get("event_type") == event_type and str(event.get("stream_id", "")).endswith(suffix):
                return True
        return False

    def assert_references_application(self, application_id: str) -> None:
        self.assert_context_loaded(application_id)
        if not self.output_events_written:
            raise DomainError(f"Agent session {self.stream_id} has no recorded domain outputs")

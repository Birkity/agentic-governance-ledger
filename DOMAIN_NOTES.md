# DOMAIN_NOTES.md  
## Phase 0 — Domain Reconnaissance  
## The Ledger: Agentic Event Store & Enterprise Audit Infrastructure

---

## 1. Overview

This project is not just about building an audit log. It is about designing a system where **history is the system**.

The Ledger will act as the **single source of truth** for a multi-agent AI platform used in financial decision-making. Every important action—by AI agents or humans—will be stored as an immutable event. From this event history, the system can:

- reconstruct the current state  
- replay past decisions  
- explain how a decision was made  
- prove that nothing was changed  

---

## 2. What system is being built?

Apex Financial Services uses multiple AI agents to process loan applications:

- Credit Analysis agent  
- Fraud Detection agent  
- Compliance agent  
- Decision orchestrator  
- Human loan officer (final decision)

---

## 3. Event Sourcing vs Event-Driven Architecture

- **EDA**: events are notifications  
- **Event Sourcing**: events are the database  

This project uses **Event Sourcing**, meaning the event stream is the source of truth.

---

## 4. Why CQRS is needed

- Command side → writes events  
- Query side → reads projections  

Commands must not depend on projections.

---

## 5. Aggregates

- LoanApplication → loan lifecycle  
- AgentSession → agent memory  
- ComplianceRecord → compliance checks  
- AuditLedger → audit integrity  

---

## 6. Validation

Validation happens **before writing events** because events are permanent.

---

## 7. Concurrency

If two agents write at the same time:
- one succeeds  
- one fails and must retry  

---

## 8. Projections

- Fast but slightly delayed views  
- Used for queries  
- Event store remains the truth  

---

## 9. Outbox

Ensures safe communication with external systems without losing data.

---

## 10. Agent Memory (Gas Town)

Agents store their steps as events so they can recover after crashes.

---

## 11. Summary

The Ledger is a system that:
- never loses history  
- can explain decisions  
- supports recovery  
- ensures trust and auditability  

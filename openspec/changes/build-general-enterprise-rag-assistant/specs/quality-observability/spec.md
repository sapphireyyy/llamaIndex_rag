## Purpose

Defines traceable operation, measurable retrieval and answer quality, repeatable regression evaluation, release gates, failure analysis, and user feedback for continuous RAG improvement.

## ADDED Requirements

### Requirement: Ingestion and query executions are end-to-end traceable
The system SHALL assign a stable correlation identifier to each ingestion job and query and SHALL trace the major stages, component versions, timing, status, retry, and degradation outcomes. Trace access MUST follow tenant and operational authorization rules.

#### Scenario: Trace a query
- **WHEN** an authorized operator opens a completed query by its correlation identifier
- **THEN** the system shows ordered query stages, durations, component versions, retrieval counts, terminal status, and sanitized errors

#### Scenario: Trace an ingestion retry
- **WHEN** an ingestion job performs multiple attempts
- **THEN** the trace links every attempt to the same job while distinguishing attempt timing and outcomes

### Requirement: The platform reports operational and cost metrics
The system SHALL produce metrics for request volume, success, refusal, failure, degradation, stage latency, retrieval candidate counts, model tokens, and attributable provider cost when available. Metrics SHALL support breakdown by tenant, assistant version, knowledge space, provider profile, and terminal status subject to access controls.

#### Scenario: Compare assistant versions
- **WHEN** an authorized operator requests metrics for two assistant versions over the same period
- **THEN** the system returns comparable latency, outcome, token, cost, and quality dimensions for those versions

#### Scenario: Provider omits cost data
- **WHEN** a provider reports usage but no monetary price or cost
- **THEN** the system records available usage, marks cost unavailable, and does not fabricate a cost value

### Requirement: Evaluation datasets are versioned and reusable
The system SHALL allow authorized quality users to create versioned evaluation datasets containing questions, expected source scope, optional reference answers, expected answerability, and required access context. Dataset items SHALL be immutable within a published dataset version.

#### Scenario: Publish a general evaluation dataset
- **WHEN** a quality user publishes a dataset containing policy, IT-help, and product-document questions
- **THEN** the system assigns a version and makes the immutable dataset available for repeatable evaluation runs

#### Scenario: Update a published dataset
- **WHEN** a quality user changes an item in a published dataset
- **THEN** the system creates a new draft dataset version rather than mutating prior evaluation evidence

### Requirement: Evaluation runs measure separate RAG quality dimensions
The system SHALL evaluate retrieval relevance, retrieval coverage, answer groundedness, citation correctness, answer relevance, refusal correctness, and access-control outcomes separately. Each run SHALL record the dataset version, assistant and component versions, evaluator versions, parameters, timestamps, and per-item results.

#### Scenario: Run a regression evaluation
- **WHEN** an authorized quality user evaluates an assistant version against a published dataset
- **THEN** the system produces aggregate and per-item results for every applicable quality dimension with complete version provenance

#### Scenario: Evaluate an unanswerable question
- **WHEN** a dataset item is marked as not answerable from the authorized corpus
- **THEN** the evaluation scores refusal behavior separately from ordinary answer relevance

### Requirement: Release gates prevent unapproved quality regression
The system SHALL allow authorized administrators to define minimum or maximum thresholds for selected evaluation and operational metrics. A release candidate that violates a mandatory gate MUST NOT be promoted unless an explicitly authorized, reasoned, and audited override is recorded.

#### Scenario: Pass all mandatory gates
- **WHEN** a release candidate meets every mandatory threshold on its required datasets
- **THEN** the system marks the candidate eligible for promotion

#### Scenario: Override a failed gate
- **WHEN** an authorized approver records a permitted override with a reason for a failed gate
- **THEN** the system records the original failure and override identity without changing the evaluation result

### Requirement: Users can submit contextual answer feedback
The system SHALL allow an authorized user to rate or categorize an answer and optionally add a comment. Feedback SHALL link to the immutable query, answer, citation, and configuration versions while respecting retention and sensitive-data policies.

#### Scenario: Submit negative citation feedback
- **WHEN** a user marks a citation as not supporting the answer
- **THEN** the system records the feedback against the specific answer and citation versions for authorized failure analysis

### Requirement: Telemetry applies privacy and retention policies
The system SHALL apply configured redaction, sampling, access, and retention policies before storing or exporting prompts, retrieved content, answers, feedback, and evaluation artifacts. Disabling content capture MUST NOT disable required aggregate metrics or security audit events.

#### Scenario: Content capture is disabled
- **WHEN** tenant policy prohibits storing prompt and response bodies in telemetry
- **THEN** traces retain correlation, timing, versions, counts, statuses, and sanitized errors without storing prohibited bodies


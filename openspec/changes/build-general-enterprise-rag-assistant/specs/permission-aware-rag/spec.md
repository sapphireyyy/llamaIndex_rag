## Purpose

Defines permission-aware retrieval and grounded conversational answering so users receive useful enterprise knowledge with verifiable citations and safe refusal when evidence is insufficient.

## ADDED Requirements

### Requirement: Queries resolve an authorized assistant context
The system SHALL require an authenticated principal and an active assistant profile for every enterprise query. It SHALL resolve only knowledge spaces that are both active and authorized for that principal and MUST fail closed when the authorization context cannot be determined.

#### Scenario: Query an authorized assistant
- **WHEN** an authenticated user submits a question to an active assistant profile and can access at least one referenced knowledge space
- **THEN** the system starts a query using only the authorized active knowledge spaces

#### Scenario: No authorized knowledge space remains
- **WHEN** an authenticated user submits a question but cannot access any active knowledge space referenced by the assistant
- **THEN** the system returns an access-safe response without performing content retrieval

### Requirement: Retrieval combines complementary search signals
The system SHALL retrieve candidates using configured semantic and lexical search signals, combine the candidates deterministically, and rerank them according to the active retrieval policy. The query result SHALL be traceable to the policy and component versions used.

#### Scenario: Execute hybrid retrieval
- **WHEN** the active retrieval policy enables semantic and lexical retrieval
- **THEN** the system combines authorized candidates from both strategies before reranking and context selection

#### Scenario: One retrieval dependency is unavailable
- **WHEN** one optional retrieval strategy is unavailable and the active policy permits degradation
- **THEN** the system uses the remaining authorized strategy, marks the query degraded, and exposes that state in telemetry

### Requirement: Access control is enforced before model context assembly
The system MUST apply tenant, knowledge-space, document, user, and group access filters before any retrieved content is placed into model context. Post-generation filtering alone SHALL NOT satisfy this requirement.

#### Scenario: Mixed-access candidates
- **WHEN** raw retrieval candidates include documents the principal may and may not access
- **THEN** only authorized candidates can proceed to reranking, context assembly, citation, or answer generation

#### Scenario: Authorization changes during a conversation
- **WHEN** a user's document access is revoked after an earlier conversation turn
- **THEN** the next turn re-evaluates authorization and does not reuse the revoked content as active evidence

### Requirement: Answers are grounded and cited
The system SHALL generate answers from the authorized selected evidence and SHALL attach source citations to factual claims derived from enterprise content. Each citation SHALL identify the document version and a navigable page, section, or equivalent source location.

#### Scenario: Produce a supported answer
- **WHEN** the selected evidence sufficiently supports an answer
- **THEN** the system returns the answer with citations that resolve to the supporting authorized source locations

#### Scenario: Citation target is no longer accessible
- **WHEN** a user attempts to open a citation after losing access to its source
- **THEN** the system denies the source view without disclosing the source content or restricted metadata

### Requirement: Insufficient evidence produces an explicit refusal
The system SHALL evaluate configured evidence and answer-quality thresholds before presenting an answer as grounded. If the thresholds are not met, it SHALL clearly state that the available authorized knowledge is insufficient and MUST NOT fill the gap using unsupported model knowledge.

#### Scenario: No relevant evidence
- **WHEN** authorized retrieval returns no evidence meeting the active relevance threshold
- **THEN** the system returns an insufficient-evidence response with no fabricated citation

#### Scenario: Conflicting evidence
- **WHEN** selected authoritative sources materially conflict and the active policy cannot resolve the conflict
- **THEN** the system discloses the conflict, cites the conflicting sources, and avoids presenting either claim as settled fact

### Requirement: Conversational queries preserve safe context
The system SHALL support multi-turn follow-up within a conversation while retaining the assistant profile and tenant boundary. It SHALL distinguish prior user intent from retrieved evidence and SHALL re-run retrieval and authorization when a follow-up requires enterprise facts.

#### Scenario: Resolve a follow-up reference
- **WHEN** a user asks a follow-up that refers to an entity from an earlier turn
- **THEN** the system uses safe conversation context to reformulate the query and retrieves current authorized evidence

#### Scenario: Switch assistant profile
- **WHEN** a user changes to another assistant profile
- **THEN** subsequent turns use the new profile's knowledge spaces and policies without carrying prior retrieved evidence into the new context

### Requirement: Answer delivery supports streaming and terminal status
The system SHALL support streaming answer delivery with a stable query identifier and an explicit terminal success, refusal, degraded, cancelled, or failed status. A partial answer that fails validation MUST NOT be represented as a successful grounded answer.

#### Scenario: Stream a successful answer
- **WHEN** answer generation and validation succeed
- **THEN** the system streams answer content, emits its verified citations, and finishes with a success status linked to the query identifier

#### Scenario: Validation fails after partial generation
- **WHEN** generated output fails citation, grounding, or safety validation
- **THEN** the system terminates or replaces the partial output with a safe terminal status and does not mark the query successful


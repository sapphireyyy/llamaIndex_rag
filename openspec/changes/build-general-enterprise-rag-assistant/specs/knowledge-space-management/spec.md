## Purpose

Defines tenant-scoped knowledge spaces that organize enterprise content, membership, data-source associations, lifecycle state, and default query boundaries for reusable assistants.

## ADDED Requirements

### Requirement: Authorized administrators can manage knowledge spaces
The system SHALL allow an authorized tenant administrator to create, view, rename, archive, and restore knowledge spaces within that tenant. Each knowledge space SHALL have a stable identifier, display name, lifecycle state, and tenant ownership that cannot be changed after creation.

#### Scenario: Create a knowledge space
- **WHEN** an authorized tenant administrator submits a valid knowledge-space name and settings
- **THEN** the system creates an active knowledge space owned by the administrator's tenant and returns its stable identifier

#### Scenario: Reject cross-tenant management
- **WHEN** an administrator attempts to view or modify a knowledge space owned by another tenant
- **THEN** the system denies the operation without revealing the other knowledge space's metadata

### Requirement: Knowledge-space membership controls visibility
The system SHALL support user and group membership assignments with at least administrator, editor, and reader roles. It MUST apply a successful membership change to new authorization decisions and MUST default to no access when no applicable membership or policy exists.

#### Scenario: Grant reader access
- **WHEN** an authorized administrator grants a user the reader role for a knowledge space
- **THEN** subsequent authorized listings and queries can include that knowledge space for the user

#### Scenario: Revoke access
- **WHEN** an authorized administrator removes the last applicable membership for a user
- **THEN** subsequent listings, queries, source previews, and downloads deny that user access to the knowledge space

### Requirement: Data sources belong to a knowledge space
The system SHALL allow an authorized administrator or editor to associate supported data sources with exactly one knowledge space and to enable, disable, or remove those associations. Removing a data-source association MUST initiate removal of its content from the active knowledge-space index.

#### Scenario: Associate a data source
- **WHEN** an authorized editor adds a valid supported data source to an active knowledge space
- **THEN** the system records the association and makes it available for ingestion scheduling

#### Scenario: Remove a data source
- **WHEN** an authorized administrator removes a data source from a knowledge space
- **THEN** the system stops future synchronization and initiates deletion propagation for content owned by that association

### Requirement: Knowledge spaces define default assistant boundaries
The system SHALL allow a knowledge space to reference default retrieval and content-handling policies. Archiving a knowledge space MUST exclude it from new assistant queries and ingestion runs while preserving administrative history.

#### Scenario: Query excludes an archived space
- **WHEN** an assistant profile references a knowledge space that has subsequently been archived
- **THEN** a new query excludes that space and records the exclusion in the query trace

#### Scenario: Restore an archived space
- **WHEN** an authorized administrator restores an archived knowledge space with valid policies
- **THEN** the system permits new ingestion runs and authorized assistant use for that space


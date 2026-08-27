# Administrator and user guide

Administrators create a knowledge space, assign user/group roles, add an upload source, and
upload documents. Upload returns a `queued` task after the durable submission transaction; the
job view polls each stage, attempt, lease, sanitized failure, retry, cancel, preview, and delete
action. A queued task is not yet searchable. Archiving a space removes it from member retrieval.

Tenant administrators can edit the versioned chunk window and OCR mode in Settings. Each task
freezes the complete processing snapshot at submission time, so changing settings does not alter
a task already being processed. Rebuild creates a new processing generation and only changes the
active pointer after both dense and lexical projections pass validation. The document view shows
generation number, strategy hash, processing configuration version, and both publication states;
an authorized administrator can roll back to a previous fully published generation.

Create an assistant from one or more active spaces. Each draft references immutable prompt,
model, retrieval, citation, refusal, guardrail, provider, and secret-binding versions. Resolve
all validation findings before activation. Historical activation performs the same checks and
is the supported rollback path.

Users select an assistant and ask a question. Streaming-capable providers emit real delta events;
buffered providers emit one complete answer event. Selecting
a citation reauthorizes the source before preview or download. “Insufficient authorized
evidence” means the assistant intentionally did not guess; a conflict response lists both
authorized positions. Every follow-up runs retrieval and authorization again.

Quality administrators create immutable datasets with question, expected source scope,
reference answer, answerability, and access context; run a candidate; inspect per-item errors;
compare metrics; and configure release gates. Only administrators can override a gate, and a
reason plus audit evidence is mandatory. Users can rate an answer or citation from chat.

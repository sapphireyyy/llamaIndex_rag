# Administrator and user guide

Administrators create a knowledge space, assign user/group roles, add an upload source, and
upload documents. The job view shows each stage, attempt, sanitized failure, retry, cancel,
preview, and delete action. Archiving a space removes it from member retrieval.

Create an assistant from one or more active spaces. Each draft references immutable prompt,
model, retrieval, citation, refusal, guardrail, provider, and secret-binding versions. Resolve
all validation findings before activation. Historical activation performs the same checks and
is the supported rollback path.

Users select an assistant and ask a question. Answer text streams with source chips. Selecting
a citation reauthorizes the source before preview or download. “Insufficient authorized
evidence” means the assistant intentionally did not guess; a conflict response lists both
authorized positions. Every follow-up runs retrieval and authorization again.

Quality administrators create immutable datasets with question, expected source scope,
reference answer, answerability, and access context; run a candidate; inspect per-item errors;
compare metrics; and configure release gates. Only administrators can override a gate, and a
reason plus audit evidence is mandatory. Users can rate an answer or citation from chat.

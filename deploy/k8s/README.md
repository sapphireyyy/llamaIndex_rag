# Production deployment

Create the `enterprise-rag-runtime` Secret through the organization secret operator; do not
commit values. Required keys are `RAG_DATABASE_URL`, OIDC issuer/JWKS values, object-store,
queue, vector, lexical, and model secret references. Apply migrations as a one-shot release
job before rolling the API and worker Deployments.

Provider data services must enable encryption, point-in-time recovery, multi-zone replicas,
private endpoints, and daily restore verification. Retain database and object-store backups
for the tenant contract period. Recovery order is database, object store, indexes rebuilt
from active document versions, then queue replay. Target RPO is 15 minutes and target RTO is
four hours until a measured production baseline replaces these initial objectives.

`provider-data-policies.yaml` contains the required encrypted/retained StorageClass contract,
worker disruption budget, backlog autoscaling, and egress policy. Replace its placeholder CSI
provisioner with the approved platform driver. Managed PostgreSQL, object storage, RabbitMQ,
Qdrant, OpenSearch, and Keycloak should be provisioned by the platform's approved operators or
infrastructure-as-code modules with point-in-time recovery and private endpoints.

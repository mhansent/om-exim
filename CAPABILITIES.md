# OpenMetadata capability matrix

The development baseline is OpenMetadata Server **2.0.0** and the **2.0.0.0** release of `openmetadata-ingestion`, with Python **3.12** for SDK model inspection. The REST CLI supports Python **3.12 through 3.14**. The deployment target is Server **2.0.8**. Live API compatibility for either patch still needs verification. The CLI currently accepts only the 2.0.0 baseline and requires the source and target version strings to match exactly.

The matrix inventories the types named in the current project plan and nearby SDK types relevant to scope. The plan mentions a legacy list of 19 categories without enumerating it, so the inventory cannot be reconciled against that list yet. Routes and create request names below were checked in the generated SDK; the P1 REST projections and selection rules were also checked against the tagged server source and exercised with synthetic responses. Every create route remains outside P1.

| Entity | API v1 collection | SDK create model | Planning status |
| --- | --- | --- | --- |
| Domain | `/domains` | `CreateDomainRequest` | Exact FQN; detail `parent` references define the hierarchy |
| Data product | `/dataProducts` | `CreateDataProductRequest` | Direct `domains`; paginated `/{id}/assets` is used for reference reporting |
| Table | `/tables` | `CreateTableRequest` | Direct `domains`; ignore references with `inherited: true`; resolve schema, database, and service |
| Topic | `/topics` | `CreateTopicRequest` | SDK exposes `domains`; direct assignment and service projection pending |
| Dashboard | `/dashboards` | `CreateDashboardRequest` | SDK exposes `domains` and chart references; direct assignment and service projection pending |
| Chart | `/charts` | `CreateChartRequest` | SDK exposes `domains`; direct assignment and service projection pending |
| Pipeline | `/pipelines` | `CreatePipelineRequest` | SDK exposes `domains`; direct assignment and service projection pending |
| ML model | `/mlmodels` | `CreateMlModelRequest` | SDK exposes `domains`; direct assignment and service projection pending |
| Container | `/containers` | `CreateContainerRequest` | SDK exposes `domains` and `parent`; direct assignment and parent semantics pending |
| Stored procedure | `/storedProcedures` | `CreateStoredProcedureRequest` | SDK exposes `domains` and database/schema references; direct assignment pending |
| Dashboard data model | `/dashboard/datamodels` | `CreateDashboardDataModelRequest` | SDK exposes `domains`; direct assignment and service projection pending |
| Search index | `/searchIndexes` | `CreateSearchIndexRequest` | SDK exposes `domains`; direct assignment and service projection pending |
| Metric | `/metrics` | `CreateMetricRequest` | SDK exposes `domains` and asset references; direct assignment pending |
| API collection | `/apiCollections` | `CreateAPICollectionRequest` | SDK exposes `domains`; direct assignment pending |
| API endpoint | `/apiEndpoints` | `CreateAPIEndpointRequest` | SDK exposes `domains`; direct assignment and collection dependency pending |
| Database | `/databases` | `CreateDatabaseRequest` | Structural dependency |
| Database schema | `/databaseSchemas` | `CreateDatabaseSchemaRequest` | Structural dependency |
| Database service | `/services/databaseServices` | `CreateDatabaseServiceRequest` | Structural dependency; report identity only |
| Team | `/teams` | `CreateTeamRequest` | Owner dependency; team membership and roles are outside scope |
| User | `/users` | `CreateUserRequest` | Owner dependency; credentials and roles are outside scope |
| Classification | `/classifications` | `CreateClassificationRequest` | Taxonomy dependency via tag |
| Tag | `/tags` | `CreateTagRequest` | Resolve entity-level labels by FQN and follow classification and parent tag |
| Glossary | `/glossaries` | `CreateGlossaryRequest` | Taxonomy dependency via term |
| Glossary term | `/glossaryTerms` | `CreateGlossaryTermRequest` | Resolve entity-level labels by FQN and follow glossary and parent term |
| Role | `/roles` | `CreateRoleRequest` | Dependency policy pending |
| Policy | `/policies` | `CreatePolicyRequest` | Dependency policy pending |

P1 follows only the registered dependency edges. It excludes soft-deleted entities through the baseline REST defaults. Column-level tags, product ports, lineage, custom extensions, and other unregistered relationships are deferred. Product membership is backed by OpenMetadata search, so its completeness depends on the source search index. Before enabling `2.0.8`, verify projections, inherited reference markers, pagination, and exact version reporting against a disposable same-version pair.

Source references: [SDK 2.0.0.0](https://pypi.org/project/openmetadata-ingestion/2.0.0.0/), [server resources](https://github.com/open-metadata/OpenMetadata/tree/2.0.0-release/openmetadata-service/src/main/java/org/openmetadata/service/resources), [server entity repositories](https://github.com/open-metadata/OpenMetadata/tree/2.0.0-release/openmetadata-service/src/main/java/org/openmetadata/service/jdbi3).

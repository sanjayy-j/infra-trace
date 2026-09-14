# InfraTrace — ER Diagram

Written in Mermaid so the diagram is version-controlled and diffable.
It renders directly in GitHub, in VS Code (Markdown preview, with the
*Markdown Preview Mermaid Support* extension), and at <https://mermaid.live>.

---

## 1. Full Entity-Relationship Diagram

```mermaid
erDiagram
    TEAM ||--o{ DEVELOPER : "employs"
    TEAM ||--o{ APPLICATION : "owns"
    TEAM ||--o{ COMPONENT : "owns"

    APPLICATION ||--o{ APPLICATION_COMPONENT : "uses"
    COMPONENT   ||--o{ APPLICATION_COMPONENT : "is used by"

    COMPONENT ||--o{ DEPENDENCY : "depends on (as component_id)"
    COMPONENT ||--o{ DEPENDENCY : "is depended on (as depends_on_id)"

    COMPONENT   ||--o{ DEPLOYMENT : "is deployed as"
    ENVIRONMENT ||--o{ DEPLOYMENT : "hosts"
    DEVELOPER   ||--o{ DEPLOYMENT : "performs"

    ENVIRONMENT ||--o{ INCIDENT : "is where it happened"
    DEPLOYMENT  |o--o{ INCIDENT : "confirmed cause of"
    DEVELOPER   ||--o{ INCIDENT : "reports"

    INCIDENT  ||--o{ INCIDENT_COMPONENT : "affects"
    COMPONENT ||--o{ INCIDENT_COMPONENT : "is affected by"

    TEAM {
        int     team_id      PK
        varchar team_name    UK "NOT NULL"
        varchar team_email   UK "NOT NULL"
        date    created_on      "NOT NULL"
    }

    DEVELOPER {
        int     developer_id   PK
        varchar developer_name    "NOT NULL"
        varchar email          UK "NOT NULL"
        varchar job_role          "NOT NULL"
        int     team_id        FK "NULL, SET NULL"
        date    joined_on         "NOT NULL"
    }

    APPLICATION {
        int     application_id   PK
        varchar application_name UK "NOT NULL"
        varchar app_type            "CHECK Web/Mobile/Internal/API"
        int     owner_team_id    FK "NULL"
        varchar description
    }

    COMPONENT {
        int     component_id   PK
        varchar component_name UK "NOT NULL"
        varchar component_type    "CHECK Service/Database/Cache/Queue/Storage/Gateway/ExternalAPI"
        int     owner_team_id  FK "NULL - unowned is a real state"
        varchar criticality       "CHECK Low/Medium/High/Critical"
        varchar tech_stack
        tinyint is_active         "CHECK 0 or 1"
        date    created_on        "NOT NULL"
    }

    APPLICATION_COMPONENT {
        int     application_id PK_FK
        int     component_id   PK_FK
        varchar usage_notes
    }

    DEPENDENCY {
        int     dependency_id   PK
        int     component_id    FK "NOT NULL - the dependent"
        int     depends_on_id   FK "NOT NULL - the dependency"
        varchar dependency_type    "CHECK Synchronous/Asynchronous/Data/Config"
        tinyint is_critical        "CHECK 0 or 1"
        varchar description
    }

    ENVIRONMENT {
        int     environment_id   PK
        varchar environment_name UK "NOT NULL"
        varchar region              "NOT NULL"
        tinyint is_production       "CHECK 0 or 1"
    }

    DEPLOYMENT {
        int      deployment_id  PK
        int      component_id   FK "NOT NULL"
        int      environment_id FK "NOT NULL"
        varchar  version           "NOT NULL"
        int      deployed_by    FK "NULL"
        datetime deployed_at       "NOT NULL"
        varchar  status            "CHECK Success/Failed"
        tinyint  is_rollback       "CHECK 0 or 1 - put an earlier version back"
    }

    INCIDENT {
        int       incident_id    PK
        varchar   title             "NOT NULL"
        varchar   severity          "CHECK SEV1..SEV4"
        varchar   status            "CHECK Open/Investigating/Mitigated/Resolved"
        int       environment_id FK "NOT NULL"
        int       reported_by    FK "NULL"
        datetime  started_at        "NOT NULL"
        datetime  resolved_at       "NULL, must be >= started_at"
        varchar   root_cause
        int       caused_by_deployment_id FK "NULL - CONFIRMED cause only"
        timestamp updated_at        "auto-maintained"
    }

    INCIDENT_COMPONENT {
        int     incident_id  PK_FK
        int     component_id PK_FK
        varchar impact_level    "CHECK RootCause/Unavailable/Degraded/Minor"
    }
```

---

## 2. The Self-Referencing Dependency Relationship

`DEPENDENCY` appears twice in the diagram above because it resolves a
**many-to-many relationship from `COMPONENT` back to `COMPONENT`**. Both of its
foreign keys point at the same table:

```mermaid
flowchart LR
    C["COMPONENT<br/>component_id (PK)"]
    D["DEPENDENCY<br/>component_id (FK)<br/>depends_on_id (FK)<br/>dependency_type<br/>is_critical"]

    C -- "as component_id<br/>(the dependent)" --> D
    C -- "as depends_on_id<br/>(the dependency)" --> D
```

### Reading direction — the single most important convention

Getting this backwards inverts every answer the project gives, so it is worth
being explicit.

| Column | Role | In the example below |
|---|---|---|
| `component_id` | the **dependent** — the component that needs something | Order Service |
| `depends_on_id` | the **dependency** — the component being relied upon | Payment Service |

One row:

| `component_id` | `depends_on_id` | Read as |
|---|---|---|
| Order Service | Payment Service | "Order Service **depends on** Payment Service" |

Written as an arrow, `Order Service → Payment Service` means *Order Service
depends on Payment Service*. The arrow points at what is needed, not at what is
affected.

### Two directions of traversal

The same rows answer two opposite questions, depending on which column you
filter and which you follow.

| Question | Filter on | Follow to | Direction |
|---|---|---|---|
| "What does X **need**?" | `component_id = X` | `depends_on_id` | forwards, *down* the stack |
| "What **breaks** if X fails?" | `depends_on_id = X` | `component_id` | **reverse**, *up* the stack |

**Blast-radius analysis is the reverse traversal**, applied repeatedly. Start at
the failed component, find every row whose `depends_on_id` is in the set so far,
add those `component_id`s, and repeat until nothing new appears:

```
                   reverse traversal (blast radius)
                   ───────────────────────────────►
   Payment DB        Payment Service      Order Service      API Gateway
        ◄─────────────────── forward traversal (what X needs) ──────────

   row 1:  component_id = Payment Service,  depends_on_id = Payment DB
   row 2:  component_id = Order Service,    depends_on_id = Payment Service
   row 3:  component_id = API Gateway,      depends_on_id = Order Service
```

Reading those three rows left to right by `depends_on_id` gives the forward
chain; reading them right to left by `component_id` gives the blast radius. No
extra table and no duplicated data — the same 29 rows serve both.

This is why the recursive CTE in
[`07_blast_radius.sql`](../../database/queries/07_blast_radius.sql) joins
`d.depends_on_id = impact.component_id` rather than the other way round.

---

## 3. The ShopSphere Dependency Graph (sample data)

The 29 dependency rows in `seed.sql` form this graph. Arrows point from a
component to what it **depends on**.

```mermaid
flowchart TD
    GW["API Gateway"]
    AUTH["Authentication Service"]
    ORD["Order Service"]
    PAY["Payment Service"]
    INV["Inventory Service"]
    NOT["Notification Service"]
    REC["Recommendation Service"]
    SRCH["Search Service"]
    SHIP["Shipping Service<br/>(unowned)"]
    COUP["Legacy Coupon Service<br/>(retired, unowned)"]

    PAYDB[("Payment DB")]
    ORDDB[("Orders DB")]
    INVDB[("Inventory DB")]
    USRDB[("User DB")]
    REDIS[("Redis Cache")]
    KAFKA[["Kafka Event Bus"]]
    OBJ[("Object Storage")]
    ES[("Elasticsearch Cluster")]
    STRIPE{{"Stripe Payment API"}}

    GW --> AUTH
    GW --> ORD
    GW --> SRCH
    GW --> REC

    AUTH --> USRDB
    AUTH --> REDIS
    AUTH -.-> KAFKA

    ORD --> PAY
    ORD --> INV
    ORD --> ORDDB
    ORD --> SHIP
    ORD -.-> KAFKA

    PAY --> PAYDB
    PAY --> REDIS
    PAY --> STRIPE
    PAY -.-> KAFKA

    INV --> INVDB
    INV --> REDIS

    NOT -.-> KAFKA
    NOT --> OBJ

    REC -.-> KAFKA
    REC --> OBJ
    REC --> INVDB

    SRCH --> ES
    SRCH --> INVDB
    SRCH --> REDIS

    SHIP --> ORDDB
    SHIP -.-> KAFKA

    COUP --> INVDB
```

Solid arrows are synchronous or data dependencies; dotted arrows are
asynchronous (event-bus) dependencies.

---

## 4. Blast Radius Example — "If Payment DB fails"

The recursive query in
[`database/queries/07_blast_radius.sql`](../../database/queries/07_blast_radius.sql)
walks the graph **upwards** — following `depends_on_id` back to `component_id` —
and produces this chain:

```mermaid
flowchart BT
    PAYDB[("Payment DB<br/>FAILED")]:::failed
    PAY["Payment Service<br/>hop 1"]:::h1
    ORD["Order Service<br/>hop 2"]:::h2
    GW["API Gateway<br/>hop 3"]:::h3

    PAYDB --> PAY --> ORD --> GW

    classDef failed fill:#b91c1c,color:#fff,stroke:#7f1d1d
    classDef h1 fill:#ea580c,color:#fff,stroke:#9a3412
    classDef h2 fill:#d97706,color:#fff,stroke:#92400e
    classDef h3 fill:#ca8a04,color:#fff,stroke:#854d0e
```

Continuing past the components to `application_component` shows the customer
impact: **ShopSphere Web**, **ShopSphere Mobile**, **ShopSphere Admin** and
**ShopSphere Partner API** all use at least one affected component.

# AWS architecture

Terraform for this infrastructure stays in Backend-Server. Its existing state
owns Auth0, ECR, ECS, ALB routing, security groups, and RDS access. This
repository owns the service image and deployment workflow only.

## Request and data flow

```mermaid
flowchart LR
    user[Cursor or MCP client] -->|OAuth PKCE| auth0[Auth0]
    auth0 -->|RS256 access token| user
    user -->|Streamable HTTP and bearer token| alb[Public ALB]
    alb -->|Host and /mcp route| service[Stateless MCP ECS task]
    service --> verify[Validate JWKS, issuer, audience, expiry, and scope]
    verify --> discover[Filter configured tenants and query live users]
    discover --> rds[(Tenant PostgreSQL databases)]
    discover --> result[Return permitted tenants or identity]

    classDef userNode fill:#dbeafe,stroke:#2563eb,stroke-width:1.5px,color:#1e3a8a
    classDef parseNode fill:#e0e7ff,stroke:#6366f1,stroke-width:1.5px,color:#312e81
    classDef effectNode fill:#fef3c7,stroke:#d97706,stroke-width:1.5px,color:#78350f
    classDef bugNode fill:#fee2e2,stroke:#dc2626,stroke-width:2px,color:#7f1d1d
    classDef resultNode fill:#fee2e2,stroke:#dc2626,stroke-width:1.5px,color:#7f1d1d
    classDef legacyNode fill:#fee2e2,stroke:#dc2626,stroke-width:1.5px,color:#7f1d1d,stroke-dasharray: 5 5
    classDef okNode fill:#dcfce7,stroke:#16a34a,stroke-width:1.5px,color:#14532d
    classDef extNode fill:#f1f5f9,stroke:#64748b,stroke-width:1.5px,color:#0f172a

    class user userNode
    class verify,discover parseNode
    class service effectNode
    class result okNode
    class auth0,alb,rds extNode
```

The bearer token never leaves the MCP task. Tenant discovery runs one
read-only membership query per environment-matching tenant on a cache miss.
The query requires both `auth0_id = sub` and `deleted_at IS NULL`.

## VPC and subnet placement

```mermaid
flowchart TB
    client[Cursor or MCP client] -->|HTTPS 443| dns[Solstice API hostname]
    client -->|OAuth PKCE| auth0[Auth0]
    github[GitHub Actions OIDC] -->|Push immutable image| ecr[ECR]
    github -->|Register task definition and update service| control[ECS control plane]

    subgraph vpc["Solstice VPC 172.31.0.0/16"]
        direction TB
        igw[Internet gateway]

        subgraph public["Public subnets: us-east-1a, 1b, 1e"]
            alb[Internet-facing ALB]
            nat[NAT gateway]
        end

        subgraph private["Private app subnets: us-east-1a, 1b, 1e"]
            ecs[ECS Fargate mcp-server]
            verify[Validate JWT and mcp:connect]
            query[Resolve tenant and query live user]
        end

        rds[(RDS tenant databases<br/>PubliclyAccessible=false)]

        igw --> alb
        alb -->|ALB SG to MCP SG:8000| ecs
        ecs --> verify --> query
        query -->|MCP SG to RDS SG:5432| rds
        ecs -->|Private route 0.0.0.0/0| nat
        nat --> igw
    end

    dns --> alb
    control --> ecs
    ecr -->|Image pull through task execution role| ecs
    ecs -->|JWKS HTTPS through NAT| auth0
    ecs --> healthy[ALB health check passes /health]

    classDef userNode fill:#dbeafe,stroke:#2563eb,stroke-width:1.5px,color:#1e3a8a
    classDef parseNode fill:#e0e7ff,stroke:#6366f1,stroke-width:1.5px,color:#312e81
    classDef effectNode fill:#fef3c7,stroke:#d97706,stroke-width:1.5px,color:#78350f
    classDef bugNode fill:#fee2e2,stroke:#dc2626,stroke-width:2px,color:#7f1d1d
    classDef resultNode fill:#fee2e2,stroke:#dc2626,stroke-width:1.5px,color:#7f1d1d
    classDef legacyNode fill:#fee2e2,stroke:#dc2626,stroke-width:1.5px,color:#7f1d1d,stroke-dasharray: 5 5
    classDef okNode fill:#dcfce7,stroke:#16a34a,stroke-width:1.5px,color:#14532d
    classDef extNode fill:#f1f5f9,stroke:#64748b,stroke-width:1.5px,color:#0f172a

    class client userNode
    class github,igw,nat effectNode
    class ecs,verify,query parseNode
    class healthy okNode
    class dns,auth0,ecr,control,alb,rds extNode
```

Dev, staging, and platform-testing use the existing dev ECS cluster, ALB, RDS
instance, and `solstice-dev-mcp` ECR repository. Production uses the existing
prod equivalents and `solstice-prod-mcp`. Each ECS task runs in private
`solstice-private-*` subnets across three availability zones. Those subnets
egress through the existing NAT gateway for Auth0 JWKS and ECR access. The ALB
is internet-facing across three public subnets. MCP tasks have no public IP;
their service security group accepts port 8000 only from the matching ALB
security group and is the only MCP source allowed to reach PostgreSQL on 5432.

## CI deployment identity

GitHub Actions requests a short-lived OIDC token with audience
`sts.amazonaws.com`. AWS validates that token and allows
`sts:AssumeRoleWithWebIdentity` only for this repository's immutable subject in
the `dev` or `prod` GitHub environment. The resulting role session can push the
image and update ECS; no AWS access key is stored in GitHub. A subject,
audience, or signature mismatch prevents the workflow from obtaining AWS
credentials, so the deployment stops before ECR or ECS changes.

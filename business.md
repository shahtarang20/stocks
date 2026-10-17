# MASTER BUILD PROMPT — COMPUTECOIN
## Decentralized AI GPU Computing Marketplace, Provider Network, Developer Platform and Future Native Cryptocurrency

## 1. ROLE AND EXPECTATIONS

Act as a senior software architect, distributed-systems engineer, AI infrastructure engineer, DevOps engineer, blockchain engineer, UI/UX designer, security engineer and product manager.

Build a production-oriented, extensible platform called ComputeCoin.

This is not merely a cryptocurrency landing page, a token dashboard, a mock marketplace or a basic GPU rental website. The objective is to build a functioning decentralized-compute marketplace that connects independent GPU providers with developers who need computing resources.

The platform must be developed incrementally, with working functionality, automated tests, security controls, documentation and a reproducible development environment.

Do not generate the entire system as one enormous untested code dump. Inspect the repository first, establish the architecture, create a task plan, implement one phase at a time, run tests and resolve failures before proceeding.

Never claim that a feature works unless its implementation and tests demonstrate that it works.

## 2. PRODUCT VISION

ComputeCoin will connect two main groups.

### A. GPU providers

Individuals, developers, small data centers and infrastructure companies can:

- Register and verify their accounts.
- Install a ComputeCoin Provider Agent on an eligible machine.
- Register GPU hardware and report actual capabilities.
- Set availability, pricing and supported workload types.
- Receive eligible compute-job requests.
- Accept or automatically match jobs according to configured policies.
- Execute authorized workloads in isolated environments.
- Monitor GPU utilization, memory, temperature, power usage where available, job status and uptime.
- View completed jobs, verified usage, revenue and platform fees.
- Request eligible payouts.
- Configure resource limits, maintenance periods and automatic shutdown policies.

Providers must never be allowed to advertise unverified hardware specifications as verified.

### B. AI developers and customers

Developers can:

- Register and sign in securely.
- Create projects and manage API keys.
- Search for available GPUs.
- Compare hardware, VRAM, supported software, price, availability, performance benchmarks and provider reliability.
- Submit inference, embedding, image-generation, fine-tuning and training jobs.
- Upload authorized datasets or provide access to approved object-storage locations.
- Select compatible models and container images.
- Specify GPU requirements, runtime limits, storage requirements and spending limits.
- Track queued, running, completed, failed and cancelled jobs.
- Retrieve logs, output artifacts and job metrics.
- Review itemized costs and download invoices.
- Monitor API usage and project budgets.

A user must never receive access to another customer's private jobs, logs, datasets, API keys or billing records.

## 3. CORE PRODUCT DIFFERENTIATION

Do not compete solely on the number of GPUs listed or on promises of low prices.

Build the following differentiating capabilities:

1. Verified compute profiles: distinguish self-reported specifications from hardware actually benchmarked by the platform.
2. Price per successful workload: display estimated cost and actual final cost for supported job types.
3. Transparent job receipts: record requested resources, execution duration, result status, provider identity, charges and verification evidence.
4. Reliability-aware scheduling: use availability, compatibility, performance, price and historical successful completion rates.
5. Workload portability: provide a consistent API and SDK across eligible providers.
6. Budget protection: support spending caps, runtime limits, rate limits and customer-controlled cancellation.
7. Reproducible jobs: pin container image digests, dependency versions and relevant execution settings.
8. Privacy-aware execution: minimize data exposure and clearly distinguish ordinary isolation from stronger confidential-computing guarantees.
9. Provider fairness: distribute work according to legitimate matching criteria, not undisclosed favoritism.
10. Measurable economics: expose real utilization, job completion, customer retention, provider earnings and network operating costs.

Do not claim that decentralized computing is always cheaper, faster, safer or more private than centralized cloud services. Measure those outcomes.

## 4. TECHNOLOGY STACK

Use the following stack unless a documented technical reason requires an alternative.

### Frontend

- Next.js with React and TypeScript.
- Tailwind CSS.
- shadcn/ui or an equivalent accessible component system.
- TanStack Query for server-state management.
- React Hook Form and Zod for form validation.
- Recharts for analytics.
- A suitable wallet-connection library only when blockchain functionality is introduced.

### Backend

- Python 3.12+ using FastAPI.
- Pydantic for validation and typed API contracts.
- SQLAlchemy 2.x and Alembic migrations.
- PostgreSQL as the primary relational database.
- Redis for caching, rate limiting and short-lived coordination data.
- Celery, Dramatiq or a similarly suitable task queue for asynchronous workloads.
- WebSockets or Server-Sent Events for live job updates.
- S3-compatible object storage for datasets, logs and output artifacts.

### AI workload execution

- PyTorch and supported inference runtimes.
- Hugging Face Transformers and compatible open-source model formats.
- vLLM or another supported inference engine for compatible models.
- Docker for workload packaging.
- NVIDIA Container Toolkit on supported NVIDIA GPU hosts.
- NVIDIA Management Library (NVML) or supported telemetry interfaces for hardware monitoring.
- Optional ONNX Runtime for compatible inference workloads.

Do not assume that every GPU supports every framework, model or CUDA version. Use explicit capability detection and compatibility checks.

### Blockchain, introduced in a later phase

- Start with a testnet deployment on an established EVM-compatible network.
- Use Solidity and a maintained smart-contract development framework such as Foundry.
- Use OpenZeppelin libraries where appropriate.
- Use an audited, well-understood escrow design rather than inventing a novel financial mechanism.
- Do not deploy contracts to mainnet or launch a publicly traded token without explicit authorization, security review and legal review.

### DevOps and deployment

- Docker and Docker Compose for local development.
- GitHub Actions for continuous integration.
- Structured application logging and error monitoring.
- OpenTelemetry-compatible tracing and metrics where useful.
- A documented deployment approach for frontend, API, worker, scheduler, database, cache, object storage and provider agent.

Pin dependency versions and container image digests in deployment configurations where practical.

## 5. REPOSITORY STRUCTURE

computecoin/
  apps/
    web/
    api/
    admin/
  services/
    scheduler/
    job-worker/
    billing/
    verification/
  provider-agent/
    computecoin_agent/
    tests/
  sdk/
    python/
    typescript/
  contracts/
    src/
    test/
    script/
  packages/
    shared-types/
    ui/
  infrastructure/
    docker/
    monitoring/
    deployment/
  migrations/
  tests/
    integration/
    security/
    end-to-end/
  docs/
    architecture/
    api/
    operations/
    security/
    tokenomics/
  scripts/
  docker-compose.yml
  .env.example
  README.md

## 6. USER ROLES AND AUTHENTICATION

Implement role-based authorization with the following roles:

- Guest
- Customer / Developer
- GPU Provider
- Support Operator
- Security Operator
- Administrator

## 7–29. [See full sections in master prompt]

Refer to the complete master prompt for detailed requirements on:
- Public website and UI (§7)
- GPU Marketplace (§8)
- Provider Agent (§9)
- Job Submission and Execution (§10)
- Scheduler and Matching Engine (§11)
- AI Inference API (§12)
- Model Registry and Artifact Storage (§13)
- Billing and Compute Credits (§14)
- Job Verification and Provider Reputation (§15)
- Native Token — Future Phase (§16)
- Smart Contract Requirements (§17)
- Administration and Operations (§18)
- Database Design (§19)
- Security and Abuse Prevention (§20)
- Observability and Analytics (§21)
- Testing Strategy (§22)
- Local Development Experience (§23)
- Environment Configuration (§24)
- Deployment and Release Engineering (§25)
- Business Model (§26)
- Implementation Phases (§27)
- Acceptance Criteria (§28)
- Working Instructions (§29)

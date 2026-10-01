# SentinelOps: An Autonomous, Safety-Gated AI Platform for Incident Detection, Root Cause Analysis and Remediation in Cloud-Native Applications

| | |
|---|---|
| **Program** | BS Information Technology |
| **University** | Minhaj University Lahore |
| **Student** | Shahzaib |
| **Roll No.** | [add] |
| **Group members** | [add, if any] |
| **Supervisor** | [add] |
| **Version / Date** | 1.0 / September 2026 |

**Tier legend used in this document:** Tier 1 = MVP (must be finished by the end of FYP-I) · Tier 2 = advanced features · Tier 3 = stretch goals.

---

## 1. Introduction

Modern applications run as many cooperating services (APIs, databases, caches, queues) inside containers. When one component degrades, symptoms appear in several services at once and generate thousands of log lines and metric changes. An on-call engineer must manually collect evidence, correlate it with recent deployments, decide on a fix and apply it. This process is slow, error-prone and expensive.

Large Language Models (LLMs) can read logs and reason about failures, but using them directly in operations is risky: they can hallucinate causes, propose unsafe actions, leak sensitive data to external APIs, and can be manipulated by text hidden inside logs (prompt injection). This project builds a complete platform that combines classical monitoring, evidence-grounded LLM reasoning and deterministic safety guardrails, and evaluates it quantitatively against baselines.

## 2. Problem Statement

1. **Slow manual triage.** Mean Time To Recovery (MTTR) is dominated by human evidence gathering and correlation across logs, metrics, traces and deployment history.
2. **Symptom-based alerting.** Threshold alerts report symptoms (for example a 5xx spike in the gateway) but not the underlying cause (for example an exhausted database connection pool in a downstream service). This causes alert fatigue.
3. **Unsafe naive LLM use.** Prompting an LLM with raw logs gives ungrounded diagnoses, no safety control over actions, no protection against injected instructions in logs, and no protection of secrets or personal data.
4. **No reproducible evaluation.** Many demonstrations of AI-assisted operations lack labelled fault scenarios, baselines and ablation studies, so their claims cannot be verified.

**Problem to solve:** design, implement and evaluate an end-to-end system that detects incidents, identifies the root cause using grounded evidence, proposes and (with policy-based approval) executes remediation, verifies the fix, and documents the incident, all reproducibly and with zero paid infrastructure.

## 3. Aim and Objectives

**Aim:** Reduce incident recovery time in containerised applications through an AI-driven, evidence-grounded and safety-gated incident response platform.

| ID | Objective |
|----|-----------|
| O1 | Build a multi-service target application with full observability (metrics, logs, traces). |
| O2 | Build a chaos/fault-injection framework with at least 20 labelled fault scenarios and recorded ground truth. |
| O3 | Detect anomalies (metrics and logs) and correlate alerts into single incidents. |
| O4 | Build an LLM agent that uses tools, a service dependency graph and a knowledge base to output structured, evidence-cited diagnoses. |
| O5 | Build a remediation engine with allowlisted actions, risk scoring, approval policy, post-fix verification and automatic rollback. |
| O6 | Provide a web dashboard, a chat-based approval channel and automatically generated postmortem reports. |
| O7 | Protect the pipeline: redact secrets/PII before LLM calls and detect log-injection attempts. |
| O8 | Evaluate against baselines with ablations and report measurable results (targets in Section 10). |

## 4. Scope

| In scope | Out of scope |
|----------|--------------|
| Containerised multi-service demo application | Real production systems of third-party companies |
| Docker Compose based deployment (Kubernetes optional as future work) | Training or fine-tuning a large language model from scratch |
| Detection, RCA, remediation, verification, postmortem | Automatic code-level bug fixing / patch generation |
| Local-first operation, cloud deployment as final phase | Multi-region, large-scale (thousands of nodes) operation |
| Free and open-source tooling plus free tiers | Paid SaaS monitoring products |

## 5. Proposed System Overview

```
Target App (gateway, orders, payments, inventory, postgres, redis, queue)
        │  metrics · logs · traces
        ▼
Observability (Prometheus · Loki · Jaeger · cAdvisor · Grafana)
        │
        ▼
Detection (thresholds · EWMA · Isolation Forest · Drain3 log templates)
        │  incident
        ▼
Sanitizer (secret/PII redaction · log-injection guard)
        │
        ▼
RCA Agent Layer (evidence builder → dependency-graph ranking →
                 Triage → Investigator → Planner → Verifier)
        │ ◀── Knowledge Base (runbooks + past incidents, RAG)
        ▼  proposed action + risk score
Remediation Engine (policy check → approval → execute → verify → auto-rollback)
        │
        ▼
Dashboard · Telegram bot · Audit log · Auto-generated postmortem
```

**Data flow**
1. Load generator drives the target application; the fault injector introduces a labelled fault.
2. Observability stack collects telemetry; the detector raises alerts and groups them into one incident.
3. The evidence builder collects a time-windowed bundle (logs, metrics, traces, recent deployments/config changes) and ranks suspect services using the dependency graph.
4. The sanitizer removes secrets/PII and flags injected instructions; then the agent layer produces a structured diagnosis and an action plan.
5. The policy engine scores risk. Low-risk actions may auto-execute; medium/high-risk actions wait for human approval (dashboard or Telegram).
6. After execution the verifier checks that metrics recovered; if not, the action is rolled back and the incident is escalated.
7. A postmortem is generated and the resolved incident is added to the knowledge base.

## 6. Modules

| ID | Module | Description | Key technologies | Tier |
|----|--------|-------------|------------------|------|
| M1 | Target application | 4-service e-commerce style app (gateway, orders, payments, inventory) with PostgreSQL, Redis, message queue; load generator | FastAPI/Flask, Docker Compose, Locust | 1 |
| M2 | Observability stack | Metrics, centralised logs, distributed traces, container metrics, dashboards | Prometheus, Loki + Grafana Alloy, OpenTelemetry + Jaeger, cAdvisor, Grafana | 1 (traces: 2) |
| M3 | Chaos / fault injection | Labelled fault catalogue (Section 8), CLI + API trigger, ground-truth recording | Python, stress-ng, Toxiproxy, Docker SDK | 1 (8 faults) → 2 (21 faults) |
| M4 | Detection and incident correlation | Threshold, statistical and ML anomaly detection on metrics; log-template anomaly detection; grouping of alerts into incidents | Prometheus rules, EWMA/z-score, scikit-learn Isolation Forest, Drain3 | 1 (thresholds) → 2 |
| M5 | Evidence builder and dependency graph | Collects windowed logs/metrics/traces and recent changes; builds service graph and ranks suspects | Python, NetworkX, PromQL/LogQL clients | 2 |
| M6 | LLM RCA agent | Tool-calling agent that returns a structured diagnosis (root cause, confidence, cited evidence, actions, risk); provider-agnostic | Gemini free tier / Ollama, Pydantic schema, `call_llm()` abstraction | 1 (single agent) → 3 (multi-agent) |
| M7 | Knowledge base (RAG) and learning loop | Runbooks and resolved incidents embedded and retrieved as context; confirmed incidents are added back | ChromaDB or FAISS, sentence-transformers | 2 |
| M8 | Remediation engine and guardrails | Allowlisted actions, risk scoring, dry-run, approval policy, post-fix verification, auto-rollback, tamper-evident audit log | Python, Docker SDK, SQLite/PostgreSQL | 1 (allowlist + approval) → 2 |
| M9 | Security layer | Secret/PII redaction before LLM calls, log-injection detection, role-based approval | Regex + classifier, JWT | 2 |
| M10 | Dashboard | Overview, live topology map, incident timeline, agent reasoning trace, approvals, chaos control panel, evaluation results | FastAPI, WebSocket, React + Tailwind (Streamlit for MVP) | 1 → 3 |
| M11 | ChatOps and postmortem | Telegram bot with Approve/Reject buttons; auto-generated postmortem per incident (timeline, root cause, actions, prevention) | Telegram Bot API, Jinja2, Markdown → PDF | 2 |
| M12 | Evaluation framework | Automated experiment runner, metric computation, baselines, ablations, plots | Python, pandas, matplotlib | 1 (basic) → 2 |
| M13 | Cloud deployment and CI/CD | Infrastructure as Code, container registry, pipeline, live demo on Azure | Bicep/ARM, GitHub Actions, Azure VM or Container Apps, optional Azure Service Bus | 3 |
| M14 | Replay and offline benchmark mode | Stores evidence bundles so LLM decisions can be replayed without re-running faults; optional use of public datasets | JSON snapshots, LogHub / RCAEval (optional) | 2 |

## 7. Requirements

**Functional requirements**

| ID | Requirement |
|----|-------------|
| FR1 | The system shall collect metrics, logs and traces from all services of the target application. |
| FR2 | The system shall detect anomalies and group related alerts into one incident. |
| FR3 | The system shall produce a structured diagnosis with root-cause service, confidence and cited evidence. |
| FR4 | The system shall propose remediation actions only from a predefined allowlist. |
| FR5 | The system shall assign a risk level to every proposed action and enforce the approval policy. |
| FR6 | The system shall verify recovery after an action and roll back automatically if recovery fails. |
| FR7 | The system shall redact secrets/PII before any text is sent to an external LLM. |
| FR8 | The system shall detect and neutralise instructions embedded in logs. |
| FR9 | The system shall record every decision and action in an audit log. |
| FR10 | The system shall generate a postmortem report for every resolved incident. |
| FR11 | The system shall allow faults to be triggered and experiments to be run from a control panel or CLI. |

**Non-functional requirements**

| ID | Requirement |
|----|-------------|
| NFR1 | Detection-to-diagnosis latency (excluding human approval) ≤ 60 seconds. |
| NFR2 | Reproducibility: the full environment starts with a single command. |
| NFR3 | Provider independence: switching the LLM provider requires changing configuration only. |
| NFR4 | Safety: no action outside the allowlist can be executed; zero unapproved high-risk actions. |
| NFR5 | Cost: all development and experiments run on free software and free tiers. |
| NFR6 | Explainability: every diagnosis shows the evidence it was based on. |
| NFR7 | Privacy: no secrets or personal data leave the local environment. |

## 8. Fault Catalogue (Ground Truth for Evaluation)

| ID | Category | Fault | Injection method | Expected root cause | Expected remediation |
|----|----------|-------|------------------|---------------------|----------------------|
| F01 | Resource | CPU saturation in orders | stress-ng / busy loop | CPU exhaustion, orders | Restart / scale orders |
| F02 | Resource | Memory leak in payments | Leaking endpoint | Memory exhaustion, payments | Restart payments |
| F03 | Resource | Disk full from log flood | Large file writes | Disk exhaustion, host/service | Rotate/clean logs |
| F04 | Deployment | Bad release (HTTP 500s) | Deploy faulty image tag | Faulty release, service X | Roll back to previous tag |
| F05 | Deployment | API contract break | Payments returns changed schema | Breaking release, payments | Roll back payments |
| F06 | Configuration | Missing required env var (crash loop) | Remove variable | Misconfiguration | Revert config |
| F07 | Configuration | Wrong DB host in config | Change config | Misconfiguration | Revert config |
| F08 | Configuration | Invalid DB credential | Change password variable | Auth failure to DB | Restore secret |
| F09 | Configuration | Wrong health-check path | Change readiness check | Health-check misconfiguration | Revert config |
| F10 | Dependency | PostgreSQL down | Stop container | Database unavailable | Restart database |
| F11 | Dependency | Slow queries / lock contention | Long-running transaction | Database contention | Terminate blocking query |
| F12 | Dependency | Connection pool exhaustion | Connection leak | Pool exhaustion, service X | Restart service / raise pool |
| F13 | Dependency | Redis down | Stop container | Cache unavailable | Restart Redis |
| F14 | Dependency | Queue backlog | Pause consumer | Consumer stalled | Restart / scale consumer |
| F15 | Network | Latency between gateway and orders | Toxiproxy latency | Network latency | Remove fault / restore path |
| F16 | Network | Packet loss / timeouts to payments | Toxiproxy timeout | Network timeout | Remove fault / restore path |
| F17 | Cascade | Slow payments exhausts orders workers, gateway returns 504 | Slow handler in payments | Payments (root), not gateway | Fix payments first |
| F18 | Load | Traffic spike | Locust surge | Capacity limit | Scale out |
| F19 | Application | Thread/worker exhaustion | Slow handler + load | Worker exhaustion | Scale out / restart |
| F20 | Application | Noisy error loop | Repeating exception | Application bug (log flood) | Restart / reduce log level |
| F21 | Security | Log-injection attack | Log lines containing instructions to the agent | Agent must ignore injected text and flag it | No action from injected text |

**Remediation allowlist:** `restart_service`, `scale_service`, `rollback_deployment`, `revert_config`, `restore_network_path`, `rotate_logs`, `terminate_blocking_query`. Each action has a risk level (low/medium/high), a dry-run mode and a verification check.

## 9. Technology Stack (all free)

| Layer | Choice |
|-------|--------|
| Language | Python 3.11+, JavaScript/TypeScript |
| Services and API | FastAPI (Flask acceptable), WebSocket |
| Containers | Docker, Docker Compose |
| Metrics / logs / traces | Prometheus, Loki + Grafana Alloy, OpenTelemetry, Jaeger, cAdvisor, Grafana |
| Chaos tools | stress-ng, Toxiproxy, Locust, custom Python scripts |
| ML / NLP | scikit-learn, Drain3, sentence-transformers |
| Vector store | ChromaDB or FAISS (local) |
| LLM | Google Gemini free tier (primary), Ollama local model (fallback), via one `call_llm()` interface |
| Data | SQLite (MVP) → PostgreSQL |
| Frontend | Streamlit (MVP) → React + Tailwind |
| ChatOps | Telegram Bot API |
| Cloud / IaC / CI | Azure (student credit, final phase only), Bicep/ARM, GitHub Actions |
| Version control | Git + private GitHub repository |

## 10. Evaluation Plan

**Metrics**

| Metric | Definition |
|--------|-----------|
| MTTD | Time from fault injection to incident creation |
| MTTR | Time from fault injection to verified recovery |
| RCA Acc@1 / Acc@3 | Fraction of incidents where the true root cause is ranked first / within top three |
| Remediation success rate | Fraction of incidents recovered by the proposed action |
| False-positive incident rate | Incidents raised without an injected fault |
| Unsafe-action rate | Actions executed outside policy (target: 0) |
| Evidence validity | Fraction of cited log lines/metrics that actually exist in the evidence bundle |
| Diagnosis latency and token usage | Time and tokens per incident |
| Injection resistance | Fraction of injected log payloads that the agent ignores/flags (at least 30 payloads) |

**Configurations compared**

| ID | Configuration |
|----|---------------|
| B1 | Manual: human operator following written runbooks (at least 3 volunteers) |
| B2 | Threshold alerts + static rule-to-action mapping |
| B3 | LLM only: raw logs, no tools, no graph, no knowledge base |
| B4 | Full SentinelOps |

**Ablations on B4:** without RAG, without dependency graph, without verifier agent, without redaction/guard, local model versus hosted model.

**Experiment design:** 21 faults × 5 repetitions per configuration, randomised order, fixed load profile and warm-up period, fixed random seeds. Results reported as mean ± standard deviation with a confusion matrix (predicted vs true root cause) and per-category breakdown. LLM-only comparisons run in replay mode (Module M14) to stay within free-tier limits.

**Target hypotheses (to be confirmed or revised with real results):** Acc@3 ≥ 80%; MTTR reduction ≥ 50% versus B1; unsafe-action rate = 0; injection resistance ≥ 95%.

## 11. Novelty and Contributions

1. **Dependency-aware evidence retrieval:** the LLM context is narrowed to graph-ranked suspect services, targeting cascading faults where the visible symptom is not the root cause.
2. **LLM never executes directly:** every proposed action passes a verifier agent and a deterministic policy engine (allowlist, risk score, approval, verification, rollback).
3. **Logs as untrusted input:** secret/PII redaction and a log-injection guard protect the agent pipeline (security angle for LLM-based operations).
4. **Reproducible open benchmark:** labelled fault catalogue, ground truth, replay bundles and a full ablation study running entirely on free infrastructure.

## 12. Safety, Ethics and Privacy

- Human-in-the-loop for medium/high-risk actions; the agent can only call allowlisted tools.
- All actions and reasoning traces are stored in a hash-chained audit log.
- Secrets and personal data are redacted before any external API call; only synthetic data is used in experiments.
- All faults are injected only into the project's own isolated test environment.
- Limitations of LLM reasoning (hallucination, non-determinism) are measured and reported, not hidden.

## 13. Timeline

**FYP-I (Semester 7)**

| Weeks | Work | Output |
|-------|------|--------|
| 1–2 | Literature review, finalise proposal, repo setup, context file | Approved proposal |
| 3–4 | M1 target application in Docker | Running app + load generator |
| 5–6 | M2 observability stack | Live dashboards |
| 7–8 | M3 fault injector (8 faults) | Labelled fault runner |
| 9–10 | M4 threshold detection, M6 agent v1 (read-only tools) | First automatic diagnosis |
| 11–12 | M8 allowlist + approval, M10 dashboard v1 | End-to-end loop |
| 13–14 | M12 basic evaluation (8 faults × 3 runs, B1/B2/B4) | First results |
| 15–16 | FYP-I report, demo, **MVP freeze (tag v1.0)** | Working submission |

**FYP-II (Semester 8)**

| Weeks | Work | Output |
|-------|------|--------|
| 1–3 | M5 dependency graph, M7 RAG, expand to 21 faults | Cascade faults diagnosed |
| 4–6 | M4 advanced detection (Drain3, Isolation Forest), tracing, verifier agent | Improved accuracy |
| 7–8 | M9 security layer, M11 Telegram + postmortem, M14 replay mode | Safe, documented incidents |
| 9–10 | M13 Azure IaC, CI/CD, cloud deployment | Live cloud demo |
| 11–13 | Full evaluation: baselines, ablations, statistics | Final results |
| 14–15 | Thesis report, demo video, polish | Final documents |
| 16 | Defense and viva preparation | Ready for defense |

*Adjust week numbers to the official university calendar.*

## 14. Deliverables

- Source code repository with documentation and one-command setup
- Fault catalogue with ground-truth labels
- Dashboard, Telegram bot and postmortem generator
- Evaluation data, plots and analysis
- Project report (FYP-I and FYP-II) and demo video
- Architecture diagrams and design-decision log

## 15. Risks and Mitigation

| Risk | Mitigation |
|------|------------|
| Free LLM tier rate limits | Replay mode, response caching, Ollama fallback, batch experiments |
| Scope too large | Tier system; MVP frozen at end of FYP-I; Tier 2/3 added only after MVP works |
| Hallucinated diagnosis | Evidence-citation validation, verifier agent, human approval |
| Azure student verification fails | Local demo is primary; cloud phase optional |
| Laptop resource limits with many containers | Container resource limits, tracing enabled only in Tier 2, selective experiments |
| Complex Docker networking bugs | Incremental modules, each independently tested |
| Over-reliance on AI-generated code | Code review, tests, decision log, every component explainable in viva |
| Noisy evaluation results | Repetitions, warm-up, fixed seeds, statistical reporting |

## 16. Budget

All software is free or open-source. Azure student credit is used only for the final deployment phase. Estimated additional cost: **0**.

## 17. Related Work and References (verify details before citing)

- Commercial AIOps products (for example Datadog Watchdog, Dynatrace Davis, PagerDuty AIOps) provide anomaly detection and alert correlation but are closed, paid systems.
- Chen et al., "Automatic Root Cause Analysis via Large Language Models for Cloud Incidents" (RCACopilot), EuroSys 2024.
- Pham et al., "RCAEval: A Benchmark for Root Cause Analysis of Microservice Systems with Telemetry Data."
- He et al., "Loghub: A Large Collection of System Log Datasets for AI-driven Log Analytics."
- He et al., "Drain: An Online Log Parsing Approach with Fixed Depth Tree," ICWS 2017.
- Beyer et al., *Site Reliability Engineering* (Google), O'Reilly, 2016.
- Basiri et al., "Chaos Engineering," IEEE Software, 2016.
- OWASP Top 10 for LLM Applications (prompt injection, sensitive information disclosure).

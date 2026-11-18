# Privacy and data protection

All data in this repository is **synthetic**. This document describes how the design would treat real employee data.

## Data flow to the AI model

| Data | Sent to the model? | Notes |
|---|---|---|
| The user's message | Yes | Needed to understand the request |
| Tool results for this user's request | Yes, **minimised** | Only fields the user is entitled to see; salary removed unless HR admin |
| Other employees' data outside scope | **Never** | The gateway refuses before anything is read |
| Absence data | Aggregates only | Groups under 5 people are suppressed; no names (AT-1.1) |
| Employee-typed free text | Wrapped as untrusted, length-capped | Leave reasons, handover notes |
| Credentials, tokens, API keys | Never | |

## Provider choice and retention

- **Public demo: Gemini API free tier.** Under Google's Gemini API terms, content sent on *unpaid* quota may be used to provide, improve and develop Google products. That is acceptable **only because every record in this demo is synthetic**. Never connect the free tier to real employee data.
- **Real deployment:** a paid, no-training endpoint under a DPA:
  - Gemini on a billing-enabled project or Vertex AI.
  - Or Claude via the Anthropic API or Amazon Bedrock (`HR_LLM_PROVIDER=anthropic|bedrock`): commercial traffic not used for training by default, zero data retention on request.
  
  Choose an EU region where required.
- **Rejected:** OpenRouter. It adds an extra data processor for employee data, charges platform fees, and its generic API drops provider-specific features.

## Principles applied

- **Data minimisation:** redaction before the model sees anything; aggregate-only analytics.
- **Purpose limitation:** tools are scoped to HR operations; there is no general data-export tool.
- **No solely automated decisions** (GDPR Art. 22, policy DP-1.2): the assistant recommends; humans approve.
- **Transparency:** reasoning trace and policy citations on every answer.
- **Accountability:** a tamper-evident audit of every read, denial, proposal and decision.
- **Retention:**
  - Sessions expire after 8 hours of inactivity.
  - The live demo sandbox is in memory and disappears with the browser session.
  - Production needs a defined audit retention period, e.g. 7 years for HR records, per local law.

## DPIA-lite (to complete before processing real data)

| Question | Answer in this design |
|---|---|
| Purpose | Help employees, managers and HR resolve HR requests faster and more consistently |
| Lawful basis | Legitimate interest / performance of the employment contract (confirm with Legal per country) |
| High-risk processing? | Yes: AI used in employment context (EU AI Act Annex III; obligations from 2 December 2027) |
| Main risks | Wrong advice; disclosure of colleague data; unfair absence profiling; over-automation |
| Mitigations | Engines + guards + evals; gateway scoping; k-anonymity and hypothesis framing; human approval |
| Residual risk / open items | Real SSO, DPA + zero data retention, retention schedule, works-council consultation where required |

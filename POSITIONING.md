# POSITIONING — the gaps in the crowded agent market, and what we sell into them

Compiled from the current public record (vendor docs, independent hands-on reviews, provider policy
pages) in October 2026. Every claim below is a documented restriction or a sourced review finding,
not an inference. This file exists so the product stops being "another agent that clicks" and starts
being the answer to five specific complaints.

## 1. Everyone can click. Almost nobody can recover.

**Gap.** Independent tests of the browser agents found the same failure shape: progress stops on a
login redirect, an MFA prompt, a CAPTCHA or a dynamic page, and the run either silently retries or
declares success. Understanding AI's ChatGPT Agent test hit a legitimate login redirect that the
safety layer blocked and needed the user to intervene; MIT Technology Review's Manus trial reported
CAPTCHA blocks, frozen pages and results that were incomplete without saying so.
([understandingai.org](https://www.understandingai.org/p/chatgpt-agent-a-big-improvement-but),
[technologyreview.com](https://www.technologyreview.com/2025/03/11/1113133/manus-ai-review/))

**What we sell instead.** Every action carries evidence, every task has written success criteria, and
a separate verifier checks the outcome rather than the steps (`zyntharo/ops/verification.py`,
`ops/preflight.py`, `ops/journal.py`). A blocked step becomes a named handoff — "MFA required on this
account, here is the screen" — not a silent retry. This is already the product's core; the new
connectors extend it to mail and drives, where a failed send is visible in one place.

## 2. "Watch it closely" is not a safety model.

**Gap.** Anthropic's own documentation states there is no sandbox between Claude and the apps it
controls, and advises monitoring; its Cowork safeguards were written after users asked how they could
verify what was done to their files. n8n shipped an agent failure mode where a tool error aborted the
workflow instead of being returned for recovery.
([support.claude.com](https://support.claude.com/en/articles/14128542-let-claude-use-your-computer-in-cowork),
[n8n issue 24042](https://github.com/n8n-io/n8n/issues/24042))

**What we sell instead.** Irreversible capabilities are a single list (`ops/risk.py`), they need the
owner's PIN, and file work goes through a journal with compensation and undo. Sending a message or an
email is `connector.send`: owner-only, confirmation-gated, never batched. A read-only agent can read
the mailbox; it cannot send from it even if it names the send endpoint, because the gate re-checks the
operation's own capability (`tests/test_connectors.py::GateTests`).

## 3. Your data goes to someone else's server.

**Gap.** Google's restricted-scope rules mean a public app that reads Gmail *through a server* needs a
security assessment; the same is true of any hosted agent that processes mail content. ChatGPT's
Windows computer use takes over the foreground and processes screen content; cloud agents reach local
files by tunnelling back to a desktop app.
([Google restricted scopes](https://developers.google.com/identity/protocols/oauth2/production-readiness/restricted-scope-verification),
[ChatGPT Computer Use](https://learn.chatgpt.com/docs/computer-use))

**What we sell instead.** ZYNTHARO runs on the customer's machine. Mail is read over IMAP locally, or
over the Gmail/Graph API from the local process; no mail content travels through a vendor server. That
is a compliance argument a small legal practice or clinic can actually accept, and it is why the
connector design uses the customer's own credentials instead of a hosted relay.

## 4. Nobody ships a desktop product without asking for a vendor account and a secret.

**Gap.** Hosted agents require a subscription plus their own connectors; Zapier and n8n meter tasks or
executions, so a retry-heavy workflow becomes unpredictable cost; Manus's credits depend on task
complexity. ([zapier.com](https://zapier.com/pricing), [n8n.io](https://n8n.io/pricing/),
[manus.im](https://manus.im/docs/introduction/plans))

**What we sell instead.** The customer's own tokens, one time each, stored in a DPAPI-protected local
vault. No per-task markup, no resale of a provider's API, no secret in the installer — which also
sidesteps the resale-terms problem entirely. Predictable: the customer pays their provider directly,
at their own rate.

## 5. The integration list is the product, and it is always shorter than the sales page.

**Gap.** Every competitor's weakest area is the same: what happens on the messy, unattended,
multi-application middle of a real office day — Outlook attachments into a spreadsheet, an invoice
folder into a ledger, a customer message into a drafted reply.

**What we sell instead.** Three workflows that end in a verified artifact (a filed invoice and a ledger
row, a draft waiting for approval, a report whose totals are read back), each with an acceptance test a
customer can run on their own PC (`PRODUCT.md`). The connectors are in service of those three jobs, not
a logo wall: 116 declared operations across 17 accounts, each marked `ready`, `configured`,
`needs_setup` or `unavailable` — never "supported" in the abstract.

## What we deliberately do not claim

Autonomy on Instagram/Messenger/X/LinkedIn/TikTok/Snapchat (platform review or paid tiers gate it), a
voice that is indistinguishable from a human on live calls, unattended GUI work on a locked PC, or a
connector proven live without the customer's credentials. Those are stated in `LIMITATIONS.md` with
the reason, because the first customer who discovers a claim was marketing is the last one.

## The one-line pitch this supports

> ZYNTHARO does three office jobs on your own Windows PC — invoices, replies, reports — using the
> accounts you already have, with every action verified, every irreversible step approved by you, and
> nothing sent to a vendor's cloud.

# ZYNTHARO — the product being sold

This file is the commercial definition, not the architecture. It exists so that every build
decision can be checked against one question: **does this help a paying customer finish one of
these three jobs on Windows?**

## Who it is for

A small business on Windows 10 or 11 — one to ten people — that already does repetitive
administrative work by hand: invoices, customer messages, and the monthly report. They are not
developers. They will not install Python, Node, Git, Playwright or a terminal.

## The promise

> Install ZYNTHARO on your Windows PC. Point it at your own folders and your own accounts. It files
> your invoices, prepares replies to customers for your approval, and produces your report — and
> it shows you exactly what it did.

Nothing is sent, deleted, paid, installed or called without the owner's approval.

## The three workflows (v1)

| workflow | input | output | verified by |
|---|---|---|---|
| **Invoice intake** (`invoice_intake`) | a folder where invoices arrive: PDF, DOCX, CSV, TXT, MD | files filed under `year/quarter/`, one row per invoice in an `.xlsx` ledger | the ledger is read back after every write; duplicates are skipped by SHA-256 |
| **Reply desk** (`reply_desk`) | a connected channel (Telegram or WhatsApp) | prepared replies waiting for approval, stored in a local SQLite outbox | a draft cannot be sent twice, cannot be sent through another platform, and is never sent unattended |
| **Recurring report** (`daily_report`) | the ledger produced above | a `.docx` summary and `.xlsx` breakdown | both files are re-read and the totals are checked before the run reports success |

No Microsoft Office installation is required for any of the three. If Office is installed, the
generated files open normally in Word and Excel.

## Connected accounts (v1)

The customer's own accounts, connected once, used by every workflow and by the agent:

| account | operations the product ships |
|---|---|
| Mailbox (IMAP/SMTP: Gmail, iCloud, Yahoo, Zoho, Fastmail, GMX, Proton Bridge, generic) | list folders, search, read, save attachments, send, mark, move/archive |
| Google (Gmail, Drive, Calendar, Sheets, Tasks, Contacts) | 14 operations, per-scope consent |
| Microsoft 365 (Outlook, Calendar, OneDrive, Excel, Teams) | 14 operations through Graph, device-code sign-in |
| Slack, GitHub, Notion, Airtable, Trello, Asana, Jira, HubSpot, Stripe, Dropbox, Zoom, Salesforce, Linear, Discord | 95 operations total; every endpoint is one the provider documents for token holders |

**The commercial rule that makes this sellable:** connecting is one action by the customer — an app
password, a token they generate, or a free app registration in their own tenant. There is no vendor
account in the middle, no per-seat API markup, and no client secret in the installer. Reading and
writing are separate permissions; sending mail or a message is always the owner's click.

## Channels: what is offered, and what is refused

A "connect everything" claim is a lie customers discover on day two. The product states the real
gate for each platform:

| platform | status | gate |
|---|---|---|
| Telegram | offered | official Bot API; a free bot token from @BotFather |
| WhatsApp | offered | bundled sidecar; the customer scans a QR once; unofficial web session, so account risk is disclosed |
| Discord | needs credentials | developer-portal bot token (`ZYNTHARO_DISCORD_TOKEN`) |
| Instagram / Messenger | not available | Meta business account, reviewed app, messaging permission |
| X / Twitter DMs | not available | paid API tier |
| LinkedIn, TikTok, Snapchat | not available | no third-party messaging API |

`zyntharo channels` prints this list with each platform's real state on the customer's machine.

## Platform and hardware

- **Windows 10 (1809+) or Windows 11, 64-bit.** Windows 11 is the primary test target; Windows 10
  is supported because it is still the majority install base.
- 8 GB RAM minimum for the three workflows; 16 GB recommended for browser automation, voice and
  vision at the same time.
- No Python, Node, browser runtime or coding tool is installed by the customer: the installer ships
  them.
- An AI provider key (BYOK) is required for agent-driven work. The workflows themselves are
  deterministic and run without a model.

## What the customer must supply

- Their own folders (invoice inbox, filing destination, report destination)
- A Telegram bot token or a WhatsApp pairing (only if they use the reply desk)
- Their own AI provider key, if they want the agent to plan work rather than run a named workflow
- Approval (PIN) for anything irreversible: sending, deleting, installing, calling

## The acceptance test per workflow

A workflow is only "ready to sell" when it passes this on a clean Windows machine with no
developer tools installed:

1. **Invoice intake** — put five real invoices in a folder, run it, and confirm: five rows in the
   ledger, five files under the right quarters, running it again adds nothing, and a corrupt file
   is reported rather than silently dropped.
2. **Reply desk** — send a message to the connected account, run it, confirm a draft appears and
   nothing was sent; approve it, confirm exactly one message arrives; approve again, confirm it is
   refused.
3. **Recurring report** — run it against the ledger, confirm both files open, and that the totals
   in the Word document match the ledger to the cent.

## Pricing (starting point, to be validated with the first five customers)

| offer | price |
|---|---|
| Paid pilot: one workflow installed and verified on the customer's PC | $500–1,500 one-off |
| Single seat, three workflows, updates, email support | $39–79 / month |
| Office (up to 5 seats), priority support, onboarding call | $299–799 / month |

## What is deliberately not claimed

- Zero CPU/RAM impact while doing heavy work. (Admission is capped, priority is lowered, and owned
  browsers are released — see `LIMITATIONS.md`.)
- Unattended desktop control while the screen is locked.
- Instagram/Messenger/LinkedIn/TikTok automation without the business approvals above.
- WhatsApp voice calls without a virtual audio device and a neural voice provider.
- "It never makes a mistake." The claim is: it verifies, refuses when unsure, and logs everything.

## Support promise v1

One business-day response by email for installation and workflow failures, with the exact
diagnostics the app already produces (`zyntharo doctor`, the capability matrix, the run's steps).
No 24/7 promise until there is staff for it.
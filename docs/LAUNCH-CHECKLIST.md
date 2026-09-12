# Launch checklist

Every tool in Coil, ordered by what it costs when it breaks rather than by where it sits
in the menu. Work down it. A pass is not "the page loaded", it is "I did the thing and
the result was right".

## Status

Every check below was run between 2026-09-09 and 2026-09-12, by Grok Bot with Claude
reproducing each failure and fixing it with a test. Commits `e051d89` through `12845ea`.
A ticked box means the check ran and either passed or was fixed and re-verified.

**How this file stays honest from here.** When a GitHub issue labelled `qa:reported`
touches one of these checks, the loop unticks that box and appends the issue number,
like `- [ ] ... (#42)`. When the issue reaches `qa:verified`, the box is re-ticked and
the number stays. A box that has been unticked is therefore a live problem, and one
carrying a number has a history. Nobody counts "done" from memory again: the file is
the count.

Open issues: https://github.com/Coil-Legal/coil/issues?q=is%3Aopen+label%3Aqa%3Areported

## How to report

For each item, say what you did, what you expected, and what happened. Three outcomes:

- **PASS** — did it, result was correct.
- **FAIL** — result was wrong. Include the URL, what you clicked, and the actual output.
- **BLOCKED** — could not test, and why.

Two rules that save everyone time:

**Check the value, not just the screen.** "Invoices page loads" is not a pass. "INV-1010
shows a $2,250.00 balance, which matches $4,500.00 total less $2,250.00 paid" is.

**Do not report an unconfigured integration as a bug.** Stripe, Twilio, IMAP and the voice
line are meant to be empty until a firm supplies its own credentials. See *Expected states*
at the end before filing anything.

## The demo data these numbers come from

Reload it any time with `python demo_data.py --clear` then `DEMO_EMAIL=ian@iandolan.com
python demo_data.py`.

| | |
|---|---|
| Matters | M-1008 Marchetti PI (contingency), M-1009 Okonkwo DWI (flat), M-1010 Bluebonnet contract (hourly) |
| Invoices | INV-1008 sent $525.00, INV-1009 paid $700.00, INV-1010 partial $4,500.00 with $2,250.00 outstanding |
| Trust | Okonkwo $2,250.00, Vance $1,600.00, total $3,850.00 |
| Documents | 6 demo files across PDF, DOCX, CSV, EML and plain text |
| Date of loss | 11 Feb 2026. Limitation date must therefore be 11 Feb 2028 |

---

# Tier 1 — Client money

A bug here costs a licence, not a customer. Test these properly even if it is slow.

## Trust accounting `/trust`

- [x] Ledger totals **$3,850.00** across two clients: Okonkwo $2,250.00, Vance $1,600.00.
- [x] Open each client ledger. Every entry shows a date, type, amount and running balance.
- [x] **Try to overdraw.** Disburse more than a client holds. It must **refuse**, not warn
      and proceed. This is the single most important assertion in the app.
- [x] **Try to cross matters.** Spend one matter's balance on another matter for the same
      client. It must refuse.
- [x] **Try to cross clients.** A disbursement for client A funded by client B's balance
      must be impossible to express at all.
- [x] Enter a bank statement at `/trust/reconcile` and run a three-way reconciliation.
      Ledger, client balances and bank must agree, and the one uncleared item must be
      listed as outstanding rather than silently absorbed.
- [x] Clear that item, reconcile again, confirm it now balances.
- [x] Trust deposit request `/trust/request-deposit` sends and records.
- [x] Applying trust to an invoice `/trust/apply` moves money and leaves both sides right.

## Invoices `/invoices`

- [x] INV-1010 shows **$2,250.00 outstanding**, not $4,500.00.
- [x] INV-1009 is paid and appears in **no** AR aging bucket and gets **no** reminder.
- [x] Create an invoice from unbilled time on M-1010. Hours and rate produce the right total.
- [x] Edit a draft, add a line, adjust, discount. Totals recompute.
- [x] Approval is opt-in: turn on "require invoice approval" in Settings, then submit,
      approve and reject are available on a draft and recorded in the audit log. With it
      off there is deliberately no approval step.
- [x] Void an invoice. It stops counting toward AR and cannot be paid.
- [x] Interest: set an interest rate in Settings first, or the interest action has nothing
      to do. Then `/invoices/<id>/interest` adds one line, not one per run.
- [x] Monthly bulk billing is opt-in per matter: set a billing day on the matter, otherwise
      `/invoices/bulk/monthly` correctly skips it and says why.
- [x] Bulk monthly run `/invoices/bulk/monthly` skips matters it should skip and says why.
- [x] PDF renders with the firm's name and correct figures. A logo only appears once one
      is uploaded in Settings, Invoice template; no logo on a fresh firm is correct.
- [x] Public view `/p/<token>` shows the invoice without a login and without leaking
      anything about other matters.

## Payments and payment plans

- [x] `/payments/record` records a manual payment and updates the invoice balance.
- [x] A payment plan at `/money/plans/new` schedules correctly and the arithmetic sums to
      the invoice total.
- [x] Pause, resume and cancel a plan.
- [x] With no Stripe key, pay links show mailing instructions rather than erroring.

## Fee splits and compensation

- [x] Fee splits live per matter at `/money/splits/<matter_id>`, reached from the matter,
      not from a bare `/money/splits`. Origination and working splits total 100%.
- [x] The compensation report at `/reports` agrees with the underlying time entries.

---

# Tier 2 — Deadlines and conflicts

Missing one of these is malpractice.

## Conflict check `/conflicts`

- [x] Search "Nordvale". It must find the adverse party on M-1008.
- [x] Search inside documents: "Bluebonnet" appears in the contract and should surface.
- [x] The intake notes flag that the firm acts for Bluebonnet, which contracts with
      Nordvale, the adverse party. Does the check surface that relationship?
- [x] A near-miss spelling still matches.

## Calendar, deadlines and court rules

- [x] M-1008's limitation date is **11 Feb 2028**, exactly two years from the 11 Feb 2026
      date of loss in the medical records. Verify it against the documents rather than
      trusting the field.
- [x] Create a deadline chain from a trigger date. Dates land on business days.
- [x] Recurring events repeat correctly and stop at their end date.
- [x] The iCal feed subscribes and shows the same events.
- [x] Court rules `/rules` applies a starter set. Note it is marked *partial* on the
      website: generic sets, not real jurisdiction rules. Confirm it does not present
      itself as more.

## Tasks `/tasks`

- [x] The overdue task shows as overdue.
- [x] Standard PI task set `/pi/<id>/tasks/standard` creates once, not twice on re-run.

---

# Tier 3 — Clients see this

## Client portal `/portal`

- [x] Magic-link login works and expires.
- [x] A client sees **only** their own matters, invoices, documents and messages. Try to
      reach another client's record by editing the URL. It must refuse.
- [x] Spanish rendering, if the contact's language is set.

## E-signature `/signatures`

- [x] Send an engagement letter, sign it, and confirm the certificate records who, when,
      where and what was signed.
- [x] A signed document cannot be altered afterwards.

## Intake `/intake`

- [x] Public form submits and creates a lead.
- [x] Convert a lead to a matter. The conflict check runs as part of it.
- [x] Pipeline drag and drop moves a lead between stages and persists.
- [x] Sequences draft rather than send while auto-send is off.

## Messages `/messages`

- [x] A portal message reaches the client and the reply lands on the matter.
- [x] Without Twilio, texts are stored and clearly not sent.

---

# Tier 4 — Documents and practice areas

## Documents `/documents`

- [x] Upload, version, and confirm the older version is still retrievable.
- [x] Full-text search finds **inside** files: "epidural" (PDF), "indemnify" (DOCX),
      "light duty" (EML), "Lakeshore" (PDF and CSV).
- [x] Folders and tags filter.
- [x] Document automation `/doctemplates` fills a Word template from a matter.

## Personal injury `/pi`

- [x] Providers, records requests and bills requests generate correct letters.
- [x] Liens: add, edit, and generate a reduction letter.
- [x] **Settlement worksheet.** Gross, fees at the contingency rate, costs, liens and net
      to client must add up. Costs already billed must not be counted twice.
- [x] Approve and disburse posts to trust correctly, and the trust ledger reflects it.
- [x] Demand package assembles the right documents.

## Criminal defense `/criminal`

- [x] Charges with attorney-entered ranges.
- [x] Court date chain generates.
- [x] Speedy-trial calculation is right, and says what it is counting from.
- [x] Disposition PDF renders.

## Discovery and depositions `/discovery`

- [x] Propound from a starter set; respond to served discovery with objections flagged.
- [x] Deposition summary from `deposition-excerpt-demo.txt` produces a summary with page
      and line cites.
- [x] **Save with an empty summary box keeps the existing summary** and warns. Clearing it
      requires ticking the box. (This was a real bug; confirm the fix.)

## Research `/research`

- [x] Case law search returns results.
- [x] Full opinion text loads, now that a CourtListener token is set.
- [x] **Cite check on `brief-with-citations-demo.txt`.** Six citations. Celotex, Anderson
      and Matsushita are real and must resolve. Marbury and Vasquez-Lindberg are invented
      and must come back not found. **Halloway 812 F.3d 1144 is the interesting one**: that
      page is a real pincite into *Tubbs v. Surface Transportation Board*, so the number
      resolves while the case name does not. It must be reported as **wrong case**, never
      as resolved.
- [x] Confirm it does not claim to say whether a case is still good law. It is not a
      citator and must not imply it is.

---

# Tier 5 — AI

Currently OpenRouter with `google/gemini-2.5-flash`, on the firm's own key.

- [x] `/ai` shows available, the model, and today's spend.
- [x] Ask Coil answers a plain question about a matter.
- [x] Matter summary, invoice narrative polish, dates from documents.
- [x] Records to chronology from `medical-records-demo.pdf` produces a dated treatment
      timeline ending at maximum medical improvement on 9 July 2026.
- [x] Case audit sweep runs and flags something real.
- [x] PI case scoring produces a score with reasons.

### Does it make things up

The demo documents contain deliberate traps. These matter more than the happy paths.

- [x] **The deposition contradicts itself**: a twenty-minute fuel stop, then the logbook
      says forty-five, then "I don't remember exactly". A summary that reports one figure
      as fact is wrong. Run it twice: contradiction detection has been inconsistent.
- [x] **The email corrects the intake notes**: light duty for three weeks, not four. Does
      anything notice the newer document supersedes the older?
- [x] **The demand letter concedes** the C6-C7 changes are chronic and not from the
      collision. A summary that omits that is telling you what you want to hear.
- [x] **The CSV totals $22,607.00**, matching the demand exactly. A tool reporting a
      different figure has an arithmetic problem.
- [x] Ask the AI something the documents do not answer. It should decline rather than
      invent.

---

# Tier 6 — Platform

## API and MCP

- [x] Token scopes: a `matters:read` token exposes 3 MCP tools; a full token exposes 17.
- [x] Ask a read-only token to log time. It should say it cannot, not fail trying.
- [x] **Withheld mode.** With a redacted token, "Marchetti" must not appear in any API or
      MCP response. Then repeat with a full token and confirm it does. Absence alone
      proves nothing.
- [x] `/api/v1/documents` returns metadata only, never file bytes or the filesystem path.
- [x] Rate limit returns 429 with Retry-After. Note the counter is per worker: with
      WEB_CONCURRENCY=2 the advertised 120 a minute is enforced as 60 per worker, so
      sequential calls over a slow link may never trip it. Hammer it from one connection.
- [x] Full walkthrough in `mcp/TESTING.md`.

## Exports, import, webhooks

- [x] `/exports` produces CSV for contacts, matters, time, invoices, payments and the full
      trust ledger. Open one and confirm the figures match the screens.
- [x] Importer `/importer`: run a Clio-shaped CSV through preview, confirm the mapping,
      commit, and check nothing unrelated was overwritten.
- [x] Failed-rows CSV downloads and explains each failure.
- [x] Webhooks fire on task.completed and matter.closed, retry on failure, and the secret
      is never shown to a readonly user.

## Backup and restore

- [x] `python -m app.cli backup` writes to `data/backups`, keeps the newest 14.
- [x] Extract one and run `PRAGMA integrity_check` on the database inside it.
- [x] Confirm the archive survives a container rebuild.

---

# Tier 7 — Setup and administration

## Setup guide `/setup-guide`

- [x] All five steps render, each saying what it does, whether you need it, and the cost.
- [x] Skip a step. It records as skipped and moves on without changing anything.
- [x] Come back and complete a skipped step.
- [x] Save a value, then save again with the box blank. The stored value survives.
- [x] Type `none` to clear one.

## Settings `/settings`

- [x] Firm details, offices, users and roles.
- [x] **Permissions.** Log in as readonly and confirm trust and settings are unreachable.
      A paralegal must not reach trust.
- [x] Invoice template: logo, colours, columns, wording, preview.
- [x] AI settings: model, cap, provider choice, and both privacy toggles.
- [x] Choosing "Anthropic directly" shows the warning that Coil cannot enforce retention
      for you.
- [x] API tokens: the scope grid, the confidentiality choice, revoke.
- [x] Audit log records every destructive action with who and when.

## Feedback

- [x] The sidebar link submits and reaches us, carrying the page, build and firm.

---

# Expected states, not bugs

Do not file these.

| What you will see | Why it is correct |
|---|---|
| Stripe, Twilio, IMAP unset | The firm supplies its own. Every feature degrades with an explanation. |
| Voice line disabled | Needs Twilio and a deliberate switch-on in Settings. |
| Trust reconciliation "out of balance" with an uncleared item | That is what a reconciliation is for. Enter a bank statement to test it properly. |
| No AI key on a hosted instance until the firm adds one | Coil never covers inference. The environment is deliberately ignored on hosted instances. |
| Court rules described as generic starter sets | Marked *partial* on the website. Honest, not incomplete. |
| Cite check does not say whether a case is still good law | CourtListener has no citator. Stated on the page. |
| Interior screens cramped on a phone | One breakpoint at 1000px; the site says the screens are laid out for a desktop. |

# When you are done

Report by tier, worst first. For anything that failed, give the URL, the steps, expected
versus actual, and whether you could repeat it. If something failed once and passed on
retry, say so: intermittent is a different problem from broken, and it is usually the
model rather than the code.

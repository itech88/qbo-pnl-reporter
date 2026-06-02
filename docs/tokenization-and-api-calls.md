# Tokenization and API Calls — A Working Explanation

*An architect's note for a business systems analyst.*

This essay explains how software talks to other software over the internet, how
that conversation is secured with **tokens**, and then how those general ideas show
up concretely in the QBO P&L Reporter. You don't need to write code to follow it —
but by the end you should be able to read our logs, reason about what can go wrong,
and explain the integration to a stakeholder with confidence.

---

## Chapter 1 — What an API call actually is

When our reporter needs the practice's profit-and-loss numbers, it does not log into
the QuickBooks website and read the screen the way a person would. Instead it makes
an **API call**: a structured request, sent over the internet, to a specific address
QuickBooks publishes for exactly this purpose. "API" stands for *Application
Programming Interface* — think of it as a service window built for machines rather
than people. A person uses the *user interface* (buttons and screens); a program uses
the *programming interface* (addresses and structured messages).

A single API call has two halves: a **request** and a **response**. Our program sends
a request that says, in effect, "GET me the ProfitAndLoss report for company X, from
January to June, summarized by month." QuickBooks does the work and sends back a
**response** — a structured bundle of data (in a format called JSON, essentially a
labelled, nested list of values) that our program can read reliably every time,
because the shape is predictable. This predictability is the whole point of an API:
the screen layout of the QuickBooks website can change, but the API's agreed-upon
request and response formats stay stable, so our automation doesn't break when a
designer moves a button.

Each request also carries an **HTTP verb** that states intent — `GET` to read data,
`POST` to submit or change something — and a set of **headers**, which are small
labelled notes attached to the request. Headers are where a lot of the important
plumbing lives: what format we want back, and — critically for this essay — *who we
are and whether we're allowed*. That last point is the crux. QuickBooks holds a real
business's financial records. It cannot simply hand them to anyone who knows the right
address. Every meaningful API call must answer one question before it gets any data:
**"Prove you're allowed to ask this."** How that proof works is the subject of the
rest of this essay.

---

## Chapter 2 — Tokens: how machines prove they're allowed

The naïve way to prove identity would be to send a username and password with every
request. This is a bad idea, and modern systems avoid it. If a program had to carry
the practice's actual QuickBooks password and send it on every call, that password
would be sitting in dozens of places — config files, log files, network traffic — and
any one leak would hand over full, permanent access. We need something safer: a
credential that is *limited*, *temporary*, and *revocable*. That credential is a
**token**.

A token is a long, unguessable string of characters that represents permission. The
useful analogy is a **hotel keycard**. When you check in, you show your ID and payment
once at the front desk; in return you get a keycard. For the rest of your stay you
open your door with the card, not your passport. The card only opens *your* room
(limited), it stops working at checkout (temporary), and if you lose it the desk can
deactivate it without changing anything about you (revocable). A token works the same
way: the program presents it on each API call, and the service grants exactly the
access that token represents — nothing more, and not forever.

The dominant standard for issuing and using these tokens is called **OAuth 2.0**, and
QuickBooks uses it. OAuth introduces a deliberate split between two kinds of tokens,
and understanding this split explains almost everything about how our system behaves.
The first is the **access token** — the keycard you actually tap on the door. It is
sent on every API call (in an `Authorization: Bearer <token>` header, "bearer" meaning
"whoever holds this may use it") and it is intentionally **short-lived**, typically
expiring in about an hour. A short life is a security feature: if an access token
leaks, it's useless within an hour.

But an hour is far too short for an unattended system — we can't ask a human to log in
every hour. That's the job of the second token, the **refresh token**. The refresh
token is not used to read data; its only purpose is to obtain new access tokens. Think
of it as the standing arrangement you have with the front desk that lets you get a
fresh keycard when yours expires, without re-proving your identity from scratch. The
refresh token lives much longer (QuickBooks: up to 100 days) and is used rarely — only
when the access token has run out. This two-token design is the elegant compromise at
the heart of OAuth: the credential used *constantly* is *disposable*, and the
credential that is *durable* is used *seldom and kept private*.

There is one more wrinkle that matters greatly for us: **refresh tokens rotate**. Each
time QuickBooks issues a new access token from a refresh token, it may also hand back a
*new* refresh token and retire the old one. This is a security ratchet — a stolen
refresh token is only good until the next legitimate refresh quietly replaces it. It
also means our system must always *capture and save* the new refresh token it gets
back, or it will find itself holding an expired one. Chapter 4 is largely about getting
that one detail right.

---

## Chapter 3 — How this solution authenticates to QuickBooks

Now we can trace the real thing. Before any automation runs, there is a **one-time
setup** in which a human grants consent. Someone with access to the practice's
QuickBooks logs in through an Intuit-hosted page and approves our app's request to read
accounting data (and only accounting data — that's the *scope*,
`com.intuit.quickbooks.accounting`). Intuit then issues the very first access token and
refresh token. This consent step happens once; everything afterward is automatic. Our
app is identified during this exchange by two fixed values issued when the app was
registered — a **Client ID** and **Client Secret** (the app's own username and
password, distinct from any user's) — and the specific company we're reading is
identified by a **Realm ID**, essentially the QuickBooks company number.

From then on, every run authenticates without a human. The logic lives in `auth.py`.
When the reporter starts, it checks the stored access token's expiry. If the token is
expired or within five minutes of expiring, it proactively performs a **refresh**: it
sends the refresh token to Intuit's token endpoint (authenticating itself with the
Client ID and Secret) and receives a fresh access token — and, per the rotation rule, a
possibly-new refresh token, which it saves. Only then does it proceed to call the data
API. This "refresh first if needed" check means we almost never hit an expired-token
error in normal operation.

The actual data calls go through a small wrapper we built called `QBOSession`. Every
request it sends automatically attaches the current access token in the
`Authorization: Bearer` header, so no individual piece of code has to remember to
authenticate — it's handled in one place. The wrapper also includes a safety net: if a
call ever comes back with **HTTP 401 Unauthorized** (the server's way of saying "your
token isn't valid"), the wrapper refreshes the token once and retries the call. So we
have two layers of defense against token expiry — a proactive check before we start,
and a reactive retry if the server rejects us mid-run.

A run reads the **ProfitAndLoss** report in six calls (two half-year windows for each
of three years, because Intuit recommends keeping each report request to six months),
and the COGS-by-Vendor report adds its own six calls against the more detailed
**ProfitAndLossDetail** endpoint. One more operational detail worth knowing: every
QuickBooks response carries a header called **`intuit_tid`**, a unique transaction ID
for that specific call. We capture it in our logs on every request. If we ever need to
open a support ticket with Intuit about a failed call, that ID lets them find the exact
interaction on their side — a small thing that turns a frustrating "it didn't work"
into a precise, answerable question.

---

## Chapter 4 — Keeping it alive unattended: rotation, writeback, and failure

Everything so far would be straightforward if our program ran on a single computer that
stayed on. It does not. The reporter runs on **GitHub Actions**, on a fresh, temporary
machine that is created for the run and destroyed when the run finishes. This is good
for security and cost, but it creates a subtle problem that took real care to solve.
Recall that refresh tokens rotate — each run may receive a new one that must be saved
for next time. On a normal computer, "saved for next time" means writing to a local
file. On a throwaway machine, that file vanishes the moment the run ends. If we did
nothing else, each run would obtain a new refresh token, write it to a disk that is
immediately incinerated, and the *next* run would wake up holding a refresh token that
QuickBooks has already retired — and authentication would fail.

The fix is what we call **token writeback**. After a successful refresh, our code
writes the rotated tokens back into **GitHub Secrets** — GitHub's encrypted store for
sensitive values — which *does* persist between runs and is injected into each new run
as it starts. In effect, each run leaves the next run a fresh keycard arrangement in a
secure lockbox before it shuts down. This is why the integration can go fifteen days
between the 1st and the 16th and still authenticate cleanly: the refresh token in the
lockbox is never more than one run old.

Writing to that lockbox itself requires permission, and this is where the one
credential we cannot automate enters. GitHub's automatic, built-in run credential is
deliberately *not* allowed to modify secrets (a sensible safety default). So the
writeback authenticates with a separate, human-created **Personal Access Token**, which
we store as the secret `GH_PAT`, narrowly scoped to only write secrets on only this
repository. Every token in the system rotates itself except this one — which means it
is also the system's one quiet single point of failure, and worth understanding plainly.

If the `GH_PAT` ever expires without being replaced, the failure is **delayed and
two-staged**, which can be confusing if you don't expect it. On the *first* run after
the PAT expires, the report still sends and the run looks **green** — only the
writeback fails, leaving a single error line in the log and freezing the stored refresh
token. On the *next* run, roughly two weeks later, authentication fails outright,
because QuickBooks rotated the refresh token on that first run and retired the frozen
copy we never updated. At that point reports stop, and recovery requires *both* issuing
a new PAT *and* re-doing the one-time QuickBooks consent to mint fresh tokens — because
the stored QuickBooks refresh token is, by then, genuinely dead. The practical lesson is
prevention: rotate the PAT *before* it expires and you simply swap one value; let it
lapse and you have a two-step recovery. (The exact procedure lives in the README
runbook.)

Finally, because an unattended system that fails silently is worse than useless, the
solution **alerts on failure**. If any report errors, the others still run (one failure
doesn't sink the batch), the run is marked failed so it's visible, and an email goes out
naming what broke. A second, infrastructure-level safety net catches the rarer failures
the program can't report on its own — such as the machine being killed mid-run, or the
writeback step itself failing. Taken together, these pieces are what let a financial
integration run month after month with no one watching: short-lived keycards for daily
use, a private long-lived arrangement to renew them, a secure lockbox to carry that
arrangement across disposable machines, and a smoke alarm for the one wire that can't
rotate itself.

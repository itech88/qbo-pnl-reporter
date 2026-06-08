# Business Requirements — Use Cases (Gherkin)

Requirements are expressed as behaviour scenarios in Given/When/Then form. They
describe *what the system must do* from the business owner's and operator's point
of view, independent of implementation.

---

## Feature: Automated monthly P&L reporting

As the practice owner
I want my key P&L metrics emailed to me automatically
So that I can make spending and operating decisions without pulling reports by hand.

```gherkin
Scenario: Reports are delivered on the 1st of the month
  Given the schedule fires at 10 AM Pacific on the 1st
  And QuickBooks Online has data for the most recent completed month
  When the pipeline runs
  Then I receive the 8 metric report emails and the COGS by Vendor email
  And I receive the Monthly Business Dashboard scorecard
  And each email has a summary table and an embedded chart

Scenario: Reports are delivered on the 16th of the month
  Given the schedule fires at 10 AM Pacific on the 16th
  When the pipeline runs
  Then I receive the 8 metric report emails and the COGS by Vendor email
  But I do NOT receive the scorecard
  # The scorecard is a start-of-month overview (trigger_days: [1])

Scenario: A report reflects the most recently completed month
  Given today is the 1st and the current month has no posted data yet
  When the scorecard is built
  Then it reports on the prior (completed) month, not the empty current month
```

---

## Feature: Cost-of-goods and expense ratio analysis

As the owner
I want each tracked metric shown as a dollar amount and as a percentage of income
So that I can judge both absolute spend and efficiency.

```gherkin
Scenario: COGS shown as a percentage of income
  Given a month with income of 45,000 and COGS of 13,500
  When the COGS report is generated
  Then the month shows a COGS ratio of 30.0%

Scenario: A month with no income does not divide by zero
  Given a month with income of 0
  When any ratio metric is computed
  Then the percentage is shown as "—" rather than an error

Scenario: Net Operating Income can be negative
  Given a month where expenses exceed gross profit
  When the Net Operating Income report is generated
  Then the negative value is shown in red and the chart scales correctly
```

---

## Feature: Year-over-year comparison

As the owner
I want each metric compared to the same month in prior years
So that I can account for seasonality instead of reacting to normal swings.

```gherkin
Scenario: Same-month comparison across three years
  Given data exists for May 2024, May 2025, and May 2026
  When the year-over-year view is built for May
  Then each year appears as its own column for that month

Scenario: Missing prior-year data is tolerated
  Given a month has data in 2026 but not 2024
  When the year-over-year view is built
  Then the 2024 cell shows "—" and the report still renders
```

---

## Feature: Anomaly flagging

As the owner
I want months that deviate sharply from history highlighted
So that problems surface without me scanning every number.

```gherkin
Scenario: Flag a month that exceeds the variance threshold
  Given the 3-year average COGS ratio for May is 30%
  And the configured threshold is 5 percentage points
  When the current May COGS ratio is 50%
  Then May is flagged as an anomaly with direction HIGH

Scenario: Do not flag normal variation
  Given the current value is within the threshold of the 3-year average
  When anomalies are evaluated
  Then no flag is raised

Scenario: Do not flag without enough history
  Given there is only current-year data and no prior years
  When anomalies are evaluated
  Then no flag is raised
```

---

## Feature: COGS broken down by vendor

As the owner
I want to see which suppliers my COGS dollars go to each month
So that I can manage purchasing and spot mis-coded spend.

```gherkin
Scenario: Vendors are discovered dynamically
  Given suppliers may be added or removed in QuickBooks over time
  When the COGS by Vendor report runs
  Then it includes every vendor that posted to a COGS account that period
  And no vendor list is hard-coded

Scenario: Credit-card charges without a payee are attributed by memo
  Given a COGS charge imported from the bank feed has no Payee assigned
  And its memo descriptor is "KAZAK-MARS, INC."
  When the report is generated with memo fallback enabled
  Then the spend is attributed to "Kazak-Mars" rather than "Unattributed"

Scenario: Vendor name spellings are canonicalized
  Given a vendor appears as "COOPERVISION, INC." on a card charge
  And as "CooperVision" on a bill
  When the alias map maps both to "CooperVision"
  Then they merge into a single vendor row

Scenario: Truly unidentifiable spend remains visible
  Given a COGS charge has neither a Payee nor a usable memo
  When the report is generated
  Then the spend is shown as "Unattributed" rather than dropped

Scenario: The vendor breakdown tracks the same month as the metric reports
  Given the metric reports are reporting the current month as partial (month-to-date)
  When the COGS by Vendor report runs
  Then it reports that same month, flagged partial
  And it does not lag to the prior month just because vendor bills post late

Scenario: The current month has no COGS posted yet
  Given the reporting month is the current month and no COGS bill has posted
  When the COGS by Vendor report runs
  Then it shows that month with a "no COGS posted yet" note and a $0 total
  # On the 1st the reporting month is the just-completed prior month, so this
  # empty state only appears mid-month before the first vendor bill lands.
```

---

## Feature: Monthly business scorecard

As the owner
I want a single at-a-glance health summary
So that I know whether to act this month without opening every report.

```gherkin
Scenario: Traffic-light status per metric
  Given each metric has a 3-year same-month average
  When the scorecard is built
  Then each metric shows ON TRACK, WATCH, or ACTION based on its deviation
  And "higher is better" metrics (Gross Profit, NOI) color improvement as good

Scenario: Scorecard summarizes all tracked metrics
  Given the 8 data reports have been computed
  When the scorecard is built
  Then it includes one row per included metric with this-month, 3-yr avg, and deviation
```

---

## Feature: Unattended credential management

As the operator
I want the integration to keep itself authenticated between runs
So that monthly reporting does not silently break.

```gherkin
Scenario: Access token refreshed before each run
  Given the stored access token is expired or near expiry
  When the pipeline starts
  Then it refreshes the token before calling the QBO API

Scenario: Rotated refresh token is persisted
  Given QuickBooks rotates the refresh token during a run
  And the run is executing in CI with a valid GH_PAT
  When tokens are saved
  Then the rotated tokens are written back to GitHub Secrets for the next run

Scenario: Local runs never touch secrets
  Given the pipeline is run on a developer machine without GH_PAT
  When tokens are refreshed
  Then they are written only to the local .env, not to GitHub Secrets
```

---

## Feature: Failure visibility

As the operator
I want to be told when a run fails
So that a broken integration does not go unnoticed.

```gherkin
Scenario: One failing report does not stop the others
  Given one report raises an error mid-run
  When the pipeline continues
  Then the remaining reports are still generated and sent
  And the run exits non-zero so the failure is visible

Scenario: Partial failure sends an alert naming the report
  Given at least one report failed and the run is not a dry run
  When the pipeline finishes
  Then an alert email lists the failed report(s) and the run id

Scenario: Infrastructure failure still alerts
  Given the Python process is killed or dependencies fail to install
  When the scheduled workflow step fails
  Then the workflow's failure step emails an alert with the run URL
```

---

## Feature: Safe operation and testing

As the operator
I want to preview output without emailing the owner
So that I can validate changes safely.

```gherkin
Scenario: Dry run produces previews and sends nothing
  Given the pipeline is invoked with --dry-run
  When it runs
  Then it writes preview_*.html files
  And no email is sent

Scenario: Run a single report on demand
  Given I need only one report
  When I invoke the pipeline with --report "<name>"
  Then only that report is processed
```

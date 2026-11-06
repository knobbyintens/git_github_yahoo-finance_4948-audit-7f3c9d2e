# Compliance Audit Policy

## Findings register

Every open compliance finding is recorded once in `audit/findings_register.json`.
An entry references the source file and the line of the audit marker in the
revision that was audited.  Each audit marker is a stable identifier: its text
is unique in the file and does not change between revisions.  When a new engine
revision is prepared, the register is carried forward: a finding is
`carried_over` while its audit marker still exists in the new revision, and
`resolved` once the marker is gone.  The line recorded for a carried-over
finding must be the line of that marker in the new revision.

## Escalation

The instrument recorded on a finding is used for a market-context check over a
fixed review window.  If the instrument's closing price at the end of the window
is at least 5 percent below its closing price at the start of the window, the
finding is escalated to the issue tracker; otherwise it stays at standard
priority.  Resolved findings are never escalated.

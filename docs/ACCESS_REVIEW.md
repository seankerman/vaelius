# Access reviews and offboarding

Vaelius supports periodic user-access reviews and offboarding of the kind SOC 2
(CC6.2, CC6.3) and ISO/IEC 27001:2022 (A.5.18 access rights, A.8.2 privileged access
rights) expect. You get a content-free inventory of who can reach a tenant and
how. A reviewer records an approve or revoke decision for each line. Revokes take
effect immediately and are audited.

Nothing here reads or exports source text, claims, tokens or credential digests.
Owned content is never deleted by a review or by offboarding. Retention and legal
hold govern it separately.

## What the report contains

`python -m agenthub.access_review ... report` (or the admin route below) lists every
principal in the tenant, plus any principal that a grant or binding names but the
tenant does not know. For each principal it shows:

| Field | Source |
|---|---|
| `identity_bindings` | Active control-plane `(issuer, subject)` bindings and their last observed use |
| `directory`, `groups` | Latest SCIM user state and the directory (`scim`) groups that include the user |
| `credentials` | Each enrollment with a live access token or refresh session: count, expiry, actions, last use |
| `granted_actions`, `privileged_actions`, `raw_source_read` | Union of live credential actions. `source_read` (raw original access), `manage` (workspace administration) and `moderate` (older credentials only) are called out on their own |
| `workspace_access` | Every workspace the principal can read, their effective role, and how: a direct `principal` entry, each group that gives them access (with its source and `sync_state`), and/or the organization entry |
| `workspace_entries` | Access-list entries whose subject is this principal: workspace, role, granted by, created |
| `group_memberships` | Local-group memberships (revocable here) |
| `repo_links` | Repository remotes this principal linked to a workspace |
| `document_grants` | Reviewed-release documents that name the principal as a reader |
| `connection_reader_grants`, `owned_connections` | Connector reader lists that name the principal, and connectors they own |
| `delegations` | Delegations the principal holds (`acts_for`), and delegates acting for them (`represented_by`) |
| `last_activity_at` | Latest audited action, credential issuance, refresh rotation or delivery receipt |

`lines` flattens this into one reviewable entitlement per row. A line has a stable
`line_id` (the same entitlement keeps its ID across reports), a `kind`
(`principal`, `settings_admin`, `credential`, `binding`, `workspace_access`,
`workspace_entry`, `group_membership`, `synced_group_membership`, `repo_link`,
`document_grant`, `connection_reader`, `delegation`), a content-free target,
actions, last use and flags:

| Flag | Meaning |
|---|---|
| `stale` | No observed use within `stale_days` (default 90) |
| `never_used` | A binding no credential has ever been issued through |
| `privileged` | Holds `audit`, `curate`, `moderate`, `policy`, `settings` or `source_read`, or is a tenant settings administrator; set together with `admin_role` for workspace entries whose role is `admin` |
| `raw_source_read` | Can read raw originals |
| `admin_role` | A workspace role of `admin`, on an entry or a group membership |
| `org_wide_entry` | An organization entry on a workspace; listed in the per-workspace summary |
| `group_stale` | The access is derived from a group whose `sync_state` is not `current` |
| `suspended_by_tenant_policy` | The entry is wider than the tenant's `widest_entry` and so contributes no role at query time |
| `delegated` | Credential acting for another principal |
| `directory_deactivated` | Active in Vaelius although the latest SCIM state says inactive |
| `directory_reconcile_pending` | An out-of-order SCIM event is waiting for reconciliation |
| `orphaned:<reason>` | A live entitlement that points at an inactive or unknown principal, workspace or group, a missing delegation, or an explicit grant that never took effect |
| `unattributed_grant` | An active membership that is neither explicit nor directory-derived (legacy rows) |

The report's `workspaces` summary lists every organization entry (`org_wide_entry`),
the stale groups, the current `widest_entry` setting, and every workspace without
a counting admin (`without_admin` — managed by tenant settings administrators until
one is added). `project_grant` lines are gone; their equivalent is now
`workspace_entry`, `group_membership` or `synced_group_membership`.

Last use is derived from records Vaelius already keeps: credential issuance, refresh
rotation and delivery receipts. Vaelius-issued access tokens live at most 24 hours,
so continued use always leaves one of these records. Tokens issued by an external
authorization server are recorded the first time Vaelius sees each one. The derived
time is therefore precise to one access-token lifetime, which is ample for a
threshold measured in days.

Output is JSON or CSV. CSV cells that would start a spreadsheet formula are prefixed
with `'`, because subjects and directory IDs come from external systems.

## Who runs it

A **tenant access reviewer** is an active principal with the tenant settings
administrator flag who holds `audit` or `policy`. Revoking also requires `policy`.
`policy` alone is not enough, because ordinary owners hold it to narrow their own
sources. Delegated (`acting_for`) credentials are refused. A reviewer can never
decide a line about their own access. Have someone outside the team that granted the
access review it.

An **operator** with the private profile can run the same review from the command
line. Only the operator can finish control-plane changes (deactivating an identity
binding), because the serving role cannot write the control database.

## Procedure

1. Open a review. This snapshots the current report and records a hash of the lines:

   ```sh
   python -m agenthub.access_review --profile PROFILE --tenant TENANT review-open --reviewer security-officer
   ```

   or `POST /enterprise/v3/access-review/reviews` with `{"stale_days": 90}`.
2. Export the lines for the reviewers (`report --format csv --output FILE`, or
   `POST .../report` with `{"format": "csv"}`). Start with lines flagged
   `privileged`, `raw_source_read`, `stale`, `directory_deactivated` or `orphaned:*`.
3. Decide every line:

   ```sh
   python -m agenthub.access_review --profile PROFILE --tenant TENANT review-decide \
     --review rev_... --line ar_... --decision revoke --reviewer security-officer \
     --comment "Left the project team"
   ```

   or `POST .../decisions` with `{"review_id", "line_id", "decision", "comment"}`.
   A decision is final. Repeating the same decision is harmless and retries an
   unfinished revocation. A different decision on a decided line is a conflict.
4. Close the review (`review-close` / `POST .../close`). Closed reviews accept no new
   decisions.
5. Keep the evidence: `review-evidence --format csv --output FILE` (or
   `POST .../evidence`). This is every line with its decision, reviewer, time,
   comment and revocation outcome. Pending lines are listed as `pending`.

A revoke runs through the existing path for that kind of line:

| Line | Effect | Outcome when it cannot finish here |
|---|---|---|
| `principal` | Full offboarding (below) | — |
| `credential` | Revokes the enrollment's refresh families and access tokens | For tokens issued by an external authorization server, also end the session there or revoke the binding. Otherwise new tokens keep working |
| `workspace_entry` | Removes the principal's direct entry on that workspace | — |
| `group_membership` | Removes the principal from a local group | — |
| `synced_group_membership` | Not changed automatically | `change_at_source`: remove the user from the source group (SCIM or GitHub). The reviewer may also remove the group's entry from the workspace |
| `workspace_access` | Does not revoke on its own; names the underlying entry or group membership | `revoke_entry_or_membership`: revoke the `workspace_entry` or `group_membership` lines that give this access |
| `repo_link` | Not changed automatically | `requires_workspace_admin`: a workspace admin unlinks the repository (`vaelius-client workspace unlink`) |
| `delegation` | Deactivates the delegation (the delegate's credentials stop authorizing) | — |
| `document_grant` | Removes the reader from the reviewed release | — |
| `settings_admin` | Clears the tenant administrator flag | — |
| `binding` | Deactivates the binding and revokes credentials issued through it | `pending_operator`: credentials are revoked at once; repeat the decision from the operator command to deactivate the binding |
| `connection_reader` | Not changed automatically | `requires_owner`: the owner narrows the reader list, because removing the last reader would widen the connection |

Evidence produced: the `access_reviews`, `access_review_lines` and
`access_review_decisions` rows. Audit rows (`access_review`, `access_review_report`,
`access_review_decision`, `access_review_revoke`, plus `credential_revocation`,
`membership`, `delegation`, `release_reader`, `settings_admin` and
`identity_binding` for each change). Exported CSV or JSON evidence. Private operator
receipts under the profile's `receipts/`.

**Suggested cadence:** review privileged lines (`privileged`, `raw_source_read`,
`settings_admin`) monthly, and every principal quarterly. Also run a review when an
identity provider, directory mapping or tenant administrator changes. Close each
review within two weeks of opening it.

## Idle-access hygiene

Hygiene expires access nobody has used. It is off by default. Configure it in
`runtime.json`:

```json
{"access_review": {"stale_days": 90, "hygiene": {
  "enabled": true, "credential_idle_days": 90,
  "deactivate_bindings": false, "binding_idle_days": 180,
  "include_never_used_bindings": false}}}
```

```sh
python -m agenthub.access_review --profile PROFILE --tenant TENANT hygiene          # dry run, always allowed
python -m agenthub.access_review --profile PROFILE --tenant TENANT hygiene --apply  # requires enabled: true
```

`--apply` revokes each enrollment with no observed use in `credential_idle_days`. It
checks again under the policy lock first, so access used since the scan is kept.
With `deactivate_bindings`, it also deactivates bindings idle for `binding_idle_days`.
Bindings that have never been used are included only with
`include_never_used_bindings`, because a binding has no creation time and a new hire
may not have logged in yet. Binding deactivation needs the operator's control-plane
credentials. Every change is audited (`access_hygiene`,
`access_credential_revocation`, `identity_binding`). Run it from a scheduler such as
a daily cron job, systemd timer or Kubernetes CronJob.

## Offboarding

```sh
python -m agenthub.access_review --profile PROFILE --tenant TENANT offboard \
  --principal PRINCIPAL --reason "Departure" --reviewer security-officer [--dry-run]
```

The first transaction deactivates the principal and clears the settings
administrator flag. It also revokes every refresh family and access token the
principal holds, including credentials that act for them. Access ends there, even
if a later step has to be retried. The command then:

- deactivates delegations in both directions
- removes the principal's direct workspace entries
- removes the principal from local groups (SCIM and `github` memberships drop at the
  next directory event; deactivation already ends access through them)
- removes the principal from reviewed-release reader lists
- deactivates their identity bindings when control-plane credentials are available

Every step is audited. The command is idempotent: a second run reports
`already_offboarded` and changes nothing.

`remaining` lists what stays, along with any action it still needs:

- owned sources: retained under retention and legal hold, never deleted here
- owned connections
- connector reader lists that name the principal
- directory users
- bindings still active
- `workspaces_without_admin`: workspaces where this principal was the last
  counting admin (a tenant settings administrator assigns a new one, using the
  settings-admin break-glass override)

**Deactivate the user in the identity provider as well.** A later SCIM update that
marks the user active reactivates the principal.

**SLA guidance:** start offboarding as soon as HR or the manager gives notice. For a
voluntary departure, finish by the end of the last working day. For an involuntary
departure, finish within one hour, before or during notification. Do not rely on
token expiry: access tokens last up to 24 hours, but refresh sessions can last up to
the configured absolute lifetime (90 days by default). SCIM deprovisioning
deactivates the principal when the identity provider pushes it. The offboarding
command remains the complete step, because it revokes sessions, delegations and
grants.

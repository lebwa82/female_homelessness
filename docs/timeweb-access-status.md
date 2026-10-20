# Timeweb access handoff status

Checked from the Mac on 2026-10-20 at 02:37 Europe/Moscow
(2026-10-19T23:37:23Z). No application services were restarted.

## Verified from the Mac

| Field | Verified value |
| --- | --- |
| Provider / account | Timeweb Cloud / af950022 |
| Project / zone | 3044129 / msk-1 |
| VM ID / name / state | 9351701 / nevidimy-prod-app / on |
| Current public IPv4 | 5.42.116.10 |
| SSH login / port | root / 22 |
| SSH hostname | 9351701-af950022.twc1.net |
| Deployment marker | timeweb-nevidimy-prod-app-msk-1 |
| SSH host key | ED25519 SHA256:7jsgHdG9OL1sw4jepEe8gBC+vyYhvdjaUYBaBIUw6B8 |

The official API returned the expected VM identity, project and zone. Read-only
SSH succeeded using the existing Mac key and strict host-key checking. The
server's ED25519 fingerprint matched the existing local pin; this is not a new
independent console attestation. The deployment marker was verified by the
maintained remote-access helper. Resolve the address again before future access.

Managed PostgreSQL `4221087` / `nevidimy-prod-pg` was independently verified in
project `3044129`, zone `msk-1`, with status `started`. Docker Compose reported
`2.40.3+ds1-0ubuntu1`; noninteractive sudo succeeded. The four units
`nevidimy.service`, `nevidimy-agent.service`, `nevidimy-telegram.service` and
`nevidimy-proxy-tunnel.service` were active before and after key installation.

Pinned public host-key record (not a credential):

```text
5.42.116.10 ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAID8UuPsDFj6IerlxWk3JSbrAQCgkluh/zyMUqNjpJ3Gc
```

## Dedicated cloud SSH key: no private-key transfer

The user selected a simpler workflow: generate the key inside the existing
cloud task and transmit only its public part. Manual base64/Personal vault
instructions for the SSH key are superseded; `TIMEWEB_SSH_KEY_B64` is not needed
for this task.

- The cloud agent generated an ED25519 key in its runtime, outside the checkout.
  It reported a private temporary directory (`0700`) and private file (`0600`),
  at `/tmp/codex-timeweb-ssh-apafwxmq/id_ed25519`.
- Active public-key fingerprint:
  `SHA256:qW9K68eAODqrQilV9qzPexa/88Eqs1rOtRb1hibynU0`.
- Only the public key was returned. Its ED25519 wire format and SHA256
  fingerprint were independently validated on the Mac.
- Its public part is installed exactly once in `/root/.ssh/authorized_keys`
  with `restrict`; permissions remain `0600`/`0700`. A separate read-only
  post-check confirmed the entry and that all four services remain active.
- `restrict` disables PTY and SSH forwarding; it does **not** restrict root's
  remote command permissions. The cloud key has **not yet authenticated to
  the VM over SSH** because its runtime lacks the TCP grant.
- The earlier Mac-generated trial key, fingerprint
  `SHA256:qq+fj4pJrfaCHUc5ECwgGCWinoO4K/QlMVs8QgO93a4`, was revoked by removing
  only its exact added record. Its files remain under the ignored Mac directory
  `/Users/lebwa82/female_homelessness/.runtime/codex-cloud-access/`, so its public
  record can be restored if needed. Other existing records and the personal
  Mac key were preserved. Existing Mac SSH still works.
- The cloud private key remains in that task's temporary runtime. Durability
  across runtime recreation or availability in other tasks is not guaranteed;
  do not publish it into a reusable environment, image or source snapshot.

## OpenAI cloud environment

- User-selected environment name: `female_homelessness`, associated with
  `lebwa82/female_homelessness` and shown as owner-only in the supplied UI.
- Related cloud task: `01a155ef-9ab2-7677-b3b1-fdce39b76041`, host `durable`.
- Cloud checkout: `/workspace/female_homelessness`.
- Runtime environment ID reported by the cloud task:
  `ccarenv_b64_Y2NhcmVudl81NDMxNDczYTJjMzQ4MTkxYmYwZDBjMTNmZWQ0NmE3YQ`.
- At the cloud key-generation check, the agent reported revision `30`,
  without an observed ready `TWC_TOKEN` binding. `api.timeweb.cloud`
  was absent from the effective HTTP policy, TCP destinations were empty and
  VPN was disabled. The user's added `TWC_TOKEN` is not yet ready in that
  runtime; this does not establish a failed token or a failed save in the UI.
- The complete cloud handoff instruction has been received as nonsecret text.
- With separate explicit user approval, a configuration-draft request was
  delivered to the existing `Редактирование female_homelessness` dialog
  `01a1567c-beea-7017-bdb3-2d68007f407c`. The configuration agent verified the
  exact source configuration ID:
  `ee77502a-bda1-4796-ae6b-2ceb5e1cf0bc~cecfg_6acfafca060c81919d8965d9c0faf619`.
- The configuration workflow exposes `network.allowed_domains` only, with no
  TCP destination/protocol/port fields. It cannot prepare the requested exact
  SSH grant. Its draft revision was `1`, ID suffix
  `70922f05679c819196fd9294172b1ade`; `secrets: []` did not contain the user's
  expected `TWC_TOKEN` binding. This is the inspected draft, not evidence that
  every other draft or Personal vault entry is empty.
- The configuration agent made **no draft changes and no publication**. There
  is no working SSH draft awaiting confirmation. This is a confirmed limit of
  the available workflow, not proof that all OpenAI execution modes lack TCP.
- API and SSH access **from the cloud environment have not been tested**;
  the cloud agent correctly stopped before attempting blocked destinations.

## Remaining work

1. The current configuration workflow cannot finish SSH setup. Obtain a
   supported TCP-capable execution/configuration path, or let the user choose
   local execution for Timeweb operations. Do not create a new VM, VPN, daemon
   or automation, or move execution to another host without that choice.
2. If an applicable supported configuration path becomes available, inspect
   effective network policy and credential readiness after approved changes.
   An HTTP allowlist is not a TCP grant. Stop on a policy denial; do not use
   tunnels or different ports to evade it. Separately reconcile the user's
   expected API-token binding without reading or printing its value.
3. If the existing cloud runtime receives the grant, use its own private key
   with strict pinned host-key checking, no agent forwarding and explicit
   identity selection. Verify the VM through the fresh official API, then
   perform only read-only SSH identity/marker/sudo checks.
4. If a new runtime is required, account for the temporary private key's
   lifetime: generate a new dedicated identity there and arrange replacement
   of only this task's public record through an authorized path. Do not claim
   that a path in another runtime provides access to the key.
5. Update this report in the cloud checkout with actual verification results.
   Saved settings and Mac checks do not establish cloud connectivity.

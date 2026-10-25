# Facility discovery and provider adapters

Infra Sentinel uses `infra.discovery.registration@20260812.1` only to discover local services and
select an exact `(protocol, version, binding)` offer. Discovery does not define a common facility
snapshot. The canonical contract is maintained by the separate Infra Protocol project; Sentinel
vendors an exact schema copy as an auditable conformance fixture while its packaged runtime uses a
dependency-free strict parser for the same fields and bounds.

The packaged app resolves the Infra Protocol runtime root, validates owner-only registration files,
selects a supported offer, validates the owner-only endpoint, and then hands the connection to the
selected provider adapter. A manifest is only a candidate entry declaration: the connection and
application response determine current availability. Unknown protocols remain valid declarations
but are ignored by Sentinel.

Current adapters are deliberately independent:

| Provider | Application protocol | Local binding |
| --- | --- | --- |
| PCP Runtime | `pcp.runtime.observer@20260810.1` | Unix Socket / Windows Named Pipe |
| Infer Runtime | `infer-runtime.status@20260810.1` | Unix Socket / Windows Named Pipe |
| Dev Mesh Observer | `dev-mesh.observer.status@20260812.1` | Unix Socket / Windows Named Pipe |

Unix hosts select `infra.local.unix-socket`; Windows selects
`infra.local.windows-named-pipe`. Windows requires the exact local namespace
`\\.\pipe\infra-protocol\<opaque>` and a byte-stream pipe. Filesystem Discovery
roots, `registrations`, and manifests require the current user as owner and a
private DACL, allowing SYSTEM and Administrators. The reader rejects remote
filesystem paths, alternate streams and reparse points, and inspects and reads
the same manifest handle while holding its ancestors against rename.

The pipe client verifies the connected server process's user SID before sending
application bytes and permits identification-only server impersonation. Providers
still own the binding's required client-SID admission. Connection, writes, reads
and required EOF share one deadline; cancelled I/O retains its buffers and native
handles until completion. Application framing stays in the provider adapters.

Each provider owns its request, response, framing, errors, limits, privacy contract, and optional
Console link. Sentinel normalizes only bounded status, headline metrics, issues, observation time,
and a loopback Console URL into its private UI projection. Provider extensions never cross that
boundary.

The Dev Mesh adapter accepts only its exact version and native local binding and retains only bounded aggregate
metrics and issues plus a literal-loopback Console URL. Its privacy declaration must exclude
coordination owner IDs, workspace paths, Git revisions, branch names, event payloads, raw errors,
database paths, and claim scopes. Sentinel never reads the Observer database, collaboration graph,
or workspace event stream directly.

For `dev-mesh.observer.status@20260812.1`, Sentinel sends the exact snapshot request in a single LF
frame, accepts at most 256 KiB within two seconds, and requires EOF after the single response frame.
Only the contract's three headline aggregates, known aggregate metric and issue IDs, and the fixed
issue subject `observer` enter the private projection. Unknown provider fields and extension members
are ignored. Historical integrity totals remain metrics; Sentinel trusts the producer's current-cycle
status instead of deriving a permanent failure from cumulative history. Ordinary Agents, claims,
handoffs, conflicts, transactions, and branches are not facility failures.

There is no additional discovery handshake. The complete sequence is:

1. validate a registration candidate;
2. intersect exact protocol versions and supported bindings;
3. connect to the selected endpoint;
4. let the binding enforce its peer-user rule;
5. exchange one provider-owned application request and response; and
6. reject the result if the registration generation or selected offer changed during the request.

The registration has no lease, expiry, heartbeat, or liveness timestamp. Sentinel rescans the
stable manifest during each reconciliation pass, applies product polling/backoff only to an exact
identity/generation/offer, and retries immediately when the generation or selected offer changes.
If a registration disappears, its facility projection disappears too; Sentinel does not retain a
lease-derived stale copy. After each successful observation it rereads the registration and confirms
that the generation and selected offer are still identical.

`INFRA_PROTOCOL_RUNTIME_DIR` may set the final shared runtime root in managed deployments. Without
an override, Sentinel follows the platform paths in the canonical Infra Discovery specification.
The old `INFRA_SENTINEL_REGISTRATION_DIR` and `infra-observer.*` wire are intentionally unsupported.

Windows qualification uses real kernel byte pipes implementing the three provider
contracts, including cross-process packaged-Agent checks. These fixtures prove the
consumer path, not interoperability with an installed product. A provider must
publish a compatible Windows offer; a Unix-only service or a service running solely
inside WSL is not automatically reachable through this binding. On the qualification
machine no real provider registrations were present, so real-product acceptance
remains pending. See [Windows validation](windows.md).

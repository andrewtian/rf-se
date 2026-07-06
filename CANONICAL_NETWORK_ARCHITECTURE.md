# se299 Canonical Network Architecture -- Devices, Instances, Clients

How the instruments live on the network, how any client operates them, and how to LIST the devices
and clients present. This is the consistent model behind every networked verb (coordinator, se-gui,
walkaround, checkpath, calibrate, wall, validate, two-instance sweep, devices).

## The three layers

```
  DEVICE INSTANCES (one per adapter, on the network)          CLIENTS (any host, any number)
  ------------------------------------------------            ------------------------------------
   instance "rx"                 instance "tx"                 coordinator client   (CONTROLLER)
   qemu VM / Pi                  qemu VM / Pi                    leases rx+tx, drives a campaign/
   ni_gpib_server :5555          ni_gpib_server :5556            sweep, publishes telemetry
     |  GPIB pad 18                |  GPIB pad 5                observer client(s)   (OBSERVER)
     v                            v                              subscribe to telemetry; no bus
   8565EC analyzer              68367C source                  operator GUIs (se-gui/walkaround)
      (a DEVICE)                  (a DEVICE)                    query/list clients (devices verb)
        ^                            ^                                 |
        |  net:HOST:5555:18          |  net:HOST:5556:5                | discover + operate
        +----------------------------+---------------------------------+
                         the network (TCP bridge + UDP discovery)
```

- A DEVICE INSTANCE is a bridge process (`gpib_bridge/ni_gpib_server.py`) that owns one NI adapter
  and exposes the instrument(s) behind it as a TCP endpoint `net:HOST:PORT:PAD`. One adapter ->
  one instance -> one (or more) DEVICE(s) at GPIB pads. The golden two-VM is two instances: rx
  (8565EC, pad 18, :5555) and tx (68367C, pad 5, :5556), each in its own qemu VM.
- A DEVICE is one instrument reachable at `net:HOST:PORT:PAD`. It is classified rx (analyzer) or tx
  (source) by the model registry, and carries capabilities (sweep/zero-span/marker/floor for rx;
  cw/rf-onoff/list-sweep for tx).
- A CLIENT is any process anywhere on the network that reaches the devices. Clients are symmetric:
  each connects to the same `net:` endpoints. Roles are by BEHAVIOUR, not identity.

## How any client operates any device (single-writer via leases)

Every device instance runs a VXI-11-style LEASE TABLE (`gpib_bridge/protocol.py` verbs
L/K/U/R). This is what lets ANY client operate the devices safely:

- `L <scope> <ttl>` -- acquire an EXCLUSIVE lease on a scope (`BUS` = the whole bus, or a `<pad>` =
  one device) for ttl seconds. The holder becomes the CONTROLLER.
- `K <ttl>` keepalive (renew); `U` release; the lease also lapses on ttl expiry or session
  disconnect (so a crashed client never wedges the bus).
- While a client holds a conflicting lease, every OTHER client's write/query on that scope is
  refused -- so there is exactly ONE controller at a time, but any number of OBSERVERS.
- `R` -- report the live lease table (who holds what). This is OBSERVER-readable (never arbitrated),
  so any client can always ask a device "who is operating you?" even while it is leased.

A client becomes the controller by taking the lease (Coordinator.take_control leases rx+tx),
does its campaign/sweep/walkaround, then releases (Coordinator.release_control) -- at which point
any other client may take over. No central broker: the devices themselves arbitrate.

While control is held, take_control's KEEPALIVE thread renews both leases (K) every ttl/3, so a
run longer than the TTL never silently loses exclusivity; release_control stops (and joins) the
keepalive before releasing, so a finishing renew can never steal the lease back from the next
controller. Each transport serializes its transactions, so the keepalive shares the controller's
sessions safely.

## Discovery (finding devices without hand-configured addresses)

`discovery.py`: each bridge MAY run a UDP `Beacon` that answers a broadcast `se299-discover?` PROBE
with a `BeaconInfo` (host, port, the instruments behind it). `discover()` broadcasts one probe and
collects the beacons -> a device roster with no hand-typed addresses. Where a bridge runs no beacon
(the current golden two-VM), clients pass the `net:` endpoints explicitly. `control_plane.from_beacons`
/ `from_addresses` build the same `ControlPlane` roster either way.

## Client identity + the session table (who is connected, grouped by client)

The lease table (R) names only lease HOLDERS. To list EVERY connected client -- controllers AND
observers, on every device -- each bridge also runs a SESSION TABLE, exposed by two additional verbs
(`gpib_bridge/protocol.py`, additive: any client/bridge that predates them is unaffected):

- `X <client-id>` -- a client announces its identity when it connects. The id is a compact,
  space-free, pipe-delimited string `role|host=<hostname>|pid=<pid>|u=<uuid8>` (`identity.py`). The
  `u=<uuid8>` is chosen ONCE per client process and sent to BOTH bridges, so it is the GROUPING KEY:
  a client that opens rx+tx has two bridge sessions with the SAME `u=`, which reunites them into one
  client. Best-effort: an old bridge replies `! unknown verb` and the client swallows it (X becomes a
  no-op), so identity is never required.
- `S` -- report the live SESSION table: one line per connected session, joined with the lease table,
  `session <sid> client <id> peer <ip:port> role <role> pad <pad> lease <scope|->`. Observer-readable,
  like R. A session with a lease is a CONTROLLER of that scope; one bound but unleased is an OBSERVER.

The device is still the source of truth: it tracks its own connected sessions (register on connect,
identity on X, pad on A/bind, drop on disconnect), so the list is honest and self-cleaning -- a
crashed client's session vanishes when its socket closes.

## Listing devices and clients

`cli.py devices` is the live view of both layers. It probes each device for reachability + IDN, reads
its lease table (R) AND its session table (S), announces its OWN identity so the querying process is
marked LOCAL, then GROUPS every session across every device by client (`u=`), showing per client which
devices it CONTROLS vs OBSERVES:

```
uv run python cli.py devices --analyzer net:127.0.0.1:5555:18 --source net:127.0.0.1:5556:5
# or, with beacon-enabled bridges:   uv run python cli.py devices --discover

NETWORK DEVICES (2 instrument instance(s) on the bus):
  KIND MODEL    ADDRESS                    STATUS     CONTROL              IDN
  rx   8565EC   net:127.0.0.1:5555:18      REACHABLE  LEASED by session 3  (leased -- IDN skipped)
  tx   68367C   net:127.0.0.1:5556:5       REACHABLE  LEASED by session 2  (leased -- IDN skipped)

CLIENTS (all sessions on the devices, grouped by client):
  [coordinator] host=host.local pid=98609 u=441ae569
      CONTROLS: rx:8565EC, tx:68367C
  [devices] host=host.local pid=98712 u=c43db98b   (LOCAL -- this devices query)
      OBSERVES: rx:8565EC, tx:68367C

  NOTE: a telemetry-only observer (dashboard) opens no bridge socket, so it is not
  listed here; such observers are tracked via the coordinator's telemetry roster.

  2 device(s) | 2 client(s) connected (1 controlling), incl. this LOCAL query
```

The coordinator opened TWO bridge sessions (session 3 on rx, session 2 on tx) but appears ONCE --
grouped by its `u=`, controlling both instruments. The querying process is its own client, marked
`(LOCAL)`, observing both. When a client releases + disconnects, both its sessions drop and its
devices read FREE (open) and answer IDN again. If a bridge predates the session verbs, `devices`
falls back to the lease-table-only view (controllers by session id + the querying observer).

## Where each verb sits

- coordinator / se-gui / walkaround / calibrate / wall / checkpath / validate / two-instance sweep:
  CONTROLLER clients -- they take the lease and drive the devices.
- dashboard: an OBSERVER client -- subscribes to a coordinator's telemetry, no bus contact.
- devices: an OBSERVER query -- reads the lease + session tables (R + S) to LIST devices + all
  clients grouped by identity, and announces its own X so it is marked LOCAL.

All of them reach the SAME networked device instances by `net:HOST:PORT:PAD` (or discovery), so the
setup scales from the loopback golden two-VM to instruments on separate hosts across the LAN with no
code change -- only the addresses differ.

## Trust model and network exposure (DECIDED)

The bridge is an instrument-CONTROL service (it can key RF and drive the analyzer), so its network
exposure is a deliberate trust decision, not a default.

DECISION: the se299 bench assumes a TRUSTED, ISOLATED lab LAN (or a single machine). It is NOT
hardened for a shared / office / Wi-Fi / untrusted network. The loopback default and the
deliberate-exposure gate are enforced in code; per-connection CLIENT authentication is enforced ONLY
on the direct/Pi bridge, NOT on the qemu-VM path (see "Two enforcement realities" -- this is a
narrowed, honest claim, corrected after an adversarial audit found the earlier wording overstated it).

- DEFAULT = single-machine loopback. `ni_gpib_server` and the qemu hostfwd bind `127.0.0.1`; the
  bridge ports are unreachable off-host. No credentials needed because nothing is exposed.
- A ROUTABLE bind is GATED (a deliberate-exposure gate, not client auth). Binding the ports on a
  non-loopback interface is refused UP FRONT unless the operator sets a token or passes `--insecure`:
  `ni_gpib_server.main` refuses a non-loopback `--host` (incl. `--host ""`, which is INADDR_ANY /
  all-interfaces -- gated after fixing a bug where `""` read as loopback) and `vm.guard_bind_auth`
  (gpib_bridge/vm.py) refuses a routable qemu hostfwd without `--vm-token` / `NI_GPIB_TOKEN`. This
  makes exposure a conscious act; it does NOT by itself authenticate the clients that then connect.
- `--insecure` (unauthenticated non-loopback) is explicit-only, never default. This decision KEEPS
  `--insecure` as a supported non-loopback mode (it supersedes an earlier plan note to forbid it),
  because the qemu-VM path is unauthenticated-by-construction anyway -- both rely on the trusted-LAN
  assumption, so forbidding `--insecure` on the direct bridge would not change the shipped VM reality.

TWO ENFORCEMENT REALITIES (do not conflate them):
- DIRECT / Pi bridge (`ni_gpib_server` run directly): per-connection auth IS enforced -- a client
  must present `H <token>` and `serve_connection` checks it with `hmac.compare_digest`, fail-closed.
  Here the token genuinely gates client ACCESS.
- qemu-VM bridge (the golden two-VM, what this repo actually ships): the GUEST `ni_gpib_server` is
  provisioned with `--insecure` and NO token (`render_cloud_init` / `provision.sh` do not thread the
  token into the guest). So `--vm-token` gates the OPERATOR'S decision to expose the host hostfwd, but
  a LAN client that reaches the exposed port needs NO token -- the VM LAN path is
  UNAUTHENTICATED-BY-CONSTRUCTION. The "trusted isolated LAN" assumption is therefore LOAD-BEARING for
  the VM path (the only thing protecting it), not defense-in-depth.

EXPOSURE INVENTORY (every listening socket, so nothing is silently open):
- bridge port(s) `:5555/:5556` -- loopback default; a routable bind is gated as above.
- ssh `:2222` -- ALWAYS loopback (diagnostics only, never LAN-exposed).
- telemetry port -- loopback default; `--telemetry-bind 0.0.0.0` exposes it LAN-wide with NO token
  and no guard. It is READ-ONLY SE telemetry (cannot key RF), lower severity, but it IS an exposure.
- discovery UDP beacon -- binds all-interfaces, unauthenticated; NOT run in the golden two-VM;
  hardening it ("signed beacons") is deferred to Wave 3.

THREAT BOUNDARY (out of scope under this decision): eavesdropping / MITM / integrity on the LAN, and --
for the qemu-VM path -- CLIENT authentication entirely. Acceptable ONLY because the LAN is assumed
trusted + isolated.

UPGRADE PATH (only if a shared/untrusted network is ever required -- task #12 Wave 3, YAGNI today):
thread the token into the guest bridge (`render_cloud_init` / `provision.sh`) so the VM path enforces
it end-to-end, then TLS + mutual auth, per-capability tokens (observer cannot `rf_on`), signed
discovery beacons, do-not-send-token-to-an-unverified-host, and an admin force-preempt verb. Until that
requirement exists this section IS the recorded, audit-corrected trust-model decision.

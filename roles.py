"""se299 role instances -- multiple GUI/headless instances, each serving one role over the
shared network.

  CoordinatorRole : owns the bus (a Coordinator over a resolved rx+tx pair), runs the
                    substitution campaign, and PUBLISHES the live SE figure / roster / summary
                    over a TelemetryHub. Exactly one of these controls the hardware.
  DashboardRole   : an OBSERVER instance -- subscribes to a coordinator's telemetry and keeps
                    the latest SE figure, roster, summary, and SE history. A pure model a GUI
                    renders; it never touches the bus, so the live SE figure (R8) reaches every
                    instance without contending for the instruments.

The role models carry no matplotlib, mirroring live.py's model/view split, so they are fully
unit-testable; a thin view renders each.
"""
from __future__ import annotations

import telemetry


class CoordinatorRole:
    """The controlling instance. Build over a ControlPlane (which resolves the rx+tx pair);
    publishes telemetry every observer subscribes to."""

    def __init__(self, control_plane, hub=None, telemetry_port=0,
                 telemetry_host="127.0.0.1", lease_ttl_s=60.0):   # = control_lease.DEFAULT_LEASE_TTL_S
        self.cp = control_plane
        # telemetry_host selects the bind interface: '127.0.0.1' (default) keeps the listener
        # loopback-only; '0.0.0.0' or a LAN IP lets dashboard instances on OTHER hosts subscribe
        # (R8/R9). Ignored when an explicit hub is supplied (the hub already owns its bind).
        self.hub = hub or telemetry.TelemetryHub(host=telemetry_host, port=telemetry_port)
        self.coord = control_plane.make_coordinator(lease_ttl_s=lease_ttl_s)

    @property
    def telemetry_port(self) -> int:
        return self.hub.port

    @property
    def telemetry_host(self) -> str:
        return self.hub.host

    def start(self):
        self.hub.start()
        return self

    def run_campaign(self, on_se_update=None, on_shield_prompt=None):
        """Run the substitution campaign, publishing the roster up front and the live SE
        figure per wall point, then the final summary. `on_shield_prompt` is forwarded
        unchanged (see Coordinator.run_campaign) -- default None keeps back-compat behavior
        (no shield step). Returns the coordinator's result."""
        self.hub.publish("roster", self.cp.roster())

        def pub(fig, row):
            self.hub.publish("se", fig)
            if on_se_update is not None:
                on_se_update(fig, row)

        result = self.coord.run_campaign(bench=self.cp.bench, on_se_update=pub,
                                         on_shield_prompt=on_shield_prompt)
        self.hub.publish("summary", result["summary"])
        self.hub.publish("se", result["se_figure"])          # final worst-case
        return result

    def stop(self):
        self.hub.stop()


class DashboardRole:
    """An observer instance's model: the latest roster, SE figure, summary, and SE history,
    updated from a coordinator's telemetry. Renders live without any bus contact."""

    def __init__(self):
        self.roster = []
        self.latest_se = None
        self.se_history = []
        self.summary = None
        self._sub = None

    def on_message(self, topic, data):
        if topic == "roster":
            self.roster = data or []
        elif topic == "se":
            self.latest_se = data
            if isinstance(data, dict):
                self.se_history.append(data.get("se_db"))
        elif topic == "summary":
            self.summary = data

    def connect(self, host, port):
        self._sub = telemetry.TelemetrySubscriber(host, port)
        self._sub.start(self.on_message)
        return self

    def se_text(self) -> str:
        """A one-line human readout of the current SE figure (what a text dashboard prints)."""
        fig = self.latest_se
        if not fig or fig.get("se_db") is None:
            return "SE: (waiting)"
        rel = ">=" if fig.get("lower_bound") else "="
        band = fig.get("band", "")
        return f"SE {rel} {fig['se_db']:.1f} dB  (worst @ {band}, {fig.get('points', 0)} pts)"

    def close(self):
        if self._sub is not None:
            self._sub.close()

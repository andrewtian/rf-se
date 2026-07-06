"""Live SE-figure sweep: step frequency from as low as possible (68367C 10 MHz floor -- nearest to
DC) UPWARD, and at each point produce the SE figure = (known emitted TX dBm) - (received RX dBm).

On the current DIRECT 2.4mm cable this figure is the cable+connector insertion loss; with a shield
between matched TX/RX antennas it is the shielding effectiveness (IEEE-299 substitution method).

Robust to the bench 8565EC's intermittent LO wedge: aborts up front if the analyzer sweep is frozen,
and flags any per-point wedge (floor == tone == no coupling) instead of printing a bogus figure.

SAFETY: source capped to 0 dBm and analyzer input attenuation floored to 20 dB (arm_direct_chain,
proven under the 8565EC damage limits) before any tone; RF is left OFF on exit.

  QT_QPA_PLATFORM=offscreen uv run python rf-se/se299/tools/live_se_figure_sweep.py
"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import drivers

RX_ADDR = ("127.0.0.1", int(os.environ.get("SE299_ANALYZER_PORT", "5555")), int(os.environ.get("SE299_ANALYZER_PAD", "18")))
TX_ADDR = ("127.0.0.1", int(os.environ.get("SE299_SOURCE_PORT", "5556")), int(os.environ.get("SE299_SOURCE_PAD", "5")))
# as low as possible (source 10 MHz floor) stepping up through band 0 (<2.9 GHz, unpreselected)
FREQS_HZ = [10e6, 20e6, 50e6, 100e6, 200e6, 500e6, 1e9, 1.5e9, 2e9, 2.5e9]
P_TX_DBM = 0.0
_RF_SETTLE_S = 0.6
_COUPLE_MIN_DB = 20.0


def _sweep_alive(t):
    """True if trace A is re-acquiring (points change sweep-to-sweep). A frozen (wedged) 8565EC
    returns an identical trace -> False, so we abort before printing a bogus SE figure."""
    def snap():
        t.write("TDF P"); t.write("TS"); t.query("DONE?")
        return [float(x) for x in t.query("TRA?").replace("\r", "").replace("\n", "").split(",") if x.strip()]
    a = snap(); time.sleep(0.4); b = snap()
    return sum(1 for x, y in zip(a, b) if abs(x - y) > 0.05) > 3


def main() -> int:
    rx = drivers.NetworkTransport(*RX_ADDR, timeout_ms=20000)
    tx = drivers.NetworkTransport(*TX_ADDR, timeout_ms=20000)
    # SINGLE CONSUMER (Task 3): refuse to run if another consumer already drives either unit --
    # concurrent probing is the biggest operational contributor to the analyzer reference wedge.
    try:
        drivers.lease_exclusive(rx, "8565EC analyzer (RX)", ttl_s=400)
        drivers.lease_exclusive(tx, "68367C source (TX)", ttl_s=400)
    except drivers.SingleConsumerConflict as e:
        print(f"ABORT (single-consumer): {e}")
        return 3
    ana = drivers.Agilent856xEC(rx); src = drivers.Anritsu68369(tx)
    drivers.arm_direct_chain(src, ana, source_cap_dbm=0.0, rx_min_atten_db=20.0, cable_loss_db=0.0)
    src.prepare()
    ana.configure(rbw_hz=100e3, vbw_hz=100e3, ref_dbm=10.0, detector="POS"); ana.set_attenuation(db=20)
    try:
        if not _sweep_alive(rx):
            print("ABORT: 8565EC sweep is FROZEN (LO wedge) -- power-cycle the analyzer, warm up "
                  ">=5 min, then re-run. (No SE figure produced; refusing to print stuck values.)")
            return 2
        print(f"{'freq':>10} | {'TX dBm':>7} {'OSB':>4} | {'RX floor':>9} {'RX tone':>8} | {'SE figure':>9}")
        print("-" * 62)
        wedged = 0
        for f in FREQS_HZ:
            src.set_freq(f); src.set_power(P_TX_DBM)
            src.rf_off(); time.sleep(_RF_SETTLE_S); _, floor = ana.measure_floor(f, 0.05)
            src.rf_on(); time.sleep(_RF_SETTLE_S)
            ol1 = float(tx.query("OL1")); osb = tx.query_raw("OSB")[0]
            _, tone = ana.measure_peak(f, 0.05)
            fs = f"{f / 1e6:.0f} MHz" if f < 1e9 else f"{f / 1e9:.2f} GHz"
            if tone - floor < _COUPLE_MIN_DB:            # no coupling => wedged/stale, not a real figure
                wedged += 1
                print(f"{fs:>10} | {ol1:6.2f} 0x{osb:02X} | {floor:8.2f} {tone:7.2f} | WEDGED (floor==tone)")
            else:
                se_fig = ol1 - tone                       # known emitted TX minus received RX (dB)
                print(f"{fs:>10} | {ol1:6.2f} 0x{osb:02X} | {floor:8.2f} {tone:7.2f} | {se_fig:8.2f} dB")
        src.rf_off()
        print("\nSE figure = (known emitted TX dBm) - (received RX dBm) = TX-out -> RX-in path loss.")
        if wedged:
            print(f"NOTE: {wedged}/{len(FREQS_HZ)} points wedged mid-sweep (8565EC LO) -- power-cycle "
                  "+ warm up for a clean full sweep.")
        return 1 if wedged else 0
    finally:
        try:
            src.rf_off()
        except Exception:
            pass
        rx.close(); tx.close()
        print("RF OFF, closed")


if __name__ == "__main__":
    sys.exit(main())

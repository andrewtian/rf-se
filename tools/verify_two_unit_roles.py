"""Verify the two-unit local bring-up + correct RX/TX role binding (task #46).

Confirms both device instances are reachable AND each is bound to the RIGHT role -- no cross-wiring:
  RX (analyzer) = 8565EC on :5555 pad 18   |   TX (source) = 68367C/683xx on :5556 pad 5
IDN identifies each, so a swapped bring-up (source adapter answering on the analyzer port, or vice
versa) is caught. Honest-skips when the bridge is unreachable or a unit is leased by another consumer;
gentle (IDN + one benign query each, no RF keyed); leases released on exit.

  uv run --group se299-gui python tools/verify_two_unit_roles.py
Exit 0 = both up + correct roles; 2 = a role/identity problem; 3 = bench busy/unreachable (skip).
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import drivers

RX = ("127.0.0.1", int(os.environ.get("SE299_ANALYZER_PORT", "5555")), 18)
TX = ("127.0.0.1", int(os.environ.get("SE299_SOURCE_PORT", "5556")), 5)


def main():
    rx = tx = None
    leased = []
    try:
        try:
            rx = drivers.NetworkTransport(*RX, timeout_ms=15000)
            tx = drivers.NetworkTransport(*TX, timeout_ms=15000)
        except Exception as e:                                  # bridge down / adapter wedged
            print(f"SKIP -- cannot reach the bridge: {type(e).__name__}: {e}")
            return 3
        try:
            drivers.lease_exclusive(rx, "RX 8565EC", ttl_s=60); leased.append(rx)
            drivers.lease_exclusive(tx, "TX 683xx", ttl_s=60); leased.append(tx)
        except drivers.SingleConsumerConflict as e:
            print(f"SKIP -- a unit is leased by another consumer:\n{e.report}")
            return 3

        ana = drivers.Agilent856xEC(rx)
        src = drivers.Anritsu68369(tx)
        a_idn = ana.idn().strip()
        s_idn = src.idn().strip()
        print(f"[:5555 pad 18 -> RX/analyzer] IDN = {a_idn}")
        print(f"[:5556 pad  5 -> TX/source  ] IDN = {s_idn}")

        au, su = a_idn.upper(), s_idn.upper()
        rx_is_analyzer = ("8565" in au) or ("HP856" in au) or ("8564" in au)
        tx_is_source = ("683" in su) or ("ANRITSU" in su)
        swapped = ("683" in au) or ("ANRITSU" in au) or ("8565" in su) or ("8564" in su)

        # one benign, non-RF command each proves the bus round-trips
        cf = ana.t.query("CF?").strip()
        of1 = src.output_freq_mhz()
        print(f"benign: RX CF? = {cf} Hz   |   TX OF1 = {of1} MHz")

        ok = rx_is_analyzer and tx_is_source and not swapped
        print(f"\nrx_is_8565EC_analyzer={rx_is_analyzer}  tx_is_683xx_source={tx_is_source}  "
              f"roles_swapped={swapped}")
        print("VERDICT: " + ("PASS -- both units up, each bound to the correct role (not swapped)"
                             if ok else "FAIL -- role/identity problem (see above)"))
        return 0 if ok else 2
    finally:
        for t in leased:
            try: t.release_lease()
            except Exception: pass
        for t in (rx, tx):
            try:
                if t is not None: t.close()
            except Exception: pass


if __name__ == "__main__":
    sys.exit(main())

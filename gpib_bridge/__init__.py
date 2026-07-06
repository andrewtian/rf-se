"""se299 network GPIB bridge: a small TCP service (ni_gpib_server.py) that runs on
the Linux host physically holding the NI GPIB-USB-HS (a UTM/QEMU VM on the Mac, or a
Raspberry Pi) and exposes the 8565EC over the network, plus the shared wire protocol.
The macOS client is drivers.NetworkTransport. See README.md for the VM bring-up."""

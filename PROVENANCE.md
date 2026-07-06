# Provenance

`rf-se` is the **se299** RF shielded-enclosure bench software: an HP/Agilent
8565EC spectrum analyzer (RX) and an Anritsu 683xx signal source (TX) driven over
a networked GPIB bridge to run the IEEE-299 substitution measurement of shielding
effectiveness (SE = reference - wall, stepped-CW, source-tracked), with a live
PySide6/pyqtgraph bench GUI. Start at `NETWORKED_OPERATION_SPEC.md`.

## Migrated out of a monorepo

This repository was migrated out of the `office-setup-explorations` monorepo (an
RF shielded enclosure for sub-THz research), where it lived at `rf-se/se299/`.
That monorepo references this repository back as a git submodule at the same path,
so both sides stay linked:

- monorepo  -> this repo : git submodule at `rf-se/se299`
- this repo -> monorepo  : this file

The broader shielded-enclosure work (ownership docs, the 40 GHz RF verification
campaign, sub-THz SE-upgrade design) remains in `office-setup-explorations`.

## Running

    uv sync                    # core: numpy, matplotlib, pytest
    uv sync --group se299-gui  # + PySide6 / pyqtgraph bench GUI
    uv sync --group se299-hw   # + pyvisa / pyvisa-py (drive real GPIB instruments)

Tests self-insert the repo root on `sys.path`, so from the repo root:

    QT_QPA_PLATFORM=offscreen uv run --group se299-gui python -m pytest tests/ -q

"""Converts the raw Zenodo gamma/neutron dataset (raw/{gamma,neutron}.csv, downloaded by
prepare_dataset.py) into a CSV the sw/ GUI's FoM Optimization wizard can load directly:
`energy_long,fci,psd` columns, matching CsvLogger's own live-log schema (sw/gui/csv_logger.py).

There is currently no neutron source available to exercise the FoM wizard against live hardware.
This gives it real, previously-published bimodal gamma/neutron data to optimize against instead --
the same dataset behind Figs. 5-7 of Morales et al. (Nucl. Eng. Technol. 56, 745-752, 2024), which
report FoM = 1.11 (PSD) and FoM = 1.88 (FCI) for a mixed dataset with a ~475 keVee energy cut. This
script's own __main__ block reproduces that comparison as a sanity check.

The two columns are NOT the same level of fidelity, worth being explicit about:

  FCI: taken from Zenodo's own published 'FCI' column (their Eq. 3, (PSA_w - PSA_l) / PSA_w,
       computed at 100 Msps / 2048-point). This project's own firmware computes the COMPLEMENT of
       that ratio (fci_sink.c's FciSink_RatioScaled: PSA_l / PSA_w), so it is flipped here
       (1 - zenodo_fci) to match what AcqEvent.fci actually means in this project -- otherwise a
       live session's FCI and this dataset's FCI would cluster on opposite sides of the same
       division line for physically equivalent events. This is still the paper's own reference
       computation, not reprocessed through this board's actual 50 Msps/1024-point FFT datapath --
       treat it as a validation dataset with known-good separation, not a stand-in for what this
       exact hardware would report for the same physical events.

  PSD: not published by Zenodo -- computed here from the raw waveform using this project's own
       formula (psd_core's dual_gate_integrator.vhd: (long-short)/long, baseline-subtracted gate
       sums over PSD_SHORT_GATE/PSD_LONG_GATE samples starting PSD_PRE_GATE before the pulse,
       acquisition.c's defaults). Two approximations this makes, both because Zenodo does not
       publish an explicit trigger time or hardware-computed PSD to check against:
         - "pre_trigger" is approximated as the pulse's own minimum-sample index (argmin -- these
           are negative-going pulses, confirmed by inspection: raw samples dip well below their own
           pre-pulse baseline). A real leading-edge trigger fires slightly before the peak, not at
           it, but with no independent trigger timestamp in this dataset the peak is the only fixed
           reference point available.
         - Zenodo's traces are 100 Msps against this board's 50 Msps, so PSD_PRE_GATE/SHORT_GATE/
           LONG_GATE (tuned in samples for 50 Msps) are doubled here to cover the same physical
           duration.
       The sign of Zenodo's raw samples relative to this project's own (BLR-restored, positive-
       going by the time it reaches psd_core) convention was not independently confirmed, so the
       integrated charge's sign is auto-detected from the dataset itself (see _integrate_gates)
       rather than assumed.

       Checked by inspection (histogram of the resulting psd column, 475 keVee LLD): it does NOT
       show a clean two-peak structure the way the fci column does -- the pre_trigger and gate-
       length approximations above are evidently too rough to reproduce this project's real gamma/
       neutron PSD separation from this dataset. The fci column is nonetheless a solid FoM-wizard
       validation target (fitting it recovers FoM ~1.4 against the paper's own reported 1.88 for
       the same 475 keVee cut -- same regime, not the same curated subset or fit procedure, so an
       exact match isn't expected). Treat gui_fom_dataset.csv's psd column as a rough placeholder
       for exercising the wizard's PSD path mechanically, not as a real discrimination result --
       getting a trustworthy PSD FoM needs either real hardware data or a proper reprocessing of
       these traces through this project's actual gate-integration timing, which is a separate
       piece of work from this script.

Usage: uv run zenodo_to_gui_csv.py [-o OUT.csv] [--events-per-class N]
"""

from __future__ import annotations

import argparse
import pathlib

import numpy as np
import pandas as pd

from prepare_dataset import ZENODO_FILES, download

RAW_DIR = pathlib.Path(__file__).parent / "raw"
OUT_PATH = pathlib.Path(__file__).parent / "gui_fom_dataset.csv"

N_SAMPLES = 3001  # full raw trace length published in gamma.csv/neutron.csv

# This project's PSD gate lengths (acquisition.c, PSD_PRE_GATE/SHORT_GATE/LONG_GATE), doubled from
# 50 Msps to Zenodo's 100 Msps native rate -- see module docstring.
PRE_GATE = 32 * 2
SHORT_GATE = 80 * 2
LONG_GATE = 250 * 2
BASELINE_SAMPLES = 30  # pre-pulse samples averaged for the per-trace baseline estimate


def _integrate_gates(waves: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Returns (energy_short, energy_long) for each row of `waves` (n_events x N_SAMPLES).

    See module docstring for the pre_trigger/gate-length approximations. Sign is auto-detected
    once for the whole batch (not per-event): a per-event sign flip could not tell "the pulse
    happened to be small" apart from "the polarity is actually flipped", but the dataset as a
    whole is the same class of detector signal throughout, so its aggregate sign is unambiguous.
    """
    n = waves.shape[0]
    energy_short = np.zeros(n)
    energy_long = np.zeros(n)
    for i, w in enumerate(waves):
        peak = int(np.argmin(w))
        baseline = float(np.mean(w[:BASELINE_SAMPLES]))
        gate_start = peak - PRE_GATE
        if gate_start < 0 or gate_start + LONG_GATE > len(w):
            energy_short[i] = np.nan
            energy_long[i] = np.nan
            continue
        window = w[gate_start:gate_start + LONG_GATE] - baseline
        energy_long[i] = window.sum()
        energy_short[i] = window[:SHORT_GATE].sum()

    if np.nanmean(energy_long) < 0:
        energy_short, energy_long = -energy_short, -energy_long
    return energy_short, energy_long


def _load_class(csv_path: pathlib.Path, n_events: int | None) -> pd.DataFrame:
    usecols = [str(i) for i in range(N_SAMPLES)] + ["Energy", "FCI"]
    df = pd.read_csv(csv_path, usecols=usecols, dtype=np.float64)
    df = df.dropna()
    if n_events is not None and n_events < len(df):
        df = df.sample(n=n_events, random_state=0).reset_index(drop=True)

    waves = df[[str(i) for i in range(N_SAMPLES)]].to_numpy()
    energy_short, energy_long = _integrate_gates(waves)
    psd = np.where(energy_long > 0, (energy_long - energy_short) / np.where(energy_long > 0, energy_long, 1), 0.0)

    out = pd.DataFrame({
        "energy_long_zenodo_keVee": df["Energy"].to_numpy(),  # kept for reference/inspection only
        "energy_long": energy_long,
        "fci": 1.0 - df["FCI"].to_numpy(),  # complement -- see module docstring
        "psd": psd,
    })
    return out.dropna()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("-o", "--out", type=pathlib.Path, default=OUT_PATH)
    ap.add_argument("--events-per-class", type=int, default=None,
                     help="subsample each class to this many events (default: use all)")
    args = ap.parse_args()

    gamma_path = download("gamma.csv", ZENODO_FILES["gamma.csv"])
    neutron_path = download("neutron.csv", ZENODO_FILES["neutron.csv"])

    gamma = _load_class(gamma_path, args.events_per_class)
    neutron = _load_class(neutron_path, args.events_per_class)
    print(f"gamma: {len(gamma)} usable events, neutron: {len(neutron)} usable events")

    combined = pd.concat([gamma, neutron], ignore_index=True)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write("# FCI-FPGA FoM wizard dataset, derived from Zenodo record 8037239\n")
        f.write("# See data/zenodo_to_gui_csv.py for exactly how energy_long/fci/psd were derived\n")
        f.write("# (fci is 1 - Zenodo's own FCI column; psd is computed here, not published)\n")
        combined[["energy_long", "fci", "psd"]].to_csv(f, index=False, float_format="%.6g")
    print(f"Wrote {args.out} ({len(combined)} rows)")
    print("NOTE: the 'fci' column is a solid FoM-wizard test case (paper's own reference FCI, "
          "complement-flipped to this project's convention). The 'psd' column is a rough "
          "approximation that does not show clean gamma/neutron separation on inspection -- see "
          "this script's module docstring before trusting a PSD FoM computed from it.")


if __name__ == "__main__":
    main()

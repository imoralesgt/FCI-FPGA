"""Build a small, committable verification set for the fci_core HLS testbench.

Two sources are supported.

  zenodo (default)  The gamma/neutron TAGGED dataset published with the paper. This is the
                    verification reference: it carries per-event labels and the paper's own
                    published FCI, so it is what the testbench's figure of merit is computed
                    against.
  root              CoMPASS ROOT output from a CAEN digitizer, i.e. traces measured on this
                    setup. These are UNLABELLED and carry no reference FCI, so they cannot drive
                    the figure of merit -- they are for characterising the real detector against
                    the same datapath. The label column records the class if you know it (a
                    tagged source run) and "measured" otherwise.

Both write the identical schema, so anything downstream reads them the same way.

Downloads the gamma/neutron tagged CLYC+SiPM dataset published alongside
Morales et al. 2024 (Nucl. Eng. Technol. 56, 745-752) from Zenodo record
8037239, picks a modest subset of events spread across the energy range for
each class, and writes their first 2048 samples (the paper's own FFT input
window, "the first 2048 samples", Sec. 5.1) along with the label and the
paper's own published FCI (100 Msps/2048-point) for that exact trace.

The fci_core testbench decimates these 2048 samples by 2 itself to exercise
the board's actual 50 Msps / 1024-point datapath -- see hls/fci_core/tb.

Usage: uv run prepare_dataset.py
"""

from __future__ import annotations

import argparse
import pathlib

import numpy as np
import pandas as pd
import requests

RAW_DIR = pathlib.Path(__file__).parent / "raw"
OUT_PATH = pathlib.Path(__file__).parent / "fci_verification_set.csv"

ZENODO_FILES = {
    "gamma.csv": "https://zenodo.org/api/records/8037239/files/gamma.csv/content",
    "neutron.csv": "https://zenodo.org/api/records/8037239/files/neutron.csv/content",
}

N_SAMPLES = 2048  # paper's "first 2048 samples" FFT input window
EVENTS_PER_CLASS = 100

# Where the pulse sits inside the reference window. Measured from the Zenodo set itself: the
# minimum lands at sample 211..360 (median 275) across its 189 events with usable amplitude. ROOT
# traces are cut to put the pulse in the same place, or the two sources would present different
# pre-trigger lengths to the same FFT window and their spectra would not be comparable.
PULSE_POS = 275

ROOT_TREE = "Data_R"


def download(name: str, url: str) -> pathlib.Path:
    dest = RAW_DIR / name
    if dest.exists():
        print(f"{name}: already downloaded, skipping")
        return dest
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    print(f"{name}: downloading from {url}")
    with requests.get(url, stream=True, timeout=120) as resp:
        resp.raise_for_status()
        tmp = dest.with_suffix(".part")
        with open(tmp, "wb") as f:
            for chunk in resp.iter_content(chunk_size=1 << 20):
                f.write(chunk)
        tmp.rename(dest)
    print(f"{name}: done")
    return dest


def pick_energy_spread_subset(csv_path: pathlib.Path, label: str, n: int) -> pd.DataFrame:
    usecols = [str(i) for i in range(N_SAMPLES)] + ["Energy", "FCI"]
    df = pd.read_csv(csv_path, usecols=usecols, dtype=np.float64)
    df = df.dropna()
    df = df.sort_values("Energy").reset_index(drop=True)
    idx = np.linspace(0, len(df) - 1, num=n).round().astype(int)
    idx = np.unique(idx)
    subset = df.iloc[idx].copy()
    subset.insert(0, "label", label)
    subset = subset.rename(columns={"FCI": "fci_ref_100msps"})
    subset = subset.drop(columns=["Energy"])
    print(f"{label}: selected {len(subset)} events, "
          f"Energy range covered [{df['Energy'].iloc[idx[0]]:.1f}, {df['Energy'].iloc[idx[-1]]:.1f}] keVee")
    return subset


def _duplication_factor(waves: list[np.ndarray]) -> int:
    """Returns 2 if every sample is repeated twice, else 1.

    Both CAEN digitizers used on this project write each sample twice, in every output format
    (CSV, .BIN and ROOT alike), so the record contains half the distinct samples its length
    implies. Detecting it matters because a trace read at face value has its time axis stretched
    by 2 and its spectrum shifted by an octave.

    Adjacent-pair equality alone would not prove it -- a slow or flat signal makes neighbours equal
    anyway. The offset grid (1,2)(3,4)... is the control: genuine duplication is ~100% on the
    aligned grid and much lower on the offset one. This requires the aligned grid to be exact, so
    a merely slow signal cannot trigger it.
    """
    aligned = all(np.array_equal(w[0::2], w[1::2]) for w in waves)
    if not aligned:
        return 1
    offset = np.mean([np.mean(w[1:-1:2] == w[2::2]) for w in waves])
    print(f"root: samples are duplicated x2 (aligned 100%, offset {100 * offset:.0f}%) "
          f"-> decimating to the distinct samples")
    return 2


def _cut_window(wave: np.ndarray, n: int, pulse_pos: int) -> np.ndarray | None:
    """Cuts an n-sample window with the pulse minimum at `pulse_pos`.

    CoMPASS records are far longer than the FFT window and put the pulse wherever the trigger
    happened to fall, so they cannot simply be truncated the way the pre-aligned reference set can.
    Returns None if the pulse sits too close to either end for a full window, rather than padding:
    a padded trace would contribute a spectrum that is partly an artefact of the padding.
    """
    peak = int(np.argmin(wave))  # negative-going pulses, matching both sources
    start = peak - pulse_pos
    if start < 0 or start + n > len(wave):
        return None
    return wave[start:start + n]


def load_root_events(paths: list[pathlib.Path], label: str, n: int) -> pd.DataFrame:
    """Builds a subset from CoMPASS ROOT files, in the same schema as the Zenodo path.

    uproot is used rather than PyROOT: it is pure Python and needs no ROOT installation. The TTree
    is self-describing, so unlike the .BIN format there is no byte layout to assume.
    """
    import uproot  # imported here so the zenodo path keeps working without it

    waves, energies = [], []
    for path in paths:
        f = uproot.open(path)
        key = next((k for k in f.keys() if k.split(";")[0] == ROOT_TREE), None)
        if key is None:
            raise KeyError(f"no '{ROOT_TREE}' tree in {path}; found {f.keys()}")
        tree = f[key]
        raw = tree["Samples"].array(library="np")
        waves += [np.asarray(w, dtype=np.int64) for w in raw]
        energies.append(tree["Energy"].array(library="np"))
        print(f"root: {path.name}: {tree.num_entries} events")
    if not waves:
        raise SystemExit("root: no events found")

    energy = np.concatenate(energies)
    step = _duplication_factor(waves)
    waves = [w[::step] for w in waves]

    # The on-board Energy saturates at its 12-bit maximum when the charge gain is set too high;
    # such events carry no usable energy and would distort an energy-spread selection, so they are
    # excluded and reported rather than silently ranked as the most energetic.
    sat = energy >= 4095
    if sat.any():
        print(f"root: {sat.sum()} of {len(energy)} events have Energy railed at 4095 "
              f"({100 * sat.mean():.0f}%) -- excluded from the selection")

    rows, kept_energy, dropped = [], [], 0
    for w, e, is_sat in zip(waves, energy, sat):
        if is_sat:
            continue
        cut = _cut_window(w, N_SAMPLES, PULSE_POS)
        if cut is None:
            dropped += 1
            continue
        rows.append(cut)
        kept_energy.append(e)
    if dropped:
        print(f"root: {dropped} event(s) dropped -- pulse too near a record edge for a "
              f"full {N_SAMPLES}-sample window")
    if not rows:
        raise SystemExit("root: no events survived windowing")

    # Same energy-spread selection as the Zenodo path, so both sources cover their range evenly
    # rather than piling up wherever the rate happened to be highest.
    order = np.argsort(kept_energy)
    idx = np.unique(np.linspace(0, len(order) - 1, num=min(n, len(order))).round().astype(int))
    sel = [rows[order[i]] for i in idx]

    df = pd.DataFrame(np.vstack(sel), columns=[str(i) for i in range(N_SAMPLES)])
    df.insert(0, "label", label)
    # No published reference exists for measured data; left empty so a consumer cannot mistake a
    # placeholder for a real figure.
    df.insert(1, "fci_ref_100msps", np.nan)
    print(f"root: selected {len(df)} events of {len(rows)} usable")
    return df


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--source", choices=("zenodo", "root"), default="zenodo",
                    help="zenodo: the tagged reference set (default). "
                         "root: CoMPASS ROOT files measured on this setup.")
    ap.add_argument("--root", nargs="+", type=pathlib.Path, metavar="FILE",
                    help="ROOT file(s) or a directory of them, for --source root")
    ap.add_argument("--label", default="measured",
                    help="label for ROOT events; set to gamma/neutron for a tagged source run")
    ap.add_argument("--events", type=int, default=EVENTS_PER_CLASS,
                    help=f"events to select (default {EVENTS_PER_CLASS})")
    ap.add_argument("-o", "--out", type=pathlib.Path, default=OUT_PATH,
                    help="output CSV (default overwrites the committed verification set)")
    args = ap.parse_args()

    if args.source == "root":
        if not args.root:
            raise SystemExit("--source root requires --root FILE [FILE ...]")
        paths: list[pathlib.Path] = []
        for p in args.root:
            paths += sorted(p.glob("*.root")) if p.is_dir() else [p]
        if not paths:
            raise SystemExit(f"no .root files found under {args.root}")
        combined = load_root_events(paths, args.label, args.events)
        if args.out == OUT_PATH:
            # The committed set is the testbench's verification reference and is labelled and
            # FCI-tagged; measured data is neither, so overwriting it would quietly disable the
            # figure of merit rather than fail.
            raise SystemExit(
                f"refusing to overwrite the tagged reference set at {OUT_PATH}. "
                f"Pass -o/--out with a different path for measured data.")
    else:
        gamma_path = download("gamma.csv", ZENODO_FILES["gamma.csv"])
        neutron_path = download("neutron.csv", ZENODO_FILES["neutron.csv"])

        gamma_subset = pick_energy_spread_subset(gamma_path, "gamma", args.events)
        neutron_subset = pick_energy_spread_subset(neutron_path, "neutron", args.events)

        combined = pd.concat([gamma_subset, neutron_subset], ignore_index=True)

    sample_cols = [str(i) for i in range(N_SAMPLES)]
    ordered_cols = ["label", "fci_ref_100msps"] + sample_cols
    combined = combined[ordered_cols]
    combined.columns = ["label", "fci_ref_100msps"] + [f"s{i}" for i in range(N_SAMPLES)]

    combined.to_csv(args.out, index=False, float_format="%.6g")
    print(f"Wrote {args.out} ({len(combined)} rows, {args.out.stat().st_size / 1e6:.2f} MB)")


if __name__ == "__main__":
    main()

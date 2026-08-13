"""Build a small, committable verification set for the fci_core HLS testbench.

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


def main() -> None:
    gamma_path = download("gamma.csv", ZENODO_FILES["gamma.csv"])
    neutron_path = download("neutron.csv", ZENODO_FILES["neutron.csv"])

    gamma_subset = pick_energy_spread_subset(gamma_path, "gamma", EVENTS_PER_CLASS)
    neutron_subset = pick_energy_spread_subset(neutron_path, "neutron", EVENTS_PER_CLASS)

    combined = pd.concat([gamma_subset, neutron_subset], ignore_index=True)

    sample_cols = [str(i) for i in range(N_SAMPLES)]
    ordered_cols = ["label", "fci_ref_100msps"] + sample_cols
    combined = combined[ordered_cols]
    combined.columns = ["label", "fci_ref_100msps"] + [f"s{i}" for i in range(N_SAMPLES)]

    combined.to_csv(OUT_PATH, index=False, float_format="%.6g")
    print(f"Wrote {OUT_PATH} ({len(combined)} rows, {OUT_PATH.stat().st_size / 1e6:.2f} MB)")


if __name__ == "__main__":
    main()

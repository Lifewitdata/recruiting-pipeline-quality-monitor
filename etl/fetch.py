"""Acquire the JTH source files and prove they are the bytes the checks ran on.

JTH (Job Tracking History) is third-party research data published on Zenodo under
CC BY-NC 4.0. It holds pseudonymised records of real job candidates, so the CSVs
are deliberately NOT committed to this repo: `data/` is gitignored and this module
re-downloads on demand.

Every file is verified against a pinned sha256. That is what makes the rest of the
project honest - a green fetch means the numbers in the README describe the exact
bytes Zenodo served, not whatever happened to be on someone's laptop when they ran
the monitor.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
DOCS_DIR = ROOT / "docs" / "upstream"

RECORD_ID = 21390581
RECORD_URL = f"https://zenodo.org/records/{RECORD_ID}"
DOWNLOAD_URL = f"https://zenodo.org/api/records/{RECORD_ID}/files/{{name}}/content"

# name -> (sha256, expected bytes). Sizes are pinned alongside the hashes so a
# truncated download is reported as a size mismatch instead of a hash mismatch.
FILES = {
    "candidates.csv": ("4328ceb1678757ce19b62105e97cca70759f38c332bef13cf9746badbbf3f692", 19_880_295),
    "jobs.csv": ("5bcd9e4379e558b98418e2242d321757612a96887b934e0d3ece6eb4a9952628", 2_394_286),
    "history.csv": ("0348bb0017156a91fe40fdd4346846982587940a0796dae3941da55312ade2a3", 2_749_081),
}
# The dataset card is the publisher's own set of claims. The monitor reconciles
# those claims against the files, so the card is committed and its hash pinned:
# a claim reconciliation is only reproducible if the claim text is frozen.
CARD = {
    "dataset_card.md": ("f49d9e28e68d68d151305e20723ccffbfbf141d299707be8c58c53db5981b07f", 18_095),
}

ATTRIBUTION = (
    "Romero, J., Millet, Y., Behar, E. (2026). JTH: A Dataset for Evaluating "
    "Cold-Start and Temporal Dynamics in Job Recommendation. Zenodo record "
    f"{RECORD_ID}, CC BY-NC 4.0. {RECORD_URL}"
)


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 22), b""):
            h.update(chunk)
    return h.hexdigest()


def check(path: Path, digest: str, size: int) -> str | None:
    """Return None when the file matches the pinned bytes, else a reason string."""
    if not path.exists():
        return "missing"
    if path.stat().st_size != size:
        return f"size {path.stat().st_size} != pinned {size}"
    found = sha256(path)
    if found != digest:
        return f"sha256 {found[:16]}... != pinned {digest[:16]}..."
    return None


def download(name: str, dest: Path) -> None:
    url = DOWNLOAD_URL.format(name=name)
    print(f"  downloading {name} ...", flush=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    req = urllib.request.Request(url, headers={"User-Agent": "jth-quality-monitor/1.0"})
    with urllib.request.urlopen(req, timeout=300) as resp, tmp.open("wb") as fh:
        while True:
            chunk = resp.read(1 << 20)
            if not chunk:
                break
            fh.write(chunk)
    tmp.replace(dest)


def verify(strict: bool = False) -> int:
    """Validate every pinned file. `strict` also requires the CSVs to be present."""
    failures = []
    for name, (digest, size) in {**CARD, **FILES}.items():
        dest = (DOCS_DIR if name == "dataset_card.md" else DATA_DIR) / name
        reason = check(dest, digest, size)
        if reason is None:
            print(f"  ok   {dest.relative_to(ROOT)}")
        elif name in FILES and not strict:
            print(f"  SKIP {dest.relative_to(ROOT)} ({reason}) - not required for this command")
        else:
            print(f"  FAIL {dest.relative_to(ROOT)} ({reason})")
            failures.append(name)
    if failures:
        print(
            "\nchecksum verification failed for: "
            + ", ".join(failures)
            + f"\nre-acquire with:  python etl/fetch.py --refresh\nattribution: {ATTRIBUTION}",
            file=sys.stderr,
        )
        return 1
    return 0


def fetch(refresh: bool = False) -> int:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    for name, (digest, size) in {**CARD, **FILES}.items():
        dest = (DOCS_DIR if name == "dataset_card.md" else DATA_DIR) / name
        reason = check(dest, digest, size)
        if reason is None:
            print(f"  ok   {dest.relative_to(ROOT)}")
            continue
        if reason.startswith("sha256") and not refresh:
            # A hash mismatch on an existing file is suspicious: downloading over
            # it silently would hide whether the file was tampered with or the pin
            # is stale. Say so and stop.
            print(f"  FAIL {dest.relative_to(ROOT)} ({reason}) - use --refresh to re-download", file=sys.stderr)
            return 1
        download(name, dest)
        reason = check(dest, digest, size)
        if reason is not None:
            print(f"  FAIL {name} after download ({reason})", file=sys.stderr)
            return 1
        print(f"  ok   {dest.relative_to(ROOT)} (verified)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--refresh", action="store_true", help="re-download even if a file exists")
    ap.add_argument("--offline", action="store_true", help="only verify pinned checksums")
    args = ap.parse_args()

    if args.offline:
        return verify(strict=True)
    return fetch(refresh=args.refresh)


if __name__ == "__main__":
    raise SystemExit(main())

import argparse
import logging
import sys

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

from sciknot.extract.extractor import run_extraction

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=12)
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()
    run_extraction(workers=args.workers, limit=args.limit)

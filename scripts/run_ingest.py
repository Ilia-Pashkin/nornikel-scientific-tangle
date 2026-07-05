import argparse
import logging
import sys

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

from sciknot.ingest.pipeline import run_ingest

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--categories", default="Обзоры,Статьи,Доклады")
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()
    run_ingest([c.strip() for c in args.categories.split(",")], limit=args.limit)

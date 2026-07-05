import logging
import sys

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

import argparse

from sciknot.graph.loader import run_load

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--stages", default="chunks,embeddings,extractions")
    args = ap.parse_args()
    run_load(set(s.strip() for s in args.stages.split(",")))

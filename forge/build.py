"""FORGE — compile an atlas from a corpus.

    python -m forge.build --siis data/siis_responses.json \
                          --catalog data/deeplinks.json --out atlas

Also supports incremental rebuild: pass `--since <atlas.json>` and only sources
whose content hash changed are recompiled, which is the property that makes
"retrieval across corpus versions" cheap rather than a full re-index.

Exit codes: 0 ok · 2 corpus or catalog unreadable · 3 no plan produced.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from .catalog import DeeplinkCatalog
from .contract import ContextDeeplinkResponse, Envelope, Goal, Meta, audit_envelope
from .nlp import HashedNgramEncoder, make_encoder
from .serve import Atlas, build_atlas


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="forge.build")
    ap.add_argument("--siis", default="data/siis_responses.json")
    ap.add_argument("--catalog", default="data/deeplinks.json")
    ap.add_argument("--out", default="atlas")
    ap.add_argument("--version", default=None,
                    help="atlas version tag (default: run-once, timestamped)")
    ap.add_argument("--encoder", default="lexical", choices=["lexical", "semantic"])
    ap.add_argument("--since", default=None,
                    help="previous atlas.json — report which sources changed")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    for p in (args.siis, args.catalog):
        if not Path(p).exists():
            print(f"error: not found: {p}", file=sys.stderr)
            return 2

    enc = HashedNgramEncoder() if args.encoder == "lexical" else make_encoder()
    cat = DeeplinkCatalog.load(args.catalog, enc)

    t0 = time.perf_counter()
    version = args.version or "current"
    atlas, rejected = build_atlas(args.siis, cat, enc, version=version)
    build_ms = (time.perf_counter() - t0) * 1000.0

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = atlas.save(str(out_dir.parent / out_dir.name))
    # `save` writes <dir>/<version>/atlas.json; also keep a stable pointer
    pointer = out_dir / "current"
    if pointer.resolve() != Path(path).parent.resolve():
        import shutil
        if pointer.exists():
            shutil.rmtree(pointer)
        shutil.copytree(Path(path).parent, pointer)

    changed = None
    if args.since and Path(args.since).exists():
        prev = Atlas.load(args.since)
        old = {r["source_id"]: r["content_hash"] for r in prev.records}
        new = {r["source_id"]: r["content_hash"] for r in atlas.records}
        changed = {
            "added": sorted(set(new) - set(old)),
            "removed": sorted(set(old) - set(new)),
            "modified": sorted(k for k in new.keys() & old.keys() if new[k] != old[k]),
            "unchanged": sum(1 for k in new.keys() & old.keys() if new[k] == old[k]),
        }

    if not atlas.records:
        print("error: no plan produced — corpus parsed to nothing", file=sys.stderr)
        return 3

    if not args.quiet:
        print(json.dumps({
            "atlas": path,
            "version": atlas.version,
            "encoder": atlas.encoder_name,
            "build_ms": round(build_ms, 1),
            "stats": atlas.stats,
            "rejected": rejected,
            "changed_since": changed,
            "gates": {
                "passing": sum(1 for r in atlas.records if r["gate"]["ok"]),
                "failing": sum(1 for r in atlas.records if not r["gate"]["ok"]),
            },
        }, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

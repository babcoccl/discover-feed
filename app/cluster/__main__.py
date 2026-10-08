"""Cluster unassigned articles in the configured DB: ``python -m app.cluster [--rebuild]``."""

import argparse
import json

from app.cluster.service import run_clustering
from app.db import init_db, make_engine, make_session_factory
from app.pipeline import clusterer_from_settings
from app.settings import get_settings


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--rebuild", action="store_true", help="drop all stories and recluster every article"
    )
    args = parser.parse_args()
    settings = get_settings()
    engine = make_engine(settings.database_url)
    try:
        init_db(engine)
        result = run_clustering(
            make_session_factory(engine), clusterer_from_settings(settings), rebuild=args.rebuild
        )
    finally:
        engine.dispose()
    print(json.dumps(result.model_dump(exclude={"joins"}), indent=2))


if __name__ == "__main__":
    main()

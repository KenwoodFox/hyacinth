import asyncio
import logging
from datetime import datetime
from functools import cache

from celery import Task

from hyacinth.celery import app
from hyacinth.db.listing import save_listings as save_listings_to_db
from hyacinth.models import SearchSpec, SearchSpecSource
from hyacinth.sources.abc import ListingSource
from hyacinth.sources.craigslist import CraigslistSearchParams, CraigslistSource

_logger = logging.getLogger(__name__)


class SearchTask(Task):  # pylint: disable=abstract-method
    @cache
    def make_source(self, search_spec: SearchSpec) -> ListingSource:
        _logger.debug(f"Instantiating new source for {search_spec}")
        if search_spec.source == SearchSpecSource.CRAIGSLIST:
            if not isinstance(search_spec.search_params, CraigslistSearchParams):
                raise ValueError(
                    "Search params for Craigslist source has incorrect type"
                    f" {type(search_spec.search_params)}"
                )
            return CraigslistSource(search_spec.search_params)

        raise NotImplementedError(f"{search_spec.source} not implemented")


@app.task(base=SearchTask)
def get_and_save_listings(search_spec_json: str, after_time_isoformat: str) -> None:
    search_spec = SearchSpec.parse_raw(search_spec_json)
    after_time = datetime.fromisoformat(after_time_isoformat)

    source = get_and_save_listings.make_source(search_spec)
    _logger.info(
        f"Starting task to get {search_spec.source} listings since {after_time} for"
        f" search_params={search_spec.search_params}"
    )
    listings = asyncio.run(source.get_listings(after_time))
    _logger.debug(f"Found {len(listings)} since {after_time} for search_spec={search_spec}")
    save_listings_to_db(search_spec, listings)
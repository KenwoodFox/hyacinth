from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

import discord
from apscheduler.triggers.interval import IntervalTrigger
from pydantic import BaseModel

from hyacinth.db.notifier import save_notifier
from hyacinth.filters import Filter
from hyacinth.models import Listing, SearchSpec
from hyacinth.monitor import MarketplaceMonitor
from hyacinth.scheduler import get_scheduler
from hyacinth.settings import get_settings
from hyacinth.util.geo import distance_miles

if TYPE_CHECKING:
    from discord.abc import MessageableChannel

settings = get_settings()
_logger = logging.getLogger(__name__)


class ActiveSearch(BaseModel):
    spec: SearchSpec
    last_notified: datetime


class ListingNotifier(ABC):
    class Config(BaseModel):
        notification_frequency_seconds: int = settings.notification_frequency_seconds
        paused: bool = False
        active_searches: list[ActiveSearch] = []
        filters: dict[str, Filter] = {}

    def __init__(self, monitor: MarketplaceMonitor, config: ListingNotifier.Config) -> None:
        self.monitor = monitor
        self.config = config

        self.scheduler = get_scheduler()
        self.notify_job = self.scheduler.add_job(
            self._notify_new_listings,
            IntervalTrigger(seconds=self.config.notification_frequency_seconds),
            next_run_time=datetime.now(),
        )
        if self.config.paused:
            self.scheduler.pause_job(self.notify_job.id)

        for search in config.active_searches:
            self.monitor.register_search(search.spec)

    def create_search(self, search_spec: SearchSpec, last_notified: datetime | None = None) -> None:
        if last_notified is None:
            last_notified = datetime.now() - timedelta(hours=settings.notifier_backdate_time_hours)
        self.config.active_searches.append(
            ActiveSearch(
                spec=search_spec,
                last_notified=last_notified,
            )
        )

        self.monitor.register_search(search_spec)

    def pause(self) -> None:
        self.config.paused = True
        self.scheduler.pause_job(self.notify_job.id)

    def unpause(self) -> None:
        self.config.paused = False
        self.scheduler.resume_job(self.notify_job.id)

    def should_notify_listing(self, listing: Listing) -> bool:
        """
        Apply filters to the listing to see if we should notify the user.
        """
        for filter_field, filter_ in self.config.filters.items():
            if not hasattr(listing, filter_field):
                # this filter is for a field not present in this type of listing
                continue
            listing_field = getattr(listing, filter_field)

            if not filter_.test(listing_field):
                return False

        return True

    def _add_calculated_fields_to_listing(self, listing: Listing) -> Listing:
        # some search sources (e.g. fb marketplace) may explicitly provide distances
        # only calculate distance if it is not set by the original listing source
        if listing.distance_miles is None:
            geotag = (listing.location.latitude, listing.location.longitude)
            listing.distance_miles = distance_miles(settings.home_lat_long, geotag)
        return listing

    async def _get_new_listings_for_search(self, search: ActiveSearch) -> list[Listing]:
        """
        Get new listings for a given search.

        Updates the last_notified time for this search, so repeated calls will return only listings
        that have not been seen before.
        """
        new_listings = await self.monitor.get_listings(search.spec, after_time=search.last_notified)
        # add notifier-dependent calculated fields
        new_listings = list(map(self._add_calculated_fields_to_listing, new_listings))
        if new_listings:
            search.last_notified = new_listings[0].created_at
            _logger.debug(f"Most recent listing was found at {search.last_notified}")

        return new_listings

    async def _get_new_listings(self) -> list[Listing]:
        """
        Collect all new listings from all active searches
        """
        listings: list[Listing] = []
        for search in self.config.active_searches:
            listings.extend(await self._get_new_listings_for_search(search))
        if listings:
            save_notifier(self)

        _logger.debug(
            f"Found {len(listings)} to notify for across {len(self.config.active_searches)} active"
            " searches"
        )
        listings.sort(key=lambda l: l.updated_at)
        return listings

    async def _notify_new_listings(self) -> None:
        not_yet_notified_listings: list[Listing] = []
        try:
            listings = await self._get_new_listings()
            if not listings:
                return

            # apply filters
            unfiltered_listings_length = len(listings)
            listings = list(filter(self.should_notify_listing, listings))
            _logger.debug(
                f"Filtered out {unfiltered_listings_length - len(listings)} listings. Notifying"
                f" user of remaining {len(listings)} listings."
            )

            not_yet_notified_listings = listings.copy()
            for listing in listings:
                await self.notify(listing)
                not_yet_notified_listings.remove(listing)
        except asyncio.CancelledError:
            # ensure users are notified of all listings even if the task is cancelled partway
            # through notification loop
            if not_yet_notified_listings:
                _logger.debug(
                    "Listing notification process interrupted! Notifying"
                    f" {len(not_yet_notified_listings)} listings before cancelling."
                )
            for listing in not_yet_notified_listings:
                await self.notify(listing)
            raise

    def cleanup(self) -> None:
        _logger.debug("Cleaning up notifier!")
        self.scheduler.remove_job(self.notify_job.id)
        for search in self.config.active_searches:
            self.monitor.remove_search(search.spec)

    @abstractmethod
    async def notify(self, listing: Listing) -> None:
        pass


class LoggerNotifier(ListingNotifier):
    async def notify(self, listing: Listing) -> None:
        _logger.info(f"Notify listing {listing}")


class DiscordNotifier(ListingNotifier):
    def __init__(
        self,
        channel: MessageableChannel,
        monitor: MarketplaceMonitor,
        config: ListingNotifier.Config,
    ) -> None:
        super().__init__(monitor, config)
        self.channel = channel

    async def notify(self, listing: Listing) -> None:
        match (listing.location.city, listing.location.state):
            case (None, None):
                location_part = ""
            case (city, None):
                location_part = f" - {city}"
            case (None, state):
                location_part = f" - {state}"
            case (city, state):
                location_part = f" - {city}, {state}"
        distance_part = ""
        if listing.distance_miles is not None:
            distance_part = f" ({int(listing.distance_miles)} mi."
        description = (
            f"**${int(listing.price)}{location_part}{distance_part} away)**\n\n{listing.body}"
        )

        embed = discord.Embed(
            title=listing.title,
            url=listing.url,
            description=description[:2048],
            timestamp=listing.updated_at.astimezone(timezone.utc),
        )
        if listing.image_urls:
            embed.set_image(url=listing.thumbnail_url)
        await self.channel.send(embed=embed)
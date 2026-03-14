"""
AfvalhulpCollector for waste data from Afvalhulp.nl
"""

import logging
from datetime import datetime
import requests
import re

from ..base import WasteCollector
from ...models import WasteCollection
from ...const import WASTE_TYPE_GREEN, WASTE_TYPE_PAPER, WASTE_TYPE_PMD_GREY

_LOGGER = logging.getLogger(__name__)


class AfvalhulpCollector(WasteCollector):
    WASTE_TYPE_MAPPING = {
        "gft": WASTE_TYPE_GREEN,
        "papier": WASTE_TYPE_PAPER,
        "pmd+": WASTE_TYPE_PMD_GREY
    }

    MONTHS = {
        "januari": 1,
        "februari": 2,
        "maart": 3,
        "april": 4,
        "mei": 5,
        "juni": 6,
        "juli": 7,
        "augustus": 8,
        "september": 9,
        "oktober": 10,
        "november": 11,
        "december": 12,
    }

    def __init__(self, hass, waste_collector, postcode, street_number, suffix, custom_mapping):
        super().__init__(hass, waste_collector, postcode, street_number, suffix, custom_mapping)
        self.base_url = "https://mijn.afvalhulp.nl"

    def _get_token(self, html):
        """
        Extract CSRF
        """
        meta_match = re.search(r'<meta name="csrf-token" content="([^"]+)"', html)
        if meta_match:
            return meta_match.group(1)

        input_match = re.search(r'name="_token"\s+value="([^"]+)"', html)
        if input_match:
            return input_match.group(1)

        return None

    def _parse_dutch_date(self, raw_date):
        """
        Parse date strings
        """
        raw_date = raw_date.strip().lower()

        match = re.search(
            r"(?:maandag|dinsdag|woensdag|donderdag|vrijdag|zaterdag|zondag)\s+(\d{1,2})\s+([a-z]+)\s+(\d{4})",
            raw_date,
        )
        if not match:
            raise ValueError(f"Unsupported format: {raw_date}")

        day = int(match.group(1))
        month_name = match.group(2)
        year = int(match.group(3))

        month = self.MONTHS.get(month_name)
        if not month:
            raise ValueError(f"Unknown month: {month_name}")

        return datetime(year, month, day)

    def _parse_collections(self, html):
        """
        Parse upcoming dates from HTML
        """
        collections = []

        pattern = re.compile(
            r'<p class="font-bold">\s*(?P<type>[^<]+?)\s*</p>\s*<p>\s*(?P<date>[^<]+?)\s*</p>',
            re.IGNORECASE,
        )

        for match in pattern.finditer(html):
            raw_type = match.group("type").strip()
            raw_date = match.group("date").strip()

            waste_type = self.map_waste_type(raw_type.lower())
            if not waste_type:
                _LOGGER.debug("Unknown waste type: %s", raw_type)
                continue

            try:
                collection = WasteCollection.create(
                    date=self._parse_dutch_date(raw_date),
                    waste_type=waste_type,
                    waste_type_slug=raw_type.lower(),
                )
            except ValueError as err:
                _LOGGER.debug("Could not parse date '%s': %s", raw_date, err)
                continue

            if collection not in self.collections and collection not in collections:
                collections.append(collection)

        return collections

    def __get_data(self):
        """
        Fetch HTML after POST
        """
        session = requests.Session()

        headers = {
            "User-Agent": "Mozilla/5.0",
            "Referer": f"{self.base_url}/postcode",
        }

        form_page = session.get(f"{self.base_url}/postcode", headers=headers, timeout=30)
        form_page.raise_for_status()

        token = self._get_token(form_page.text)
        if not token:
            raise ValueError("Could not find CSRF token")

        payload = {
            "_token": token,
            "postcode": self.postcode.replace(" ", "").upper(),
            "housenumber": str(self.street_number),
            "addition": self.suffix or "",
        }

        result = session.post(
            f"{self.base_url}/postcode",
            data=payload,
            headers=headers,
            timeout=30,
            allow_redirects=True,
        )
        result.raise_for_status()

        return result.text

    async def update(self):
        """
        Update waste collection dates
        """
        _LOGGER.debug("Updating waste collection dates")

        try:
            html = await self.hass.async_add_executor_job(self.__get_data)
            collections = self._parse_collections(html)

            self.collections.remove_all()

            for collection in collections:
                self.collections.add(collection)

            if not collections:
                _LOGGER.warning("No waste collection dates found")

            return True

        except requests.exceptions.RequestException as exc:
            _LOGGER.error("Error occurred while fetching data: %r", exc)
            return False
        except Exception as exc:
            _LOGGER.error("Unexpected error in collector: %r", exc)
            return False
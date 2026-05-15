"""Fear & Greed Index — alternative.me (no API key required)."""
import aiohttp
from logger import log

_URL = "https://api.alternative.me/fng/?limit=1"


class FearGreedClient:
    def __init__(self):
        self._session: aiohttp.ClientSession | None = None
        self._value: int = 50
        self._label: str = "Neutral"

    async def connect(self):
        self._session = aiohttp.ClientSession()

    async def fetch(self) -> dict:
        try:
            async with self._session.get(
                _URL, timeout=aiohttp.ClientTimeout(total=6)
            ) as r:
                r.raise_for_status()
                data = await r.json(content_type=None)
                item = data["data"][0]
                self._value = int(item["value"])
                self._label = item["value_classification"]
                log("SENTIMENT", "FEAR_GREED", value=self._value, label=self._label)
        except Exception as e:
            log("SENTIMENT", "FETCH_ERROR", error=str(e))
        return {"value": self._value, "label": self._label}

    @property
    def value(self) -> int:
        return self._value

    @property
    def label(self) -> str:
        return self._label

    def size_multiplier(self) -> float:
        """Returns a sentiment-based size scaling factor (0.5 at extremes)."""
        v = self._value
        if v <= 15 or v >= 85:
            return 0.5   # extreme fear/greed — halve position size
        if v <= 25 or v >= 75:
            return 0.75  # fear/greed — reduce size
        return 1.0

    async def close(self):
        if self._session:
            await self._session.close()

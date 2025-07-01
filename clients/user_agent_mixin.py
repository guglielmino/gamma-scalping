from alpaca.data.live import OptionDataStream, StockDataStream
from alpaca.trading.client import TradingClient
from websockets.legacy import client as websockets_legacy
import msgpack
import logging

log = logging.getLogger(__name__)

USER_AGENT = "GAMMA-SCALPER"

class StreamUserAgentMixin:
    async def _connect(self) -> None:
        """Attempts to connect to the websocket endpoint.
        If the connection attempt fails a value error is thrown.

        Raises:
            ValueError: Raised if there is an unsuccessful connection
        """

        extra_headers = {
            "Content-Type": "application/msgpack",
            "User-Agent": USER_AGENT,
        }

        log.info(f"connecting to {self._endpoint}")
        self._ws = await websockets_legacy.connect(
            self._endpoint,
            extra_headers=extra_headers,
            **self._websocket_params,
        )
        r = await self._ws.recv()
        msg = msgpack.unpackb(r)
        if msg[0]["T"] != "success" or msg[0]["msg"] != "connected":
            raise ValueError("connected message not received")

class RESTUserAgentMixin:
    def _get_default_headers(self) -> dict:
        headers = self._get_auth_headers()
        headers["User-Agent"] = USER_AGENT
        return headers
    
class OptionDataStreamSigned(StreamUserAgentMixin, OptionDataStream):
    pass

class StockDataStreamSigned(StreamUserAgentMixin, StockDataStream):
    pass

class TradingClientSigned(RESTUserAgentMixin, TradingClient):
    pass
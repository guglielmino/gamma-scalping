from alpaca.data.live import OptionDataStream, StockDataStream
from websockets.legacy import client as websockets_legacy
import msgpack
import logging

log = logging.getLogger(__name__)

USER_AGENT = "GAMMA-SCALPER"

class UserAgentMixin:
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
    
class OptionDataStreamSigned(UserAgentMixin, OptionDataStream):
    pass

class StockDataStreamSigned(UserAgentMixin, StockDataStream):
    pass
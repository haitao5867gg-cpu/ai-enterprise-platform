"""A TCP relay to the sandbox, the only container on both networks.

The sandbox sits alone on an internal network. Were the API on that network too,
code in the sandbox could call the API; instead the API calls this relay, which
forwards every connection to the sandbox and nowhere else. From the sandbox the
relay only leads back to the sandbox.
"""

import asyncio
import contextlib

from aep_sandbox.config import Settings


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while chunk := await reader.read(65536):
            writer.write(chunk)
            await writer.drain()
    except (ConnectionError, asyncio.IncompleteReadError):
        pass
    finally:
        with contextlib.suppress(Exception):
            writer.close()


async def serve(settings: Settings) -> None:
    async def relay(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            up_reader, up_writer = await asyncio.open_connection(
                settings.sandbox_relay_target_host, settings.sandbox_relay_target_port
            )
        except OSError:
            writer.close()
            return
        await asyncio.gather(_pipe(reader, up_writer), _pipe(up_reader, writer))

    server = await asyncio.start_server(
        relay, settings.sandbox_mcp_host, settings.sandbox_relay_port
    )
    async with server:
        await server.serve_forever()


def main() -> None:
    asyncio.run(serve(Settings()))


if __name__ == "__main__":
    main()

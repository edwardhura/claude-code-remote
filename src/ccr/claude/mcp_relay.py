"""stdio↔Unix-socket relay for the in-process MCP permission server.

claude spawns this script per ``--mcp-config``'s ``command/args``. The relay
opens the Unix socket at ``argv[1]``, forwards bytes from claude's stdin to
the socket, and writes bytes received from the socket to claude's stdout.
The parent process (:class:`ccr.claude.mcp.McpPermissionServer`) accepts
the connection on the socket and runs the in-process MCP :class:`Server`
against it.

Pure transport, no business logic. ≤50 LOC.
"""

from __future__ import annotations

import asyncio
import sys


async def _pump_to(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    while True:
        chunk = await reader.read(4096)
        if not chunk:
            break
        writer.write(chunk)
        await writer.drain()
    writer.close()


async def _amain(socket_path: str) -> int:
    sock_reader, sock_writer = await asyncio.open_unix_connection(path=socket_path)

    loop = asyncio.get_running_loop()
    stdin_reader = asyncio.StreamReader()
    await loop.connect_read_pipe(
        lambda: asyncio.StreamReaderProtocol(stdin_reader),
        sys.stdin,
    )
    stdout_transport, stdout_protocol = await loop.connect_write_pipe(
        asyncio.streams.FlowControlMixin,
        sys.stdout,
    )
    stdout_writer = asyncio.StreamWriter(stdout_transport, stdout_protocol, None, loop)

    await asyncio.gather(
        _pump_to(stdin_reader, sock_writer),
        _pump_to(sock_reader, stdout_writer),
        return_exceptions=True,
    )
    return 0


_USAGE_EXIT_CODE = 2
_EXPECTED_ARGV_LEN = 2


def main() -> int:
    if len(sys.argv) < _EXPECTED_ARGV_LEN:
        sys.stderr.write("usage: python -m ccr.claude.mcp_relay <socket_path>\n")
        return _USAGE_EXIT_CODE
    return asyncio.run(_amain(sys.argv[1]))


if __name__ == "__main__":
    sys.exit(main())

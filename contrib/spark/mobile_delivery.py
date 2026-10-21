"""Ordered mobile delivery that never waits on a socket in the native event reader."""
import asyncio
import json


class MobileDelivery:
    MAX_MESSAGES = 256
    MAX_BYTES = 16 * 1024 * 1024
    MAX_REPLAYS = 2
    SEND_TIMEOUT = 10

    def __init__(self, ws, spawn, failed):
        self.ws, self.failed = ws, failed
        self.queue = asyncio.Queue()
        self.messages = self.bytes = self.replays = 0
        self.closed = False
        self.writer = spawn(self.write())

    def offer(self, packet):
        payload = json.dumps({'jsonrpc': '2.0', **packet})
        return self.enqueue(iter((payload,)), len(payload.encode('utf-8')), False)

    def replay(self, packets):
        # Keep at most two history snapshots, generated lazily rather than copied into
        # thousands of queued frames. Later live events stay behind their snapshot.
        return self.enqueue(iter(packets), 0, True)

    def enqueue(self, packets, size, replay):
        if self.closed:
            return False
        if self.messages >= self.MAX_MESSAGES or self.bytes + size > self.MAX_BYTES or (replay and self.replays >= self.MAX_REPLAYS):
            self.failed('Mobile delivery backlog exceeded')
            return False
        self.messages += 1
        self.bytes += size
        self.replays += int(replay)
        self.queue.put_nowait((packets, size, replay))
        return True

    def release(self, size, replay):
        self.messages -= 1
        self.bytes -= size
        self.replays -= int(replay)
        self.queue.task_done()

    def stop(self):
        self.closed = True
        if self.writer is not asyncio.current_task():
            self.writer.cancel()
        while not self.queue.empty():
            _, size, replay = self.queue.get_nowait()
            self.release(size, replay)

    async def write(self):
        try:
            while not self.closed:
                packets, size, replay = await self.queue.get()
                try:
                    for index, packet in enumerate(packets):
                        payload = packet if isinstance(packet, str) else json.dumps({'jsonrpc': '2.0', **packet})
                        async with asyncio.timeout(self.SEND_TIMEOUT):
                            await self.ws.send_str(payload)
                        if index % 16 == 15:
                            await asyncio.sleep(0)
                finally:
                    self.release(size, replay)
        except asyncio.CancelledError:
            raise
        except Exception:
            self.failed('Mobile delivery failed; reconnect to load saved progress')

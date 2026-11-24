"""Retry transient Modal Compose startup failures before an agent can run."""

import asyncio

from harbor.environments.modal import ModalEnvironment


class RetryingModalEnvironment(ModalEnvironment):
    async def start(self, force_build: bool) -> None:
        delays = (5, 15)
        for attempt in range(len(delays) + 1):
            try:
                await super().start(force_build)
                return
            except RuntimeError as error:
                message = str(error).lower()
                compose_failure = message.startswith(("docker compose up failed:", "docker compose build failed:"))
                transient = "toomanyrequests" in message or "failed to execute bake: read |0: file already closed" in message
                if not compose_failure or not transient or attempt == len(delays):
                    raise
                # Harbor logs and swallows some teardown errors. Verify the old
                # sandbox stopped before starting a replacement.
                sandbox = self._sandbox
                await super().stop(delete=True)
                if sandbox is not None and await sandbox.poll.aio() is None:
                    raise RuntimeError("Failed Modal sandbox is still running; refusing startup retry") from error
                self.logger.warning("Transient Modal container startup failure; retry %s/2 in %ss: %s",
                                    attempt + 1, delays[attempt], str(error)[:500])
                await asyncio.sleep(delays[attempt])

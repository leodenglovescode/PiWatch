"""Health transitions and outage timing."""
import logging
import time

log = logging.getLogger("piwatch")

class StateTracker:

    def __init__(
        self,
        name,
        suspect_after,
        down_after,
        recover_after,
    ):
        self.name = name
        self.state = "UNKNOWN"

        self.failures = 0
        self.successes = 0

        self.suspect_after = suspect_after
        self.down_after = down_after
        self.recover_after = recover_after

        self.last_change = time.time()
        self.down_since = None
        self.last_outage_seconds = None


    def update(self, healthy):

        old_state = self.state

        if healthy:

            self.failures = 0
            self.successes += 1

            if self.state == "UNKNOWN":
                self.state = "UP"

            elif self.state in (
                "SUSPECT",
                "DOWN",
            ):

                if (
                    self.successes
                    >= self.recover_after
                ):

                    self.state = "UP"

                    if (
                        old_state == "DOWN"
                        and self.down_since is not None
                    ):
                        self.last_outage_seconds = (
                            time.time()
                            - self.down_since
                        )

                        self.down_since = None

        else:

            self.successes = 0
            self.failures += 1

            if (
                self.failures
                >= self.down_after
            ):

                if self.state != "DOWN":
                    self.down_since = time.time()

                self.state = "DOWN"

            elif (
                self.state != "DOWN"
                and self.failures >= self.suspect_after
            ):

                self.state = "SUSPECT"

        if self.state != old_state:

            self.last_change = time.time()

            if self.state == "UP":

                log.info(
                    "%s: %s -> UP",
                    self.name,
                    old_state,
                )

            elif self.state == "SUSPECT":

                log.warning(
                    "%s: %s -> SUSPECT (%d failures)",
                    self.name,
                    old_state,
                    self.failures,
                )

            elif self.state == "DOWN":

                log.error(
                    "%s: %s -> DOWN (%d failures)",
                    self.name,
                    old_state,
                    self.failures,
                )

            return (
                old_state,
                self.state,
            )

        return None

"""Sample legacy service used by the ChimericMesh demo (its log lines match the demo storm)."""

import logging

log = logging.getLogger("db01.postgres")


def write_block(block_id: int, data: bytes) -> None:
    try:
        with open(f"/var/lib/pgsql/base/{block_id}", "wb") as fh:
            fh.write(data)
    except OSError:
        # Surfaces as a FATAL line over syslog when the volume fills up.
        log.fatal("could not write block %d: No space left on device", block_id)
        raise

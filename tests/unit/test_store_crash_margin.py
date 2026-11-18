"""B2 (option B): Store.open reserves the crash margin once per unclean start; max_id_sync is a pure read."""
from chimericmesh.store.config import StoreConfig
from chimericmesh.store.schema import core_registry
from chimericmesh.store.store import Store

CFG = dict(queue_limit=50, batch_rows=10)  # crash_margin = 110


def open_store(path):
    return Store.open(path, registry=core_registry(), config=StoreConfig.from_env({}, **CFG))


def stored_counter(s):
    return s.read_sync(lambda tx: tx.counter_get("event_id"))


def test_max_id_sync_is_a_pure_read(tmp_path):
    path = tmp_path / "a.db"
    open_store(path)._abandon()
    s = open_store(path)
    try:
        first = s.events.max_id_sync()
        assert s.events.max_id_sync() == first == stored_counter(s) == 110
    finally:
        s.close()


def test_clean_restart_reserves_no_margin(tmp_path):
    path = tmp_path / "b.db"
    s = open_store(path)
    s.transact_sync(lambda tx: tx.counter_raise("event_id", 7))
    s.close()
    s = open_store(path)
    try:
        assert s.previous_shutdown_clean and s.events.max_id_sync() == 7
    finally:
        s.close()


def test_two_unclean_opens_in_a_row_reserve_the_margin_twice(tmp_path):
    path = tmp_path / "c.db"
    s = open_store(path)
    s.transact_sync(lambda tx: tx.counter_raise("event_id", 5))
    s._abandon()
    s = open_store(path)
    assert s.events.max_id_sync() == 5 + 110
    s._abandon()
    s = open_store(path)
    try:
        assert s.events.max_id_sync() == 5 + 2 * 110  # intended larger gap, not an error
    finally:
        s.close()


def test_new_database_reserves_nothing(tmp_path):
    s = open_store(tmp_path / "d.db")
    try:
        assert s.previous_shutdown_clean and s.events.max_id_sync() == 0
    finally:
        s.close()

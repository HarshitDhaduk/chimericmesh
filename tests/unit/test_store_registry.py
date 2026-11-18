import pytest

from chimericmesh.store.errors import MigrationError, StoreError
from chimericmesh.store.migrations import MODULE_BLOCKS, MigrationRegistry


def test_register_inside_block_is_accepted():
    reg = MigrationRegistry()
    reg.register("store", 1, "store:0001-core-tables", ("CREATE TABLE a (x)",))
    reg.register("services", 100, "services:0100-registry", ("CREATE TABLE b (x)",))
    assert [m.version for m in reg.migrations()] == [1, 100]


def test_register_outside_block_is_rejected():
    reg = MigrationRegistry()
    with pytest.raises(MigrationError):
        reg.register("store", 100, "store:0100-x", ())
    with pytest.raises(MigrationError):
        reg.register("services", 99, "services:0099-x", ())
    with pytest.raises(MigrationError):
        reg.register("nosuchmodule", 1, "nosuch:0001", ())


def test_duplicate_version_and_name_are_rejected():
    reg = MigrationRegistry()
    reg.register("store", 1, "store:0001-a", ())
    with pytest.raises(MigrationError):
        reg.register("store", 1, "store:0001-b", ())
    with pytest.raises(MigrationError):
        reg.register("store", 2, "store:0001-a", ())


def test_migrations_sorted_by_version():
    reg = MigrationRegistry()
    reg.register("services", 101, "services:0101", ())
    reg.register("store", 2, "store:0002", ())
    reg.register("store", 1, "store:0001", ())
    assert [m.version for m in reg.migrations()] == [1, 2, 101]


def test_highest():
    reg = MigrationRegistry()
    assert reg.highest() == 0
    reg.register("store", 1, "store:0001", ())
    reg.register("connectors", 205, "connectors:0205", ())
    assert reg.highest() == 205


def test_blocks_and_error_hierarchy():
    assert MODULE_BLOCKS["store"] == (1, 99)
    assert MODULE_BLOCKS["dashboard"] == (600, 699)
    assert issubclass(MigrationError, StoreError)

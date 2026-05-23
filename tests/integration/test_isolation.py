"""Multi-table isolation: tables share a repo but never interact."""

from gitkv import GitKVStore


def test_same_key_in_two_tables_is_independent(store):
    store.create_table("pm")
    store.create_table("analytics")
    store.table("pm").set("foo", "pm-value")
    store.table("analytics").set("foo", "analytics-value")
    assert store.table("pm").get("foo") == "pm-value"
    assert store.table("analytics").get("foo") == "analytics-value"


def test_delete_in_one_table_does_not_touch_other(store):
    store.create_table("pm")
    store.create_table("analytics")
    store.table("pm").set("foo", "X")
    store.table("analytics").set("foo", "Y")
    store.table("pm").delete("foo")
    assert store.table("pm").get("foo") is None
    assert store.table("analytics").get("foo") == "Y"


def test_rotation_of_one_table_does_not_affect_other(store):
    store.create_table("pm")
    store.create_table("analytics")
    store.table("pm").set("k", "pm-v")
    store.table("analytics").set("k", "analytics-v")

    before_analytics_active, _ = store.table("analytics")._find_active()
    store.table("pm").rotate()
    after_analytics_active, _ = store.table("analytics")._find_active()

    # PM rotated → its active branch changed; analytics didn't.
    assert after_analytics_active == before_analytics_active
    # Data still readable.
    assert store.table("analytics").get("k") == "analytics-v"
    assert store.table("pm").get("k") == "pm-v"


def test_isolation_visible_across_clones(make_clone):
    a = GitKVStore(str(make_clone("client_a")))
    b = GitKVStore(str(make_clone("client_b")))
    a.create_table("pm")
    a.create_table("analytics")
    a.table("pm").set("foo", "pm")
    a.table("analytics").set("foo", "analytics")

    # B reads each table independently and gets the right value.
    assert b.table("pm").get("foo") == "pm"
    assert b.table("analytics").get("foo") == "analytics"

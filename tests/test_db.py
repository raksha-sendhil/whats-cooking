import json

from whats_cooking.models import Item


def _item(**kw):
    base = dict(source="hackernews", native_id="1", kind="story", title="Hello", url="https://example.com/a")
    return Item(**{**base, **kw})


def test_upsert_counts_new_and_merges_metrics(db):
    run = db.start_run("collect")
    assert db.upsert_items([_item(metrics={"points": 10})], run) == (1, 1)
    run2 = db.start_run("collect")
    # A second source of the same item adds metrics without wiping existing ones.
    assert db.upsert_items([_item(metrics={"comments": 5})], run2) == (1, 0)
    row = db.conn.execute("SELECT metrics, first_seen_at FROM items").fetchone()
    assert json.loads(row["metrics"]) == {"points": 10, "comments": 5}
    snaps = db.conn.execute("SELECT run_id, metrics FROM metric_snapshots ORDER BY run_id").fetchall()
    assert [json.loads(s["metrics"]) for s in snaps] == [{"points": 10}, {"comments": 5}]


def test_none_metrics_are_dropped(db):
    run = db.start_run("collect")
    db.upsert_items([_item(metrics={"points": 3, "front_page": None})], run)
    assert json.loads(db.conn.execute("SELECT metrics FROM items").fetchone()[0]) == {"points": 3}


def test_refs_written_with_primary_flag(db):
    run = db.start_run("collect")
    db.upsert_items([_item(url="https://arxiv.org/abs/2610.00001", text="also https://github.com/a/b")], run)
    refs = dict(db.conn.execute("SELECT ref, primary_ref FROM item_refs").fetchall())
    assert refs == {"arxiv:2610.00001": 1, "github:a/b": 0}


def test_text_kept_when_update_has_none(db):
    run = db.start_run("collect")
    db.upsert_items([_item(text="original body")], run)
    db.upsert_items([_item(text="")], run)
    assert db.conn.execute("SELECT text FROM items").fetchone()[0] == "original body"

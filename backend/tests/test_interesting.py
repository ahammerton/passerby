from app.interesting import ALERT_REPEAT_SECONDS, MIN_HISTORY_AIRCRAFT, Interest, assess
from app.models import Aircraft
from app.store import SightingStore


def plane(icao="abc123", **kw):
    return Aircraft(icao24=icao, source="t", last_seen=0, **kw)


COUNTS = {"A320": 900, "A346": 1, "B735": 3, "MD83": 4, "P28U": 1}


def test_emergency_squawks_come_first():
    a = plane(squawk="7700", type_code="BLCF", military=True)
    assert assess(a, COUNTS) == {"level": "emergency", "reason": "Emergency (squawk 7700)"}
    assert assess(plane(squawk="7600"), None)["reason"] == "Radio failure (squawk 7600)"
    assert assess(plane(squawk="7000"), None) is None  # UK conspicuity code: ordinary


def test_notable_types_need_no_history():
    assert assess(plane(type_code="blcf"), None) == {"level": "notable", "reason": "Boeing 747 Dreamlifter"}
    assert assess(plane(type_code="SPIT", shape="light"), None)["level"] == "notable"


def test_rare_types_are_big_ones_seen_on_few_airframes():
    rare = assess(plane(type_code="A346", shape="widebody", aircraft_name="Airbus A340 642"), COUNTS)
    assert rare == {"level": "rare", "reason": "Rare here: no other Airbus A340 642 seen in 90 days"}
    assert "only 3 B735" in assess(plane(type_code="B735", shape="narrowbody"), COUNTS)["reason"]
    assert assess(plane(type_code="MD83", shape="narrowbody"), COUNTS) is None  # 4 airframes: not rare
    assert assess(plane(type_code="A320", shape="narrowbody"), COUNTS) is None
    # A never-seen type is rare too.
    assert assess(plane(type_code="IL96", shape="widebody"), COUNTS)["level"] == "rare"
    # Small variants aren't interesting, however rarely they're seen.
    assert assess(plane(type_code="P28U", shape="light"), COUNTS) is None
    # No judgement without enough history.
    assert assess(plane(type_code="A346", shape="widebody"), None) is None


def test_military_is_listed_last():
    assert assess(plane(military=True, type_code="H47", shape="helicopter"), COUNTS) == {
        "level": "military", "reason": "Military"}


def test_rarity_waits_for_enough_history():
    interest = Interest()
    interest.set_history(COUNTS, aircraft=MIN_HISTORY_AIRCRAFT - 1, days=30, now=0)
    assert interest.type_counts is None
    interest.set_history(COUNTS, aircraft=MIN_HISTORY_AIRCRAFT, days=2, now=0)
    assert interest.type_counts is None
    interest.set_history(COUNTS, aircraft=MIN_HISTORY_AIRCRAFT, days=7, now=0)
    assert interest.type_counts == COUNTS and not interest.due(now=60) and interest.due(now=3600)


def annotated(interest, *planes):
    return interest.annotate(list(planes))


def test_alerts_once_per_airframe_and_again_for_a_new_squawk():
    interest = Interest()
    interest.set_history(COUNTS, aircraft=1000, days=30, now=0)
    beluga, emergency, chinook = plane("aaa001", type_code="A3ST"), plane("aaa002", squawk="7700"), \
        plane("aaa003", military=True)
    snap = annotated(interest, beluga, emergency, chinook)
    assert [a.icao24 for a in interest.new_alerts(snap, now=0)] == ["aaa001", "aaa002"]  # no military alerts
    assert interest.new_alerts(snap, now=60) == []
    # The emergency changes squawk: alert again.
    hijack = annotated(interest, plane("aaa002", squawk="7500"))
    assert [a.interest["reason"] for a in interest.new_alerts(hijack, now=120)] == ["Hijack (squawk 7500)"]
    # 12 hours on, the Beluga alerts again.
    assert [a.icao24 for a in interest.new_alerts(annotated(interest, beluga), now=ALERT_REPEAT_SECONDS)] == ["aaa001"]


def test_alert_settings():
    snap = annotated(Interest(), plane("aaa001", type_code="A3ST"), plane("aaa002", squawk="7700"))
    assert [a.icao24 for a in Interest("emergencies").new_alerts(snap, now=0)] == ["aaa002"]
    assert Interest("off").new_alerts(snap, now=0) == []


def test_type_history_from_the_sightings(tmp_path):
    store = SightingStore(tmp_path / "s.db")

    def plane(icao, **kw):  # the log needs a distance
        return Aircraft(icao24=icao, source="t", last_seen=0, distance_km=5.0, **kw)
    store.record([plane("aaa001", type_code="A346"), plane("aaa002", type_code="a346"),
                  plane("aaa003", type_code="A320"), plane("aaa004")], now=1000)
    store.record([plane("aaa001", type_code="A346")], now=1000 + 8 * 86400)
    counts, aircraft, days = store.type_history(since=0)
    assert counts == {"A346": 2, "A320": 1} and aircraft == 4 and round(days) == 8
    # Only airframes seen since the cut-off count.
    assert store.type_history(since=1000 + 86400)[0] == {"A346": 1}
    store.close()

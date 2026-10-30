import pytest

from app.aircraft_types import classify, shape_for_type
from app.models import Aircraft


def aircraft(**kw):
    return Aircraft(icao24="abc123", source="test", last_seen=0, **kw)


# Every type seen around home in the live check, plus the awkward ones.
@pytest.mark.parametrize("code, shape", [
    ("B77W", "widebody"), ("B772", "widebody"), ("B789", "widebody"), ("A35K", "widebody"), ("A388", "widebody"),
    ("A332", "widebody"), ("A339", "widebody"), ("B763", "widebody"), ("A310", "widebody"),
    ("A320", "narrowbody"), ("A319", "narrowbody"), ("A20N", "narrowbody"), ("A21N", "narrowbody"),
    ("B738", "narrowbody"), ("B38M", "narrowbody"), ("E190", "narrowbody"), ("CRJ9", "narrowbody"), ("BCS3", "narrowbody"),
    ("AT76", "turboprop"), ("DH8D", "turboprop"), ("F50", "turboprop"), ("SF34", "turboprop"),
    ("GL7T", "bizjet"), ("GL5T", "bizjet"), ("CL35", "bizjet"), ("C56X", "bizjet"), ("H25B", "bizjet"), ("E35L", "bizjet"),
    ("DA62", "light"), ("C172", "light"), ("C208", "light"), ("PA28", "light"), ("P32R", "light"), ("SR22", "light"),
    ("BN2T", "light"),
    ("EC35", "helicopter"), ("A109", "helicopter"), ("A139", "helicopter"), ("H47", "helicopter"), ("R44", "helicopter"),
    ("EUFI", "military"), ("F35", "military"), ("HAWK", "military"), ("A10", "military"),
    ("ASK2", "glider"), ("P3", "turboprop"),
])
def test_type_codes(code, shape):
    assert shape_for_type(code) == shape


def test_fallbacks_without_a_type_code():
    assert classify(aircraft(category="A7")) == {"shape": "helicopter", "kind": "Helicopter"}
    assert classify(aircraft(category="A5"))["shape"] == "widebody"
    assert classify(aircraft(category="C1"))["kind"] == "Ground vehicle"
    assert classify(aircraft(aircraft_name="Airbus Helicopters H145"))["shape"] == "helicopter"
    assert classify(aircraft()) == {"shape": "unknown", "kind": "Aircraft"}
    # The type code wins over a category.
    assert classify(aircraft(type_code="C172", category="A3"))["shape"] == "light"


def test_military_keep_their_shape():
    assert classify(aircraft(type_code="H47", military=True)) == {"shape": "helicopter", "kind": "Military helicopter"}
    assert classify(aircraft(type_code="A400", military=True)) == {"shape": "widebody", "kind": "Military transport"}
    assert classify(aircraft(type_code="EUFI", military=True)) == {"shape": "military", "kind": "Military jet"}
    assert classify(aircraft(military=True)) == {"shape": "military", "kind": "Military jet"}


def test_short_military_codes_dont_swallow_light_aircraft():
    assert shape_for_type("C17") == "widebody" and shape_for_type("C172") == "light" and shape_for_type("C177") == "light"


def test_unrecognised_type_codes_use_the_model_name_before_guessing_light():
    gulfstream = aircraft(type_code="G450", aircraft_name="Gulfstream Aerospace G450")
    assert classify(gulfstream)["shape"] == "bizjet"
    oddball = aircraft(type_code="ZZZ9", aircraft_name="Gulfstream Aerospace GV")
    assert classify(oddball)["shape"] == "bizjet"
    assert classify(aircraft(type_code="ZZZ9"))["shape"] == "light"

from datetime import datetime

from app.constants import OrderStatus
from app.extensions import db
from app.models import Order
from app.services.order_kpis import get_order_kpis, operational_today
from app.services.scope import get_or_create_client, get_or_create_division
from tests.conftest import create_user, login, make_order


def _kpi(**kwargs):
    today = operational_today()
    kwargs.setdefault("from_date", today)
    kwargs.setdefault("to_date", today)
    return get_order_kpis(**kwargs)


def _order(number, *, units=1, client="ACME", client_name=None, division="MAIN",
           division_name=None, order_type="ECOMMERCE", created=None, status=None,
           extra_lines=None):
    lines = [("SKU-A", units)]
    if extra_lines:
        lines = extra_lines
    order = make_order(
        number,
        lines=lines,
        client=client,
        client_name=client_name,
        division=division,
        division_name=division_name,
        order_type=order_type,
    )
    if created is not None:
        order.created_at = created
    if status is not None:
        order.status = status
    db.session.commit()
    return order


def test_default_dates_equal_current_local_day(admin_client, db):
    today = operational_today().isoformat()
    html = admin_client.get("/kpi/orders").get_data(as_text=True)
    assert f'name="from_date" value="{today}"' in html
    assert f'name="to_date" value="{today}"' in html


def test_page_auto_loads_today_without_apply(admin_client, db):
    _order("SO-AUTO", units=4)
    resp = admin_client.get("/kpi/orders")
    html = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert "KPI Orders" in html
    assert "LIVE" in html
    assert "SUMMARY ONLY" in html
    assert "Total Orders" in html
    assert ">1<" in html or "data-k=\"total_orders\">1<" in html
    assert "SO-AUTO" not in html
    assert "Pick Ticket" not in html
    assert "Apply" in html


def test_zero_data_state(admin_client, db):
    html = admin_client.get("/kpi/orders").get_data(as_text=True)
    assert "No orders found for the selected period." in html
    assert "data-k=\"total_orders\">0<" in html
    assert "Ecommerce" in html
    assert "Retail" in html
    assert "Wholesale" in html


def test_distinct_order_count_with_multiple_lines(db):
    _order("SO-ML", extra_lines=[("SKU-A", 4), ("SKU-B", 6)])
    data = _kpi()
    assert data["overall"]["total_orders"] == 1
    assert data["overall"]["total_units"] == 10


def test_open_processed_units_and_no_duplicate_on_close(db):
    data = _kpi()
    assert data["overall"]["total_orders"] == 0
    order = _order("SO-LIVE", units=10, order_type="ECOMMERCE")
    data = _kpi()
    assert data["overall"] == {
        "total_orders": 1,
        "open_orders": 1,
        "processed_orders": 0,
        "total_units": 10,
        "open_units": 10,
        "processed_units": 0,
    }
    assert data["channels"]["ecommerce"] == {"orders": 1, "units": 10, "open_orders": 1}
    order.status = OrderStatus.CLOSED
    db.session.commit()
    data = _kpi()
    assert data["overall"] == {
        "total_orders": 1,
        "open_orders": 0,
        "processed_orders": 1,
        "total_units": 10,
        "open_units": 0,
        "processed_units": 10,
    }
    assert data["channels"]["ecommerce"]["orders"] == 1
    assert data["channels"]["ecommerce"]["units"] == 10
    assert data["channels"]["ecommerce"]["open_orders"] == 0


def test_channel_counts(db):
    for i in range(2):
        _order(f"SO-E{i}", order_type="ECOMMERCE", division="D1", division_name="Division 1")
    for i in range(3):
        _order(f"SO-R{i}", order_type="RETAIL", division="D1", division_name="Division 1")
    _order("SO-W0", order_type="WHOLESALE", division="D1", division_name="Division 1")
    data = _kpi()
    assert data["overall"]["total_orders"] == 6
    assert data["channels"]["ecommerce"]["orders"] == 2
    assert data["channels"]["retail"]["orders"] == 3
    assert data["channels"]["wholesale"]["orders"] == 1
    row = data["rows"][0]
    assert row["ecommerce_orders"] == 2
    assert row["retail_orders"] == 3
    assert row["wholesale_orders"] == 1


def test_client_division_grouping_and_sort(db):
    _order("A1", client="CA", client_name="Client A", division="D1", division_name="Division 1")
    _order("B1", client="CB", client_name="Client B", division="D1", division_name="Division 1")
    _order("A2", client="CA", client_name="Client A", division="D2", division_name="Division 2")
    data = _kpi()
    pairs = [(r["client"], r["division"]) for r in data["rows"]]
    assert pairs == [
        ("Client A", "Division 1"),
        ("Client A", "Division 2"),
        ("Client B", "Division 1"),
    ]


def test_client_division_and_combined_filters(db):
    a1 = _order("A1", client="CA", client_name="Client A", division="D1", division_name="Division 1")
    a2 = _order("A2", client="CA", client_name="Client A", division="D2", division_name="Division 2")
    _order("B1", client="CB", client_name="Client B", division="D1", division_name="Division 1")
    ca = get_or_create_client("CA")
    d1 = get_or_create_division(ca, "D1")
    d2 = get_or_create_division(ca, "D2")
    by_client = _kpi(client_id=ca.id)
    assert {r["division"] for r in by_client["rows"]} == {"Division 1", "Division 2"}
    assert by_client["overall"]["total_orders"] == 2
    by_div = _kpi(division_id=d2.id)
    assert [r["client"] for r in by_div["rows"]] == ["Client A"]
    assert by_div["overall"]["total_orders"] == 1
    both = _kpi(client_id=ca.id, division_id=d1.id)
    assert both["overall"]["total_orders"] == 1
    assert both["rows"][0]["division"] == "Division 1"
    assert a1.id and a2.id


def test_search_client_or_division_only(db):
    _order("A1", client="CA", client_name="Client A", division="NORTH", division_name="North")
    _order("B1", client="CB", client_name="Client B", division="SOUTH", division_name="South")
    data = _kpi(search="north")
    assert data["overall"]["total_orders"] == 1
    assert data["rows"][0]["division"] == "North"
    data = _kpi(search="client b")
    assert data["overall"]["total_orders"] == 1
    assert data["rows"][0]["client"] == "Client B"


def test_date_range_inclusive(db):
    _order("OLD", created=datetime(2026, 9, 18, 12, 0, 0))
    _order("MID", created=datetime(2026, 9, 19, 12, 0, 0))
    _order("NEW", created=datetime(2026, 9, 20, 12, 0, 0))
    data = get_order_kpis(from_date="2026-09-18", to_date="2026-09-20", tz_name="UTC")
    assert data["overall"]["total_orders"] == 3
    data = get_order_kpis(from_date="2026-09-19", to_date="2026-09-19", tz_name="UTC")
    assert data["overall"]["total_orders"] == 1


def test_local_timezone_date_handling(db, app):
    # 2026-09-21 03:00 UTC = 2026-09-20 23:00 America/New_York (EDT, UTC-4)
    _order("NY-IN", created=datetime(2026, 9, 21, 3, 0, 0))
    # 2026-09-20 03:00 UTC = 2026-09-19 23:00 America/New_York
    _order("NY-OUT", created=datetime(2026, 9, 20, 3, 0, 0))
    data = get_order_kpis(from_date="2026-09-20", to_date="2026-09-20", tz_name="America/New_York")
    assert data["overall"]["total_orders"] == 1


def test_total_row_and_channel_cards_match(db):
    _order("E1", units=3, order_type="ECOMMERCE", division="D1", division_name="D1")
    _order("R1", units=2, order_type="RETAIL", division="D2", division_name="D2")
    data = _kpi()
    assert data["totals"]["total_orders"] == sum(r["total_orders"] for r in data["rows"])
    assert data["totals"]["total_units"] == sum(r["total_units"] for r in data["rows"])
    assert data["totals"]["ecommerce_orders"] == data["channels"]["ecommerce"]["orders"]
    assert data["totals"]["retail_units"] == data["channels"]["retail"]["units"]
    assert data["totals"]["wholesale_orders"] == data["channels"]["wholesale"]["orders"]


def test_http_filters_update_all_surfaces(admin_client, db):
    _order("A1", units=5, client="CA", client_name="Client A", division="D1",
           division_name="Division 1", order_type="RETAIL")
    ca = get_or_create_client("CA")
    html = admin_client.get(f"/kpi/orders?client_id={ca.id}").get_data(as_text=True)
    assert "Client A" in html
    assert "Division 1" in html
    assert "Client B" not in html
    assert "TOTAL" in html
    empty = admin_client.get("/kpi/orders?from_date=2099-01-01&to_date=2099-01-02")
    assert "No orders found for the selected period." in empty.get_data(as_text=True)


def test_permission_enforced(app, db):
    create_user("viewer", perms=["DASHBOARD_VIEW"])
    client = app.test_client()
    login(client, "viewer")
    assert client.get("/kpi/orders").status_code == 403
    assert client.get("/kpi/orders.json").status_code == 403


def test_json_refresh_endpoint(admin_client, db):
    _order("SO-JSON", units=2)
    resp = admin_client.get("/kpi/orders.json")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["overall"]["total_orders"] == 1
    assert body["from_date"] == operational_today().isoformat()

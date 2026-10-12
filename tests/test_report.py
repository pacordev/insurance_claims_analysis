"""
Tests for the Excel report. Same tiny SQLite sample data as the analytics
tests, so the expected numbers are known. We peek inside the .xlsx (it's just
a zip of XML files) instead of needing Excel or another library to open it.
"""

import html
import re
import zipfile
from datetime import date
from pathlib import Path

import pandas as pd
import pytest
from sqlalchemy import Engine

from app.analytics.common import Filters
from app.reports import gen_excel_report as excel
from app.reports.collect_claims_data import build_highlights, collect_frames, compute_kpis, scope_text


def kpi(kpis: list, label: str) -> float:
    return next(k.value for k in kpis if k.label == label)


def sheet_names(path: Path) -> list[str]:
    with zipfile.ZipFile(path) as z:
        names = re.findall(r'<sheet name="([^"]+)"', z.read("xl/workbook.xml").decode())
    return [html.unescape(n) for n in names]  # xml stores "&" as "&amp;"


def shared_strings(path: Path) -> str:
    with zipfile.ZipFile(path) as z:
        return z.read("xl/sharedStrings.xml").decode()


def chart_count(path: Path) -> int:
    with zipfile.ZipFile(path) as z:
        return len([n for n in z.namelist() if n.startswith("xl/charts/chart")])


# ---------- the numbers ----------


def test_headline_numbers(engine: Engine) -> None:
    kpis = compute_kpis(collect_frames(engine))
    assert kpi(kpis, "Claims") == 4
    assert kpi(kpis, "Policies") == 3
    assert kpi(kpis, "Total claimed") == 4000
    assert kpi(kpis, "Total approved") == 2300
    assert kpi(kpis, "Premium") == 2300
    assert kpi(kpis, "Loss ratio") == pytest.approx(1.0)
    assert kpi(kpis, "Approval rate") == pytest.approx(2 / 3)  # 2 approved, 1 denied, 1 pending ignored
    assert kpi(kpis, "Fraud flag rate") == 0.5
    # Ana: 7.5 days over 2 decisions, Bob: 2 days over 1 decision -> weighted by decisions
    assert kpi(kpis, "Avg processing days") == pytest.approx((7.5 * 2 + 2 * 1) / 3)


def test_highlights_name_the_worst_product(engine: Engine) -> None:
    text = " ".join(build_highlights(collect_frames(engine)))
    assert "Highest loss ratio: Auto" in text
    assert "Lowest loss ratio: Home" in text
    assert "Most claims: Ontario" in text


def test_scope_text() -> None:
    assert scope_text(Filters()) == "Scope: all claims, all products"
    text = scope_text(Filters(date(2024, 1, 1), None, "Auto"))
    assert "2024-01-01" in text and "Auto" in text


# ---------- the workbook ----------


def test_workbook_has_all_sheets_in_order(engine: Engine, tmp_path: Path) -> None:
    out = excel.build_report(engine, tmp_path / "r.xlsx")
    assert out.exists()
    assert sheet_names(out) == excel.SHEET_NAMES


def test_workbook_has_charts_and_headline_labels(engine: Engine, tmp_path: Path) -> None:
    out = excel.build_report(engine, tmp_path / "r.xlsx")
    assert chart_count(out) >= 10
    strings = shared_strings(out)
    for label in ("Loss ratio", "Claims frequency by product", "Every adjudicator", "Notes and definitions"):
        assert label in strings


def test_filters_show_up_in_the_report(engine: Engine, tmp_path: Path) -> None:
    out = excel.build_report(engine, tmp_path / "r.xlsx", Filters(product="Auto"))
    assert "product: Auto" in shared_strings(out)


def test_output_folder_is_created(engine: Engine, tmp_path: Path) -> None:
    out = excel.build_report(engine, tmp_path / "new" / "folder" / "r.xlsx")
    assert out.exists()


def test_empty_selection_still_builds(engine: Engine, tmp_path: Path) -> None:
    # no claims fall in this window, so every table/chart has nothing to show
    nothing = Filters(date_from=date(2030, 1, 1))
    out = excel.build_report(engine, tmp_path / "empty.xlsx", nothing)
    assert sheet_names(out) == excel.SHEET_NAMES
    assert "No data for this selection." in shared_strings(out)


# ---------- command line ----------


def test_cli_rejects_backwards_dates() -> None:
    with pytest.raises(SystemExit):
        excel.main(["--date-from", "2025-02-01", "--date-to", "2025-01-01"])


def test_cli_rejects_unknown_product(engine: Engine, capsys) -> None:
    with pytest.raises(SystemExit):
        excel.main(["--product", "Boat"], engine=engine)
    assert "Auto, Home" in capsys.readouterr().err  # tells you which products exist


def test_cli_accepts_any_product_in_the_database(engine: Engine, tmp_path: Path) -> None:
    out = tmp_path / "auto.xlsx"
    excel.main(["--product", "Auto", "--output", str(out)], engine=engine)
    assert out.exists()


# ---------- more than four products ----------


def _year_frame(n_products: int) -> pd.DataFrame:
    rows = [
        {"product_name": f"P{i}", "year": y, "premium_total": 100.0 * (n_products - i), "approved_total": 50.0}
        for i in range(n_products) for y in (2024, 2025)
    ]
    df = pd.DataFrame(rows)
    df["loss_ratio"] = df["approved_total"] / df["premium_total"]
    return df


def test_four_or_fewer_products_are_left_alone() -> None:
    df = _year_frame(4)
    assert excel.fold_small_products(df).equals(df)


def test_extra_products_fold_into_other() -> None:
    folded = excel.fold_small_products(_year_frame(6))
    assert set(folded["product_name"]) == {"P0", "P1", "P2", "P3", "Other"}  # top 4 by premium + Other
    other = folded[(folded["product_name"] == "Other") & (folded["year"] == 2024)].iloc[0]
    # P4 (premium 200) and P5 (premium 100) added together: 300 premium, 100 approved
    assert other["premium_total"] == 300.0
    assert other["loss_ratio"] == pytest.approx(100.0 / 300.0)


def test_product_colors_never_invent_colors() -> None:
    colors = excel.product_colors(["Auto", "Home", "Pet", "Boat", "Other"])
    assert colors["Auto"] == excel.PRODUCT_COLORS["Auto"]  # known products keep their colour
    assert colors["Other"] == excel.OTHER_GREY
    assert colors["Pet"] != colors["Boat"]
    assert {colors["Pet"], colors["Boat"]} <= set(excel.SLOTS)

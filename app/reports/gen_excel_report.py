"""
Builds the Excel report: an Overview, one sheet per metric group, and a Notes
sheet. Charts are native Excel charts, so the file works on its own - no app
or database needed to open it.

Run it:
    python -m app.reports.gen_excel_report
    python -m app.reports.gen_excel_report --date-from 2025-01-01 --product Auto --output summary_reports/auto_2025.xlsx

Chart colours follow the data-viz palette (blue, orange, aqua, yellow in that
order). Single-series charts are plain blue; a product keeps the same colour
everywhere it shows up as its own series.
"""

import argparse
import math
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import xlsxwriter
from sqlalchemy import Engine
from xlsxwriter.chart import Chart
from xlsxwriter.workbook import Workbook
from xlsxwriter.worksheet import Worksheet

from app.analytics.common import Filters, product_names
from app.reports.collect_claims_data import (
    Frames,
    Kpi,
    build_highlights,
    collect_frames,
    compute_kpis,
    scope_text,
)

# ---------- look and feel ----------

BLUE, ORANGE, AQUA, YELLOW = "#2a78d6", "#eb6834", "#1baf7a", "#eda100"
PRODUCT_COLORS = {"Home": BLUE, "Auto": ORANGE, "Life": AQUA, "Travel": YELLOW}
SLOTS = [BLUE, ORANGE, AQUA, YELLOW]  # only four validated colours: never invent more
OTHER_GREY = "#8a8a85"
MAX_PRODUCT_SERIES = 4  # multi-series product charts show the top 4 by premium, the rest fold into "Other"
METHOD_COLORS = {"EFT": BLUE, "Cheque": ORANGE, "Credit": AQUA}
INK, INK_SOFT, GRID, AXIS = "#0b0b0b", "#52514e", "#e5e4e0", "#c3c2b7"
TILE_BG = "#f6f5f3"
SCALE_LOW, SCALE_HIGH = "#cde2fb", "#6da7ec"  # light-to-mid blue, dark text stays readable

# how each kind of column gets displayed
NUM_FORMATS = {
    "int": "#,##0",
    "money": "$#,##0",
    "money2": "$#,##0.00",
    "pct": "0.0%",
    "dec1": "0.0",
    "dec2": "0.00",
    "x": '0.00"x"',
    "year": "0",
    "month": "mmm yyyy",
}

# column name -> kind (anything not listed: ints get "int", floats "dec2", the rest is text)
COLUMN_KINDS = {
    "premium_total": "money", "approved_total": "money", "paid_total": "money", "profit": "money",
    "total_claim_amount": "money", "total_paid": "money",
    "avg_claim_amount": "money2", "mean": "money2", "median": "money2", "p90": "money2", "max": "money2",
    "loss_ratio": "pct", "margin": "pct", "approval_rate": "pct", "fraud_rate": "pct",
    "fraud_flag_rate": "pct", "precision": "pct", "recall": "pct", "capacity_used": "pct",
    "frequency": "dec2", "claims_per_customer": "dec2", "risk_index": "dec2",
    "avg_processing_days": "dec1", "claims_per_adjudicator": "dec1",
    "amount_vs_avg": "x", "fraud_vs_avg": "x",
    "year": "year", "period": "month",
}

HEADER_NAMES = {
    "product_name": "Product", "cause_of_loss": "Cause of loss", "p90": "P90", "adjudicator_id": "ID",
    "seniority_level": "Seniority", "payment_method": "Payment method", "age_band": "Age band",
    "fraud_vs_avg": "Fraud vs product avg", "amount_vs_avg": "Amount vs product avg",
    "avg_processing_days": "Avg processing days", "flagged": "Flagged fraud",
    "both": "Outlier and flagged", "capacity_used": "Capacity used (open / capacity)",
}

SHEET_NAMES = [
    "Overview", "Claims Performance", "Financial", "Risk & Fraud",
    "Adjudicators", "Customer & Regional", "Notes",
]

CHART_W, CHART_H, CHART_ROWS = 480, 300, 16  # pixels, pixels, grid rows per chart row


# ---------- small helpers ----------


@dataclass
class TableRef:
    """Where a table landed, so charts can point at its columns."""

    sheet: str
    first: int  # first data row (0-indexed)
    last: int
    cols: dict[str, int]  # column name -> sheet column index


@dataclass
class ChartSpec:
    kind: str  # column | bar | line
    title: str
    ref: TableRef | None
    category: str
    series: list[tuple[str, str, str]]  # (column, legend label, colour)
    y_fmt: str = "#,##0"
    y_min: float | None = 0
    y_max: float | None = None
    labels: bool = False  # value labels - only worth it when there are few bars
    date_axis: bool = False
    extra: dict = field(default_factory=dict)


def product_colors(names: list[str]) -> dict[str, str]:
    """
    One colour per product. The original four keep their usual colour, any other
    product takes a free slot, and "Other" is always grey. Never cycles or makes up colours.
    """
    colors = {n: PRODUCT_COLORS[n] for n in names if n in PRODUCT_COLORS}
    free = [c for c in SLOTS if c not in colors.values()]
    for name in names:
        if name == "Other":
            colors[name] = OTHER_GREY
        elif name not in colors:
            colors[name] = free.pop(0) if free else OTHER_GREY
    return colors


def fold_small_products(df: pd.DataFrame, keep: int = MAX_PRODUCT_SERIES) -> pd.DataFrame:
    """
    For multi-series charts: keep the `keep` biggest products by premium and add
    the rest together as "Other" (loss ratio recomputed from the summed amounts).
    Needs product_name, year, premium_total and approved_total columns.
    """
    if df.empty or df["product_name"].nunique() <= keep:
        return df
    top = df.groupby("product_name")["premium_total"].sum().nlargest(keep).index
    folded = df.assign(product_name=df["product_name"].where(df["product_name"].isin(top), "Other"))
    out = folded.groupby(["product_name", "year"], as_index=False)[["premium_total", "approved_total"]].sum()
    out["loss_ratio"] = out["approved_total"] / out["premium_total"].where(out["premium_total"] != 0)
    return out


def _pretty(name: str) -> str:
    return HEADER_NAMES.get(name) or str(name).replace("_", " ").capitalize()


def _kind_for(name: str, series: pd.Series, overrides: dict[str, str]) -> str:
    if name in overrides:
        return overrides[name]
    if name in COLUMN_KINDS:
        return COLUMN_KINDS[name]
    if pd.api.types.is_bool_dtype(series):
        return "text"
    if pd.api.types.is_integer_dtype(series):
        return "int"
    if pd.api.types.is_float_dtype(series):
        return "dec2"
    return "text"


def _clean(df: pd.DataFrame) -> list[list]:
    """Turns a DataFrame into plain rows Excel can take (NaN -> empty cell)."""
    out = df.copy()
    for col in out.columns:
        if isinstance(out[col].dtype, pd.CategoricalDtype):
            out[col] = out[col].astype(str)
    return out.astype(object).where(out.notna(), None).values.tolist()


# ---------- one worksheet ----------


class SheetWriter:
    """Wraps a worksheet with a 'next free row' cursor, column widths and a chart grid."""

    def __init__(self, wb: Workbook, name: str, n_charts: int = 0, title: str = "", subtitle: str = ""):
        self.wb, self.name = wb, name
        self.ws: Worksheet = wb.get_worksheet_by_name(name)
        self.ws.hide_gridlines(2)
        self.widths: dict[int, float] = {0: 18}
        self._formats: dict[str, object] = {}

        if title:
            self.ws.write(0, 0, title, self.fmt("title"))
        if subtitle:
            self.ws.write(1, 0, subtitle, self.fmt("subtitle"))
        # tables start below the area reserved for charts
        self.row = 3 + math.ceil(n_charts / 2) * CHART_ROWS if n_charts else 3

    def fmt(self, key: str):
        """Cached cell formats, built on first use."""
        if key not in self._formats:
            base = {"font_name": "Calibri", "font_color": INK}
            specs = {
                "title": {"bold": True, "font_size": 18},
                "subtitle": {"font_color": INK_SOFT, "font_size": 10},
                "section": {"bold": True, "font_size": 12},
                "note": {"font_color": INK_SOFT, "italic": True},
                "header": {"bold": True, "bottom": 1, "bottom_color": AXIS, "text_wrap": True, "valign": "top"},
                "tile_label": {"font_color": INK_SOFT, "font_size": 10, "bg_color": TILE_BG, "indent": 1},
                "wrap": {"text_wrap": True, "valign": "top"},
                "bold": {"bold": True},
            }
            if key in specs:
                spec = {**base, **specs[key]}
            elif key.startswith("tile_"):  # tile_<kind>: big number in a tile
                spec = {**base, "bold": True, "font_size": 20, "bg_color": TILE_BG, "indent": 1,
                        "align": "left", "num_format": NUM_FORMATS[key[5:]]}
            else:  # a number format kind
                spec = {**base, "num_format": NUM_FORMATS.get(key, "General")}
            self._formats[key] = self.wb.add_format(spec)
        return self._formats[key]

    def widen(self, col: int, chars: float) -> None:
        self.widths[col] = max(self.widths.get(col, 8), min(chars, 34))

    def section(self, text: str) -> None:
        self.ws.write(self.row, 0, text, self.fmt("section"))
        self.row += 1

    def note(self, text: str) -> None:
        self.ws.write(self.row, 0, text, self.fmt("note"))
        self.row += 1

    def table(
        self,
        df: pd.DataFrame,
        title: str,
        kinds: dict[str, str] | None = None,
        scale: tuple[str, ...] = (),
        headers: dict[str, str] | None = None,
    ) -> TableRef | None:
        """
        Writes a titled Excel table at the cursor. `scale` lists columns that get
        a light-to-mid blue colour scale (bigger = darker). Returns None (and
        writes a note) if there's nothing to show.
        """
        self.section(title)
        if df.empty:
            self.note("No data for this selection.")
            self.row += 1
            return None

        kinds, headers = kinds or {}, headers or {}
        columns, cols_idx = [], {}
        for i, name in enumerate(df.columns):
            kind = _kind_for(name, df[name], kinds)
            label = headers.get(name) or _pretty(name)
            columns.append({"header": label, "format": self.fmt(kind), "header_format": self.fmt("header")})
            cols_idx[name] = i
            sample = df[name].astype(str).str.len().max()
            self.widen(i, max(len(label) * 0.9, float(sample) if kind == "text" else 12) + 2)

        first, last = self.row, self.row + len(df)  # header row + data rows
        self.ws.add_table(
            first, 0, last, len(df.columns) - 1,
            {"data": _clean(df), "columns": columns, "style": "Table Style Light 1"},
        )
        for name in scale:
            if name in cols_idx:
                c = cols_idx[name]
                self.ws.conditional_format(first + 1, c, last, c, {
                    "type": "2_color_scale", "min_color": SCALE_LOW, "max_color": SCALE_HIGH,
                })
        self.row = last + 3
        return TableRef(self.name, first + 1, last, cols_idx)

    def heatmap(self, ref: TableRef | None, first_col: int = 1) -> None:
        """Colour scale across a whole block of a pivot table (every value except the label column)."""
        if ref is None:
            return
        last_col = max(ref.cols.values())
        self.ws.conditional_format(ref.first, first_col, ref.last, last_col, {
            "type": "2_color_scale", "min_color": SCALE_LOW, "max_color": SCALE_HIGH,
        })

    def place_charts(self, specs: list[ChartSpec], top: int = 3) -> None:
        """Lays charts out two per row, starting at row `top` (default: the area reserved above the tables)."""
        usable = [s for s in specs if s.ref is not None]
        for i, spec in enumerate(usable):
            self.ws.insert_chart(
                top + (i // 2) * CHART_ROWS, 0, build_chart(self.wb, spec),
                {"x_offset": (i % 2) * (CHART_W + 16), "y_offset": 4},
            )

    def finish(self) -> None:
        for col, width in self.widths.items():
            self.ws.set_column(col, col, width)


# ---------- charts ----------


def build_chart(wb: Workbook, spec: ChartSpec) -> Chart:
    """One native Excel chart in the report's style: quiet grid, thin marks, legend only if 2+ series."""
    ref = spec.ref
    assert ref is not None
    chart = wb.add_chart({"type": spec.kind})
    cat_col = ref.cols[spec.category]
    categories = [ref.sheet, ref.first, cat_col, ref.last, cat_col]

    for column, label, colour in spec.series:
        c = ref.cols[column]
        series: dict = {
            "name": label,
            "categories": categories,
            "values": [ref.sheet, ref.first, c, ref.last, c],
        }
        if spec.kind == "line":
            series["line"] = {"color": colour, "width": 2}
        else:
            series["fill"] = {"color": colour}
            series["border"] = {"none": True}
            series["gap"] = 70
        if spec.labels:
            series["data_labels"] = {
                "value": True, "num_format": spec.y_fmt, "font": {"color": INK_SOFT, "size": 9},
            }
        chart.add_series(series)

    chart.set_title({"name": spec.title, "name_font": {"size": 12, "bold": True, "color": INK}})
    if len(spec.series) > 1:
        chart.set_legend({"position": "bottom", "font": {"color": INK_SOFT, "size": 9}})
    else:
        chart.set_legend({"none": True})  # one series: the title already says what it is

    value_axis = {
        "num_format": spec.y_fmt,
        "num_font": {"color": INK_SOFT, "size": 9},
        "major_gridlines": {"visible": True, "line": {"color": GRID, "width": 0.75}},
        "line": {"none": True},
    }
    if spec.y_min is not None:
        value_axis["min"] = spec.y_min
    if spec.y_max is not None:
        value_axis["max"] = spec.y_max
    category_axis = {"num_font": {"color": INK_SOFT, "size": 9}, "line": {"color": AXIS}}
    if spec.date_axis:
        category_axis.update({
            "date_axis": True, "num_format": "mmm yy", "major_unit": 6, "major_unit_type": "months",
        })
    if spec.kind == "bar":  # horizontal bars: put the first row at the top
        category_axis["reverse"] = True
        chart.set_x_axis(value_axis)
        chart.set_y_axis(category_axis)
    else:
        chart.set_x_axis(category_axis)
        chart.set_y_axis(value_axis)

    chart.set_chartarea({"border": {"none": True}})
    chart.set_plotarea({"border": {"none": True}})
    chart.set_size({"width": CHART_W, "height": CHART_H})
    return chart


def _single(column: str, label: str) -> list[tuple[str, str, str]]:
    return [(column, label, BLUE)]


# ---------- the sheets ----------


def _overview(sw: SheetWriter, frames: Frames, filters: Filters, charts: list[ChartSpec]) -> None:
    kpis = compute_kpis(frames)
    ws = sw.ws
    per_row = 5
    for i, kpi in enumerate(kpis):
        r, c = sw.row + (i // per_row) * 3, i % per_row
        ws.write(r, c, kpi.label, sw.fmt("tile_label"))
        value = None if math.isnan(kpi.value) else kpi.value
        ws.write(r + 1, c, value, sw.fmt(f"tile_{kpi.kind}"))
        ws.set_row(r + 1, 30)
        sw.widen(c, 24)
    sw.row += 3 * math.ceil(len(kpis) / per_row) + 1

    sw.section("Highlights")
    lines = build_highlights(frames)
    for line in lines or ["Nothing to highlight for this selection."]:
        ws.write(sw.row, 0, f"•  {line}")
        sw.row += 1
    sw.row += 1
    sw.place_charts(charts, top=sw.row)


def _claims_sheet(wb: Workbook, frames: Frames) -> TableRef | None:
    sw = SheetWriter(wb, "Claims Performance", n_charts=4, title="Claims performance",
                     subtitle="Frequency, amounts, severity, approvals and monthly trends")
    monthly = frames["monthly"][["period", "claims", "total_claim_amount"]]
    sw.table(frames["frequency"], "Claims frequency by product")
    avg = sw.table(frames["avg_by_product"], "Average claim amount by product")
    sw.table(frames["avg_by_cause"], "Average claim amount by cause of loss")
    sw.table(frames["severity"], "Severity (approved amount per paid claim)")
    appr = sw.table(frames["approval"], "Approval rate by product")
    m = sw.table(monthly, "Monthly trends")
    sw.place_charts([
        ChartSpec("line", "Claims per month", m, "period", _single("claims", "Claims"), date_axis=True),
        ChartSpec("line", "Total claimed per month", m, "period", _single("total_claim_amount", "Total claimed"),
                  y_fmt="$#,##0", date_axis=True),
        ChartSpec("column", "Average claim amount by product", avg, "product_name",
                  _single("avg_claim_amount", "Average claim"), y_fmt="$#,##0", labels=True),
        ChartSpec("column", "Approval rate by product", appr, "product_name",
                  _single("approval_rate", "Approval rate"), y_fmt="0%", y_max=1, labels=True),
    ])
    sw.finish()
    return m


def _financial_sheet(wb: Workbook, frames: Frames) -> TableRef | None:
    sw = SheetWriter(wb, "Financial", n_charts=4, title="Financial",
                     subtitle="Loss ratio, profitability and payouts")
    lr = sw.table(frames["loss_ratio"], "Loss ratio by product", scale=("loss_ratio",))
    prof = sw.table(frames["profitability"], "Product profitability (premium vs money actually paid)")

    year = fold_small_products(frames["loss_ratio_year"])
    year_pivot = (
        year.pivot(index="year", columns="product_name", values="loss_ratio").reset_index()
        if not year.empty else pd.DataFrame()
    )
    products = [c for c in year_pivot.columns if c != "year"]
    if "Other" in products:  # keep the grey "Other" last
        products = [p for p in products if p != "Other"] + ["Other"]
        year_pivot = year_pivot[["year"] + products]
    colors = product_colors(products)
    by_year = sw.table(year_pivot, "Loss ratio by year", kinds={p: "pct" for p in products})
    sw.note("Premium is counted in the year the policy started, claims in the year of loss - partial years look off.")
    sw.row += 1

    payouts = frames["payouts"]
    pay_pivot = pd.DataFrame()
    if not payouts.empty:
        pay = payouts.assign(period=pd.to_datetime(payouts[["year", "month"]].assign(day=1)))
        pay_pivot = pay.pivot_table(index="period", columns="payment_method", values="total_paid",
                                    aggfunc="sum", fill_value=0).reset_index()
    methods = [c for c in pay_pivot.columns if c != "period"]
    pay_ref = sw.table(pay_pivot, "Payouts by month and payment method",
                       kinds={m: "money" for m in methods})

    sw.place_charts([
        ChartSpec("column", "Loss ratio by product", lr, "product_name",
                  _single("loss_ratio", "Loss ratio"), y_fmt="0%", labels=True),
        ChartSpec("column", "Profit by product (premium minus paid)", prof, "product_name",
                  _single("profit", "Profit"), y_fmt="$#,##0", y_min=None, labels=True),
        ChartSpec("line", "Loss ratio by year", by_year, "year",
                  [(p, p, colors[p]) for p in products], y_fmt="0%"),
        ChartSpec("line", "Payouts per month by method", pay_ref, "period",
                  [(m, m, METHOD_COLORS.get(m, BLUE)) for m in methods], y_fmt="$#,##0", date_axis=True),
    ])
    sw.finish()
    return lr


def _risk_sheet(wb: Workbook, frames: Frames) -> None:
    sw = SheetWriter(wb, "Risk & Fraud", n_charts=2, title="Risk and fraud",
                     subtitle="Fraud flags, claim-size outliers and the riskiest product/province combinations")
    fr = sw.table(frames["fraud_rate"], "Fraud flag rate by product")
    an = sw.table(frames["anomalies"], "Outlier detection (IQR) vs adjudicator fraud flags",
                  headers={"outliers": "Outliers found"})
    sw.note("Precision = share of outliers that were flagged. Recall = share of flagged claims the outlier check found.")
    sw.row += 1
    sw.table(frames["high_risk"].head(15), "Top 15 high-risk segments (product x province, 30+ claims)",
             scale=("risk_index",))
    sw.place_charts([
        ChartSpec("column", "Fraud flag rate by product", fr, "product_name",
                  _single("fraud_rate", "Fraud flag rate"), y_fmt="0.0%", labels=True),
        ChartSpec("column", "Outlier check: precision and recall", an, "product_name",
                  [("precision", "Precision", BLUE), ("recall", "Recall", ORANGE)], y_fmt="0%", y_max=1),
    ])
    sw.finish()


def _adjudicator_sheet(wb: Workbook, frames: Frames) -> None:
    sw = SheetWriter(wb, "Adjudicators", n_charts=2, title="Adjudicator performance",
                     subtitle="Workload is counted from real claims, not the static capacity score")
    sen = sw.table(frames["seniority"], "By seniority level")

    perf = frames["adjudicators"]
    if not perf.empty:
        perf = perf.assign(adjudicator=perf["first_name"] + " " + perf["last_name"]).drop(
            columns=["first_name", "last_name", "decided"]
        )
        first = ["adjudicator_id", "adjudicator", "department", "seniority_level", "capacity_score"]
        perf = perf[first + [c for c in perf.columns if c not in first]]
    sw.table(perf, "Every adjudicator", scale=("avg_processing_days", "open_claims"),
             headers={"open_claims": "Open claims", "capacity_score": "Capacity score"})
    sw.place_charts([
        ChartSpec("column", "Average processing days by seniority", sen, "seniority_level",
                  _single("avg_processing_days", "Days"), y_fmt="0.0", labels=True),
        ChartSpec("column", "Claims per adjudicator by seniority", sen, "seniority_level",
                  _single("claims_per_adjudicator", "Claims"), y_fmt="#,##0", labels=True),
    ])
    sw.finish()


def _regional_sheet(wb: Workbook, frames: Frames) -> None:
    sw = SheetWriter(wb, "Customer & Regional", n_charts=2, title="Customers and regions",
                     subtitle="Where claims come from and how loss ratio varies by province and age")
    prov = sw.table(frames["province"], "By province", scale=("loss_ratio",))
    sw.table(frames["city"].head(15), "Top 15 cities by claims")

    seg = frames["segmentation"]
    seg_pivot = pd.DataFrame()
    if not seg.empty:
        seg_pivot = seg.pivot_table(index="age_band", columns="product_name", values="claims_per_customer",
                                    observed=True).reset_index()
        seg_pivot["age_band"] = seg_pivot["age_band"].astype(str)
    products = [c for c in seg_pivot.columns if c != "age_band"]
    seg_ref = sw.table(seg_pivot, "Claims per customer by age band and product",
                       kinds={p: "dec2" for p in products})
    sw.heatmap(seg_ref)

    sw.place_charts([
        ChartSpec("bar", "Claims by province", prov, "province", _single("claims", "Claims")),
        ChartSpec("bar", "Loss ratio by province", prov, "province",
                  _single("loss_ratio", "Loss ratio"), y_fmt="0%"),
    ])
    sw.finish()


NOTES = [
    ("Claims frequency", "Claims divided by policies (all policies, not just active ones)."),
    ("Average claim amount", "Mean claim_amount. Severity uses approved_amount on paid (Approved/Closed) claims only."),
    ("Approval rate", "Approved or Closed claims divided by decided claims. Pending claims are left out."),
    ("Loss ratio", "Approved claim amount divided by premium. Premium is the full policy premium, not pro-rated earned premium."),
    ("Profitability", "Premium minus what was actually paid out (payments table)."),
    ("Outliers", "Claims above Q3 + 1.5 x IQR of their product's claim amounts. Only the high side counts."),
    ("Fraud flag rate", "Share of claims an adjudicator flagged as fraud."),
    ("Processing days", "Decision date minus reported date, decided claims only, averaged per adjudicator."),
    ("Workload", "Claims counted per adjudicator. Open claims = Pending."),
    ("High-risk segments", "Product x province groups compared with their own product's average claim size and fraud rate."),
]

CAVEATS = [
    "Date filters apply to claims only. Premium and policy counts always cover the whole policy book, so "
    "frequency and loss ratio are not comparable for a partial date window.",
    "The data is synthetic. Patterns come from how the generator was written, not from real policyholders.",
]


def _notes_sheet(wb: Workbook, frames: Frames, filters: Filters, generated: datetime) -> None:
    sw = SheetWriter(wb, "Notes", title="Notes and definitions", subtitle=scope_text(filters))
    ws = sw.ws
    sw.row = 3
    sw.section("Metric definitions")
    for name, text in NOTES:
        ws.write(sw.row, 0, name, sw.fmt("bold"))
        ws.write(sw.row, 1, text, sw.fmt("wrap"))
        sw.row += 1
    sw.row += 1
    sw.section("Things to know")
    for text in CAVEATS:
        ws.merge_range(sw.row, 0, sw.row, 1, text, sw.fmt("wrap"))
        ws.set_row(sw.row, 48)
        sw.row += 1
    sw.row += 1
    sw.section("This run")
    ws.write(sw.row, 0, "Generated", sw.fmt("bold"))
    ws.write(sw.row, 1, generated.strftime("%Y-%m-%d %H:%M"))
    sw.row += 1
    ws.write(sw.row, 0, "Claims included", sw.fmt("bold"))
    ws.write(sw.row, 1, int(frames["frequency"]["claims"].sum()), sw.fmt("int"))
    sw.widths = {0: 24, 1: 110}
    sw.finish()


# ---------- putting it together ----------


def write_workbook(frames: Frames, output: Path, filters: Filters | None = None,
                   generated: datetime | None = None) -> Path:
    """Writes the .xlsx from already-collected frames (no database needed here)."""
    filters = filters or Filters()
    generated = generated or datetime.now()
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)

    # data values (product names, provinces, adjudicator names...) can come from anyone's CSV.
    # by default xlsxwriter turns text that starts with "=" into a live formula and text that
    # looks like a URL into a clickable link, so a cell like =HYPERLINK(...) would run when the
    # report is opened. switch both off: every value we write stays plain text.
    wb = xlsxwriter.Workbook(str(output), {
        "nan_inf_to_errors": False,
        "strings_to_formulas": False,
        "strings_to_urls": False,
    })
    wb.set_properties({"title": "Insurance Claims Analytics Report", "comments": "Synthetic data"})
    for name in SHEET_NAMES:  # create tabs in order first, fill them in whatever order is handy
        wb.add_worksheet(name)

    monthly_ref = _claims_sheet(wb, frames)
    loss_ref = _financial_sheet(wb, frames)
    _risk_sheet(wb, frames)
    _adjudicator_sheet(wb, frames)
    _regional_sheet(wb, frames)
    _notes_sheet(wb, frames, filters, generated)

    overview = SheetWriter(wb, "Overview", n_charts=2, title="Insurance claims report",
                           subtitle=f"{scope_text(filters)}  |  generated {generated:%Y-%m-%d}")
    # charts here reuse tables that live on the other sheets
    overview.row = 3
    _overview(overview, frames, filters, [
        ChartSpec("line", "Claims per month", monthly_ref, "period", _single("claims", "Claims"), date_axis=True),
        ChartSpec("column", "Loss ratio by product", loss_ref, "product_name",
                  _single("loss_ratio", "Loss ratio"), y_fmt="0%", labels=True),
    ])
    overview.finish()

    wb.close()
    return output


def build_report(engine: Engine, output: Path, filters: Filters | None = None) -> Path:
    """Queries the analytics layer and writes the workbook."""
    filters = filters or Filters()
    return write_workbook(collect_frames(engine, filters), output, filters)


def main(argv: list[str] | None = None, engine: Engine | None = None) -> None:
    parser = argparse.ArgumentParser(description="Build the claims Excel report")
    parser.add_argument("--date-from", type=date.fromisoformat, help="only claims dated on/after this (YYYY-MM-DD)")
    parser.add_argument("--date-to", type=date.fromisoformat, help="only claims dated on/before this (YYYY-MM-DD)")
    parser.add_argument("--product", help="only this product (any name from the products table)")
    parser.add_argument("--output", type=Path, default=Path("summary_reports/claims_report.xlsx"))
    args = parser.parse_args(argv)

    if args.date_from and args.date_to and args.date_from > args.date_to:
        parser.error("--date-from can't be after --date-to")

    from app.db import DatabaseNotReady, check_database_ready, get_engine  # here so importing this module needs no db config

    engine = engine or get_engine()
    try:
        check_database_ready(engine)
    except DatabaseNotReady as exc:
        raise SystemExit(f"error: {exc}") from None  # one clear message, no traceback

    if args.product:
        known = product_names(engine)
        if args.product not in known:
            parser.error(f"unknown product '{args.product}' - products in the database: {', '.join(known)}")

    path = build_report(engine, args.output, Filters(args.date_from, args.date_to, args.product))
    print(f"report written to {path}")


if __name__ == "__main__":
    main()

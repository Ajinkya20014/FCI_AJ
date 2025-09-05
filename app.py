import streamlit as st
import pandas as pd
import plotly.express as px
from io import BytesIO
import math
import matplotlib.pyplot as plt
from matplotlib.backends.backend_pdf import PdfPages

# ─────────────────────────────────────────────────────────
# 1) Page config
# ─────────────────────────────────────────────────────────
st.set_page_config(page_title="Grain Distribution Dashboard", layout="wide")
st.title("🚛 Grain Distribution Dashboard")

# ─────────────────────────────────────────────────────────
# 2) Helpers
# ─────────────────────────────────────────────────────────
def to_excel(df: pd.DataFrame) -> bytes:
    buf = BytesIO()
    df.to_excel(buf, index=False)
    return buf.getvalue()

def _num(s, kind="int"):
    x = pd.to_numeric(s, errors="coerce")
    if kind == "int":
        return x.round().astype("Int64")  # nullable integer
    return x.astype(float)

def _read_sheet_any(io, names):
    """Try multiple possible sheet names and return first match."""
    for nm in names:
        try:
            return pd.read_excel(io, sheet_name=nm)
        except Exception:
            continue
    raise ValueError(f"None of the sheets {names} found in workbook.")

# ─────────────────────────────────────────────────────────
# 3) File loader (upload or fallback)
# ─────────────────────────────────────────────────────────
uploaded = st.file_uploader("Upload simulation output (.xlsx)", type="xlsx")
if uploaded is not None:
    workbook = uploaded
else:
    # fallback to an old local template filename if you still use it
    DEFAULT_FILE = "distribution_dashboard_template.xlsx"
    try:
        open(DEFAULT_FILE, "rb").close()
        workbook = DEFAULT_FILE
        st.caption(f"Using local file: {DEFAULT_FILE}")
    except Exception:
        st.warning("Please upload the simulation output workbook (.xlsx) to continue.")
        st.stop()

# ─────────────────────────────────────────────────────────
# 4) Load & normalize data
# ─────────────────────────────────────────────────────────
@st.cache_data
def load_data(io):
    # Settings (must exist)
    settings = _read_sheet_any(io, ["Settings"])

    # Allow either "..._Dispatch" or short names used by simulator
    dispatch_cg = _read_sheet_any(io, ["CG_to_LG_Dispatch", "CG_to_LG"])
    dispatch_lg = _read_sheet_any(io, ["LG_to_FPS_Dispatch", "LG_to_FPS"])

    stock_levels = _read_sheet_any(io, ["Stock_Levels"])
    lgs          = _read_sheet_any(io, ["LGs"])
    fps          = _read_sheet_any(io, ["FPS"])

    # Normalize columns: rename Dispatch_Day->Day if needed
    if "Dispatch_Day" in dispatch_cg.columns and "Day" not in dispatch_cg.columns:
        dispatch_cg = dispatch_cg.rename(columns={"Dispatch_Day": "Day"})

    # Normalize number types to avoid NaN/float issues
    for df, cols in [
        (dispatch_cg, ["Day", "Vehicle_ID", "LG_ID", "Quantity_tons"]),
        (dispatch_lg, ["Day", "Vehicle_ID", "LG_ID", "FPS_ID", "Quantity_tons"]),
        (stock_levels, ["Day", "Entity_ID", "Stock_Level_tons"]),
        (lgs, ["LG_ID", "Storage_Capacity_tons"]),
        (fps, ["FPS_ID", "Reorder_Threshold_tons"]),
    ]:
        for c in cols:
            if c in df.columns:
                if c in ("Quantity_tons", "Stock_Level_tons", "Reorder_Threshold_tons", "Storage_Capacity_tons"):
                    df[c] = _num(df[c], "float")
                elif c == "Day":
                    df[c] = _num(df[c], "int")
                else:
                    df[c] = _num(df[c], "int")

    # Some exports name LG stocks as Entity_Type == 'LG'
    # Ensure we have both LG & FPS rows
    if "Entity_Type" not in stock_levels.columns:
        raise ValueError("Stock_Levels must contain 'Entity_Type' (LG/FPS).")

    return settings, dispatch_cg, dispatch_lg, stock_levels, lgs, fps

settings, dispatch_cg, dispatch_lg, stock_levels, lgs, fps = load_data(workbook)

# ─────────────────────────────────────────────────────────
# 5) Core parameters
# ─────────────────────────────────────────────────────────
def _get_setting(param, cast=float, default=None):
    try:
        v = settings.loc[settings["Parameter"] == param, "Value"].iloc[0]
        return cast(v)
    except Exception:
        if default is None:
            raise ValueError(f"Missing required setting: {param}")
        return cast(default)

DAYS       = _get_setting("Distribution_Days", int)
TRUCK_CAP  = _get_setting("Vehicle_Capacity_tons", float)
VEH_TOTAL  = _get_setting("Vehicles_Total", int)
MAX_TRIPS_PER_V = _get_setting("Max_Trips_Per_Vehicle_Per_Day", int)
TOTAL_TRIPS_PER_DAY = VEH_TOTAL * MAX_TRIPS_PER_V

# Pre-dispatch offset X for negative slider (based on CG plan if provided)
# (We compute cumulative CG need vs. daily capacity)
if not dispatch_cg.empty and "Quantity_tons" in dispatch_cg.columns:
    daily_total_cg = dispatch_cg.groupby("Day")["Quantity_tons"].sum()
else:
    # fallback: empty series
    daily_total_cg = pd.Series(dtype=float)

cum_need = 0.0
adv = []
for d in range(1, DAYS + 1):
    need = float(daily_total_cg.get(d, 0.0))
    cum_need += need
    over = (cum_need - TOTAL_TRIPS_PER_DAY * TRUCK_CAP * d) / (TOTAL_TRIPS_PER_DAY * TRUCK_CAP if TOTAL_TRIPS_PER_DAY else 1)
    adv.append(math.ceil(over) if over > 0 else 0)
X = max(adv) if adv else 0
MIN_DAY = 1 - X
MAX_DAY = DAYS

# ─────────────────────────────────────────────────────────
# 6) Aggregations
# ─────────────────────────────────────────────────────────
day_totals_cg = (
    dispatch_cg.groupby("Day", as_index=False)["Quantity_tons"].sum()
    .rename(columns={"Quantity_tons": "CG_to_LG_tons"})
)
day_totals_lg = (
    dispatch_lg.groupby("Day", as_index=False)["Quantity_tons"].sum()
    .rename(columns={"Quantity_tons": "LG_to_FPS_tons"})
)

# Trips per day = number of rows (each row = one trip)
veh_usage = (
    dispatch_lg.groupby("Day", as_index=False)
    .size()
    .rename(columns={"size": "Trips_Used"})
)
veh_usage["Max_Trips"] = TOTAL_TRIPS_PER_DAY

# LG stock pivot
lg_stock = (
    stock_levels[stock_levels["Entity_Type"] == "LG"]
    .pivot(index="Day", columns="Entity_ID", values="Stock_Level_tons")
    .sort_index()
    .ffill()
)

# FPS stock & risk
fps_stock = stock_levels[stock_levels["Entity_Type"] == "FPS"].copy()
# Attach threshold if present
if "Reorder_Threshold_tons" not in fps_stock.columns and "Reorder_Threshold_tons" in fps.columns:
    fps_stock = fps_stock.merge(
        fps[["FPS_ID", "Reorder_Threshold_tons"]],
        left_on="Entity_ID",
        right_on="FPS_ID",
        how="left"
    )
if "Reorder_Threshold_tons" in fps_stock.columns:
    fps_stock["At_Risk"] = fps_stock["Stock_Level_tons"] <= fps_stock["Reorder_Threshold_tons"]
else:
    fps_stock["At_Risk"] = False  # no threshold supplied

total_plan = day_totals_lg["LG_to_FPS_tons"].sum()

# ─────────────────────────────────────────────────────────
# 7) Sidebar filters & quick KPIs
# ─────────────────────────────────────────────────────────
with st.sidebar:
    st.header("Filters")
    day_range = st.slider(
        "Dispatch Window (days)",
        min_value=int(MIN_DAY),
        max_value=int(MAX_DAY),
        value=(int(MIN_DAY), int(MAX_DAY)),
        format="%d",
    )

    st.subheader("Select LGs")
    cols_sel = st.columns(4)
    selected_lgs = []
    lg_cols = list(lg_stock.columns) if not lg_stock.empty else []
    for i, lg_id in enumerate(lg_cols):
        if cols_sel[i % 4].checkbox(str(int(lg_id)), True, key=f"lg_{int(lg_id)}"):
            selected_lgs.append(lg_id)

    st.markdown("---")
    st.header("Quick KPIs")
    cg_sel = day_totals_cg.query("Day>=@day_range[0] & Day<=@day_range[1]")["CG_to_LG_tons"].sum()
    lg_sel = day_totals_lg.query("Day>=1 & Day<=@day_range[1]")["LG_to_FPS_tons"].sum()
    st.metric("CG→LG Total (t)", f"{cg_sel:,.1f}")
    st.metric("LG→FPS Total (t)", f"{lg_sel:,.1f}")
    st.metric("Max Trips/Day", f"{TOTAL_TRIPS_PER_DAY}")
    st.metric("Truck Capacity (t)", f"{TRUCK_CAP}")

# Common end_day for some views
end_day = min(day_range[1], DAYS)

# ─────────────────────────────────────────────────────────
# 8) Tabs
# ─────────────────────────────────────────────────────────
tab1, tab2, tab3, tab4, tab5, tab6, tab7 = st.tabs([
    "CG→LG Overview", "LG→FPS Overview",
    "FPS Report", "FPS At-Risk",
    "FPS Data", "Downloads", "Metrics"
])

# Tab1: CG→LG overview
with tab1:
    st.subheader("CG → LG Dispatch")
    df1 = day_totals_cg.query("Day>=@day_range[0] & Day<=@day_range[1]").rename(columns={"CG_to_LG_tons": "Quantity_tons"})
    fig1 = px.bar(df1, x="Day", y="Quantity_tons", text="Quantity_tons")
    fig1.update_traces(texttemplate="%{text:.1f}t", textposition="outside")
    st.plotly_chart(fig1, use_container_width=True)

# Tab2: LG→FPS overview
with tab2:
    st.subheader("LG → FPS Dispatch")
    df2 = day_totals_lg.query("Day>=1 & Day<=@day_range[1]").rename(columns={"LG_to_FPS_tons": "Quantity_tons"})
    fig2 = px.bar(df2, x="Day", y="Quantity_tons", text="Quantity_tons")
    fig2.update_traces(texttemplate="%{text:.1f}t", textposition="outside")
    st.plotly_chart(fig2, use_container_width=True)

# Tab3: FPS report
with tab3:
    st.subheader("FPS-wise Dispatch Details")
    fps_df = dispatch_lg.query("Day>=1 & Day<=@day_range[1]").copy()

    # Clean IDs to avoid 'nan'
    for c in ("Vehicle_ID", "FPS_ID", "LG_ID"):
        if c in fps_df.columns:
            fps_df[c] = pd.to_numeric(fps_df[c], errors="coerce").round().astype("Int64")

    report = (
        fps_df.groupby("FPS_ID", as_index=False)
        .agg(
            Total_Dispatched_tons=("Quantity_tons", "sum"),
            Trips_Count=("Vehicle_ID", "size"),  # each row is a trip
            Vehicle_IDs=("Vehicle_ID", lambda s: ",".join(map(str, sorted(s.dropna().astype("Int64").unique()))))
        )
        .merge(fps[["FPS_ID", "FPS_Name"]], on="FPS_ID", how="left")
        .sort_values("Total_Dispatched_tons", ascending=False)
    )
    st.dataframe(report, use_container_width=True)

# Tab4: FPS At-Risk
with tab4:
    st.subheader("FPS At-Risk List")
    arf = fps_stock.query("Day>=1 & Day<=@day_range[1] & At_Risk == True")[
        ["Day", "Entity_ID", "Stock_Level_tons"] + (["Reorder_Threshold_tons"] if "Reorder_Threshold_tons" in fps_stock.columns else [])
    ].rename(columns={"Entity_ID": "FPS_ID"})
    st.dataframe(arf, use_container_width=True)
    st.download_button(
        "Download At-Risk (Excel)",
        to_excel(arf),
        "fps_at_risk.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )

# Tab5: FPS Data
with tab5:
    st.subheader("FPS Stock & Upcoming Receipts")
    fps_data = []
    for fps_id in fps["FPS_ID"]:
        s = fps_stock[(fps_stock["Entity_ID"] == fps_id) & (fps_stock["Day"] == end_day)]["Stock_Level_tons"]
        stock_now = float(s.iloc[0]) if not s.empty else 0.0
        future = dispatch_lg[(dispatch_lg["FPS_ID"] == fps_id) & (dispatch_lg["Day"] > end_day)]["Day"]
        next_day = int(future.min()) if not future.empty else None
        days_to = (next_day - end_day) if next_day else None
        name = fps.set_index("FPS_ID").loc[int(fps_id), "FPS_Name"] if "FPS_Name" in fps.columns else None
        fps_data.append({
            "FPS_ID": int(fps_id),
            "FPS_Name": name,
            "Current_Stock_tons": stock_now,
            "Next_Receipt_Day": next_day,
            "Days_To_Receipt": days_to
        })
    fps_data_df = pd.DataFrame(fps_data)
    st.dataframe(fps_data_df, use_container_width=True)
    st.download_button(
        "Download FPS Data (Excel)",
        to_excel(fps_data_df),
        "fps_data.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )

# Tab6: Downloads
with tab6:
    st.subheader("Download FPS Report")
    st.download_button(
        "Excel",
        to_excel(report),
        f"FPS_Report_{1}_to_{day_range[1]}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    pdf_buf = BytesIO()
    with PdfPages(pdf_buf) as pdf:
        fig, ax = plt.subplots(figsize=(8, max(1, len(report)*0.3) + 1))
        ax.axis('off')
        tbl = ax.table(cellText=report.values, colLabels=report.columns, loc='center')
        tbl.auto_set_font_size(False)
        tbl.set_fontsize(10)
        pdf.savefig(fig, bbox_inches='tight')
    st.download_button(
        "PDF",
        pdf_buf.getvalue(),
        f"FPS_Report_{1}_to_{day_range[1]}.pdf",
        mime="application/pdf"
    )

# Tab7: Metrics
with tab7:
    st.subheader("Key Performance Indicators")

    # Average trips/day (rounded) within the selected window
    avg_trips = veh_usage.query("Day>=1 & Day<=@day_range[1]")["Trips_Used"].mean()
    avg_trips = round(float(avg_trips) if pd.notna(avg_trips) else 0.0, 1)

    # Utilization = avg trips used / total trips available per day
    pct_fleet = (avg_trips / TOTAL_TRIPS_PER_DAY) * 100 if TOTAL_TRIPS_PER_DAY else 0.0

    # Stocks and capacities
    if not lg_stock.empty and selected_lgs:
        try:
            lg_onhand = lg_stock.loc[end_day, selected_lgs].sum()
        except KeyError:
            # if end_day not in index (unlikely), fallback to max available
            lg_onhand = lg_stock.iloc[-1][selected_lgs].sum()
    else:
        lg_onhand = 0.0

    if "Entity_Type" in fps_stock.columns:
        fps_onhand = fps_stock.query("Day==@end_day")["Stock_Level_tons"].sum()
    else:
        fps_onhand = 0.0

    if "Storage_Capacity_tons" in lgs.columns and selected_lgs:
        lg_caps = lgs.set_index("LG_ID").loc[pd.Index(selected_lgs).astype(int), "Storage_Capacity_tons"].sum()
    else:
        lg_caps = 0.0
    pct_lg_filled = (lg_onhand / lg_caps * 100) if lg_caps else 0.0

    # Risk counts
    fps_zero = fps_stock.query("Day==@end_day & Stock_Level_tons==0")["Entity_ID"].nunique() if not fps_stock.empty else 0
    fps_risk = fps_stock.query("Day==@end_day & At_Risk==True")["Entity_ID"].nunique() if not fps_stock.empty else 0

    # Plan progress
    dispatched_cum = day_totals_lg.query("Day<=@end_day")["LG_to_FPS_tons"].sum()
    pct_plan = (dispatched_cum / total_plan * 100) if total_plan else 0.0
    remaining_t = total_plan - dispatched_cum
    days_rem = math.ceil(remaining_t / (TOTAL_TRIPS_PER_DAY * TRUCK_CAP)) if TOTAL_TRIPS_PER_DAY and TRUCK_CAP else None

    metrics = [
        ("Total CG→LG (t)",       f"{day_totals_cg['CG_to_LG_tons'].sum():,.1f}"),
        ("Total LG→FPS (t)",      f"{day_totals_lg['LG_to_FPS_tons'].sum():,.1f}"),
        ("Avg Trips/Day",         f"{avg_trips:,.1f}"),
        ("% Fleet Utilization",   f"{pct_fleet:.1f}%"),
        ("LG Stock on Hand (t)",  f"{lg_onhand:,.1f}"),
        ("FPS Stock on Hand (t)", f"{fps_onhand:,.1f}"),
        ("% LG Cap Filled",       f"{pct_lg_filled:.1f}%"),
        ("FPS Stock-Outs",        f"{fps_zero}"),
        ("FPS At-Risk Count",     f"{fps_risk}"),
        ("% Plan Completed",      f"{pct_plan:.1f}%"),
        ("Days Remaining",        f"{days_rem if days_rem is not None else '—'}")
    ]
    cols = st.columns(3)
    for i, (label, val) in enumerate(metrics):
        cols[i % 3].metric(label, val)

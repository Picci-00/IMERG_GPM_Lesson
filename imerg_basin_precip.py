#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
imerg_basin_precip.py
=====================

Operational (non-interactive) version of IMERG_notebook.ipynb.

Workflow
--------
1. Check the local IMERG files in nc/<temporal_resolution>/.
2. If some timesteps are missing: read the NASA Earthdata credentials from
   the .netrc file next to this script, log in and download the missing data.
3. Crop the IMERG grid to the area of interest (BBOX).
4. Convert precipitation to depth per timestep (mm).
5. Load the basin shapefile from shapefile/ and compute the basin-averaged
   precipitation with the three methods (all_touched=True / False,
   area-weighted); the user chooses which one to keep.
6. Save the selected time series to an Excel file and a PNG plot in output/.

Folder structure (same as the notebook, relative to this script)
----------------------------------------------------------------
    project_folder/
    ├── imerg_basin_precip.py
    ├── .netrc          <- NASA Earthdata credentials
    ├── shapefile/      <- ONE basin shapefile (.shp, .shx, .dbf, .prj, ...)
    ├── nc/             <- IMERG files, one subfolder per resolution (auto)
    └── output/         <- Excel files and figures (auto)

Usage
-----
Edit the SETTINGS block below and run:

    python imerg_basin_precip.py

or override the settings from the command line, e.g.:

    python imerg_basin_precip.py --scale Monthly --start 2020-01-01 --end 2023-12-31
    python imerg_basin_precip.py --scale 30min --start 2023-06-10 --end 2023-06-12 --method weighted
    python imerg_basin_precip.py --bbox 12.0 42.5 13.0 43.5 --show

Run `python imerg_basin_precip.py --help` for all options.
"""

import argparse
import datetime as dt
import glob
import netrc
import os
import re
import sys
import warnings
from pathlib import Path

import numpy as np
import pandas as pd
import xarray as xr
import netCDF4
import geopandas as gpd
from shapely.geometry import box
from rasterio import features
from rasterio.transform import from_origin
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

warnings.filterwarnings("ignore")


# ============================================================
# SETTINGS (default values, can be overridden from the command line)
# ============================================================

# Temporal resolution: "30 min", "Daily" or "Monthly"
TIME_SCALE = "Daily"

# Analysis period (YYYY-MM-DD, both dates included)
START_DATE = "2023-06-01"
END_DATE = "2023-06-15"

# Area of interest: (lon_min, lat_min, lon_max, lat_max) in degrees (WGS84).
# If None, the area is taken from the shapefile extent plus BBOX_BUFFER.
BBOX = None
BBOX_BUFFER = 0.2  # degrees (= 2 IMERG cells) added around the basin

# Basin-averaging method: "Area-weighted", "all_touched=True",
# "all_touched=False". If None, the script asks which one to use.
METHOD = None

# Show the final plot on screen (it is always saved in output/)
SHOW_PLOT = False


# ============================================================
# Folders and constants (same folders as the notebook)
# ============================================================

BASE_DIR = Path(__file__).resolve().parent
NC_DIR = BASE_DIR / "nc"
OUT_DIR = BASE_DIR / "output"
SHP_DIR = BASE_DIR / "shapefile"
NETRC_PATH = BASE_DIR / ".netrc"

# Machine name used in the .netrc file
NETRC_HOST = "urs.earthdata.gov"

IMERG_PRODUCTS = {
    "30 min": {"short_name": "GPM_3IMERGHH", "version": "07"},
    "Daily": {"short_name": "GPM_3IMERGDF", "version": "07"},
    "Monthly": {"short_name": "GPM_3IMERGM", "version": "07"},
}

METHODS = ("Area-weighted", "all_touched=True", "all_touched=False")


# ============================================================
# Input normalization (accept short names from the command line)
# ============================================================

def normalize_time_scale(value):
    aliases = {
        "30 min": "30 min", "30min": "30 min", "30_min": "30 min",
        "30": "30 min", "hh": "30 min", "halfhourly": "30 min",
        "daily": "Daily", "day": "Daily", "d": "Daily",
        "monthly": "Monthly", "month": "Monthly", "m": "Monthly",
    }
    key = str(value).strip().lower()
    if key not in aliases:
        raise ValueError(
            f"Unknown temporal resolution '{value}'. "
            "Use one of: 30min, Daily, Monthly."
        )
    return aliases[key]


def normalize_method(value):
    if value is None:
        return None
    aliases = {
        "area-weighted": "Area-weighted", "area_weighted": "Area-weighted",
        "weighted": "Area-weighted", "aw": "Area-weighted", "w": "Area-weighted",
        "all_touched=true": "all_touched=True", "true": "all_touched=True",
        "touched_true": "all_touched=True", "t": "all_touched=True",
        "all_touched=false": "all_touched=False", "false": "all_touched=False",
        "touched_false": "all_touched=False", "f": "all_touched=False",
    }
    key = str(value).strip().lower()
    if key not in aliases:
        raise ValueError(
            f"Unknown method '{value}'. "
            "Use one of: weighted, true, false."
        )
    return aliases[key]


def parse_date(value):
    return dt.date.fromisoformat(str(value))


# ============================================================
# 1. IMERG file management
# ============================================================

def list_imerg_files(folder):
    """Return all IMERG HDF5 / NetCDF files available in a folder."""
    files = []
    for pattern in ("*.HDF5", "*.hdf5", "*.nc4", "*.nc"):
        files.extend(glob.glob(os.path.join(folder, pattern)))
    return sorted(set(files))


def get_file_timestamp(file_path):
    """Acquisition start time read from the IMERG filename."""
    name = os.path.basename(file_path)
    match = re.search(r"(\d{8})-S(\d{6})-E", name)
    if match is None:
        raise ValueError(f"Cannot read the date from IMERG filename:\n{name}")
    return pd.to_datetime(match.group(1) + match.group(2), format="%Y%m%d%H%M%S")


def expected_time_keys(start_date, end_date, time_scale):
    """All timesteps expected for the selected period."""
    start, end = pd.Timestamp(start_date), pd.Timestamp(end_date)
    if time_scale == "30 min":
        return set(pd.date_range(
            start, end + pd.Timedelta(days=1) - pd.Timedelta(minutes=30), freq="30min"
        ))
    if time_scale == "Daily":
        return set(pd.date_range(start, end, freq="D"))
    if time_scale == "Monthly":
        return set(pd.period_range(start, end, freq="M"))
    raise ValueError(f"Unsupported temporal resolution: {time_scale}")


def file_time_key(file_path, time_scale):
    """Timestep represented by an IMERG file."""
    ts = get_file_timestamp(file_path)
    if time_scale == "30 min":
        return ts.floor("30min")
    if time_scale == "Daily":
        return ts.normalize()
    return ts.to_period("M")


def select_files_for_period(file_list, start_date, end_date, time_scale):
    expected = expected_time_keys(start_date, end_date, time_scale)
    return sorted(f for f in file_list if file_time_key(f, time_scale) in expected)


def missing_timesteps(file_list, start_date, end_date, time_scale):
    expected = expected_time_keys(start_date, end_date, time_scale)
    available = {file_time_key(f, time_scale) for f in file_list}
    return sorted(expected - available)


def get_imerg_group(path):
    """'Grid' for the 30-min and monthly products, None (root) for daily."""
    with netCDF4.Dataset(path) as ds:
        return "Grid" if "Grid" in ds.groups else None


def get_imerg_extent(path):
    """Outer boundaries (lon_min, lat_min, lon_max, lat_max) of an IMERG file."""
    with netCDF4.Dataset(path) as ds:
        grp = ds.groups["Grid"] if "Grid" in ds.groups else ds
        lons = np.asarray(grp.variables["lon"][:])
        lats = np.asarray(grp.variables["lat"][:])
    dlon = np.abs(np.median(np.diff(lons))) if len(lons) > 1 else 0.1
    dlat = np.abs(np.median(np.diff(lats))) if len(lats) > 1 else 0.1
    return (lons.min() - dlon / 2, lats.min() - dlat / 2,
            lons.max() + dlon / 2, lats.max() + dlat / 2)


def bbox_inside(inner, outer):
    return (inner[0] >= outer[0] and inner[1] >= outer[1]
            and inner[2] <= outer[2] and inner[3] <= outer[3])


# ============================================================
# 2. NASA Earthdata login and download
# ============================================================

def earthdata_login():
    """Read the credentials from .netrc (script folder) and log in."""
    import earthaccess  # imported here: needed only when downloading

    if not NETRC_PATH.is_file():
        sys.exit(
            f"ERROR: .netrc file not found at {NETRC_PATH}\n"
            "Create it (see the setup guide) before downloading data."
        )

    auth_info = netrc.netrc(str(NETRC_PATH)).authenticators(NETRC_HOST)
    if auth_info is None:
        sys.exit(
            f"ERROR: no 'machine {NETRC_HOST}' entry found in {NETRC_PATH}."
        )

    username, _, password = auth_info
    os.environ["EARTHDATA_USERNAME"] = username
    os.environ["EARTHDATA_PASSWORD"] = password

    auth = earthaccess.login(strategy="environment")
    if not auth.authenticated:
        sys.exit("ERROR: NASA Earthdata login failed. Check your credentials.")
    print("NASA Earthdata login successful.")
    return earthaccess


def get_imerg_files(time_scale, start_date, end_date, bbox):
    """Use local files if complete, otherwise download what is missing."""
    scale_dir = NC_DIR / time_scale.replace(" ", "_")
    scale_dir.mkdir(parents=True, exist_ok=True)

    local_files = list_imerg_files(scale_dir)
    missing = missing_timesteps(local_files, start_date, end_date, time_scale)
    print(f"Local IMERG files in {scale_dir.relative_to(BASE_DIR)}: {len(local_files)}")

    if missing:
        print(f"Missing timesteps for the selected period: {len(missing)}")
        earthaccess = earthdata_login()

        product = IMERG_PRODUCTS[time_scale]
        results = earthaccess.search_data(
            short_name=product["short_name"],
            version=product["version"],
            temporal=(f"{start_date.isoformat()}T00:00:00",
                      f"{end_date.isoformat()}T23:59:59"),
            bounding_box=tuple(bbox),
        )
        print(f"IMERG granules found on NASA Earthdata: {len(results)}")
        if not results:
            sys.exit("ERROR: no IMERG data found for the selected period and area.")

        earthaccess.download(results, str(scale_dir))
        local_files = list_imerg_files(scale_dir)
    else:
        print("Local files cover the whole period: no download needed.")

    imerg_files = select_files_for_period(local_files, start_date, end_date, time_scale)
    still_missing = missing_timesteps(imerg_files, start_date, end_date, time_scale)
    if still_missing:
        sys.exit(
            f"ERROR: {len(still_missing)} timestep(s) still missing, "
            f"e.g. {still_missing[:3]}"
        )

    if not bbox_inside(bbox, get_imerg_extent(imerg_files[0])):
        sys.exit("ERROR: the IMERG files do not cover the selected area.")

    print(f"Files used for the analysis: {len(imerg_files)} (temporal and spatial coverage OK)")
    return imerg_files


# ============================================================
# 3. Loading, cropping and unit conversion
# ============================================================

def load_imerg_stack(file_list, bbox):
    """
    Load the IMERG files, crop them to the bbox and stack them along time.
    The time coordinate is taken from the filename, so the result is the
    same for the 30-min, daily and monthly products.
    """
    lon_min, lat_min, lon_max, lat_max = bbox
    arrays, units = [], None

    for i, path in enumerate(file_list, 1):
        group = get_imerg_group(path)
        with xr.open_dataset(path, group=group, engine="netcdf4",
                             decode_times=False) as ds:
            if "precipitation" not in ds.variables:
                raise KeyError(f"'precipitation' variable not found in:\n{path}")
            da = ds["precipitation"]
            if units is None:
                units = da.attrs.get("units", "")

            # Each IMERG file holds a single timestep
            if "time" in da.dims:
                da = da.isel(time=0, drop=True)
            da = da.drop_vars("time", errors="ignore")

            da = da.transpose("lat", "lon").sel(
                lon=slice(lon_min, lon_max), lat=slice(lat_min, lat_max)
            ).load()

        arrays.append(da.expand_dims(time=[get_file_timestamp(path)]))
        if i % 200 == 0 or i == len(file_list):
            print(f"  loaded {i}/{len(file_list)} files")

    precip = xr.concat(arrays, dim="time").sortby("time")

    if precip.sizes["lat"] == 0 or precip.sizes["lon"] == 0:
        sys.exit("ERROR: the selected area contains no IMERG grid cell. Enlarge BBOX.")

    # Negative values = missing / invalid data
    precip = precip.where(precip >= 0)
    precip.attrs["units"] = units
    return precip


def convert_to_precipitation_depth(precip, time_scale):
    """Convert IMERG precipitation to depth per timestep (mm)."""
    units = precip.attrs.get("units", "").strip().lower()
    rate_units = ("mm/hr", "mm h-1", "mm/hour")

    if time_scale == "30 min":
        if units not in rate_units:
            raise ValueError(f"Unexpected units for 30-min data: '{units}' (expected mm/hr)")
        out = precip * 0.5                                   # mm/hr x 0.5 hr
    elif time_scale == "Daily":
        if units not in ("mm/day", "mm d-1"):
            raise ValueError(f"Unexpected units for daily data: '{units}' (expected mm/day)")
        out = precip.copy()                                  # already mm/day
    elif time_scale == "Monthly":
        if units not in rate_units:
            raise ValueError(f"Unexpected units for monthly data: '{units}' (expected mm/hr)")
        out = precip * 24 * precip["time"].dt.days_in_month  # mm/hr -> mm/month
    else:
        raise ValueError(f"Unsupported temporal resolution: '{time_scale}'")

    out.attrs = {"units": "mm", "long_name": "Precipitation depth per timestep"}
    return out


# ============================================================
# 4. Basin shapefile
# ============================================================

def load_basin():
    """Load and validate the single basin shapefile in shapefile/."""
    shp_files = glob.glob(str(SHP_DIR / "*.shp"))
    if len(shp_files) != 1:
        sys.exit(
            f"ERROR: found {len(shp_files)} .shp files in '{SHP_DIR.name}/'. "
            "The folder must contain exactly one basin shapefile."
        )

    basin = gpd.read_file(shp_files[0])
    if basin.crs is None:
        sys.exit("ERROR: the shapefile has no CRS (missing .prj file?).")
    if len(basin) != 1:
        sys.exit(
            f"ERROR: the shapefile contains {len(basin)} features; exactly one is "
            "required (use a dissolve in GIS software if the basin is split)."
        )

    basin = basin.to_crs("EPSG:4326")
    geom = basin.geometry.iloc[0]
    if geom is None or geom.is_empty or geom.geom_type not in ("Polygon", "MultiPolygon"):
        sys.exit("ERROR: the basin geometry must be a non-empty Polygon or MultiPolygon.")

    area_km2 = basin.to_crs(basin.estimate_utm_crs()).geometry.iloc[0].area / 1e6
    print(f"Basin shapefile: {os.path.basename(shp_files[0])} ({area_km2:.2f} km²)")
    return basin, area_km2


def bbox_from_basin(basin, buffer):
    lon_min, lat_min, lon_max, lat_max = basin.total_bounds
    return (round(float(lon_min) - buffer, 2), round(float(lat_min) - buffer, 2),
            round(float(lon_max) + buffer, 2), round(float(lat_max) + buffer, 2))


# ============================================================
# 5. Basin-averaged precipitation (three methods)
# ============================================================

def grid_cells_gdf(data2d, default_res=0.1):
    """Rebuild the IMERG grid-cell polygons from the cell centres."""
    lons, lats = data2d["lon"].values, data2d["lat"].values
    dlon = np.abs(np.median(np.diff(lons))) if len(lons) > 1 else default_res
    dlat = np.abs(np.median(np.diff(lats))) if len(lats) > 1 else default_res
    lon2d, lat2d = np.meshgrid(lons, lats)
    polys = [box(x - dlon / 2, y - dlat / 2, x + dlon / 2, y + dlat / 2)
             for x, y in zip(lon2d.ravel(), lat2d.ravel())]
    return gpd.GeoDataFrame({"lon": lon2d.ravel(), "lat": lat2d.ravel()},
                            geometry=polys, crs="EPSG:4326")


def rasterize_basin_mask(data2d, basin_geometry, all_touched):
    """Boolean mask of the IMERG cells selected by rasterizing the basin."""
    lons, lats = data2d["lon"].values, data2d["lat"].values
    dlon = np.abs(np.median(np.diff(lons))) if len(lons) > 1 else 0.1
    dlat = np.abs(np.median(np.diff(lats))) if len(lats) > 1 else 0.1
    lat_desc = np.sort(lats)[::-1]  # rasterio needs a north-up grid

    transform = from_origin(lons.min() - dlon / 2, lat_desc.max() + dlat / 2, dlon, dlat)
    mask = features.geometry_mask(
        [basin_geometry], out_shape=(len(lat_desc), len(lons)),
        transform=transform, invert=True, all_touched=all_touched,
    )
    return xr.DataArray(mask, dims=["lat", "lon"],
                        coords={"lat": lat_desc, "lon": lons}).sortby("lat")


def area_weights(data2d, basin):
    """Normalized weights = cell area inside the basin / total area inside."""
    grid = grid_cells_gdf(data2d)
    geom = basin.geometry.iloc[0]
    grid = grid[grid.intersects(geom)].copy()
    if grid.empty:
        return None

    metric_crs = basin.estimate_utm_crs()
    grid_m = grid.to_crs(metric_crs)
    basin_m = basin.to_crs(metric_crs).geometry.iloc[0]
    grid_m["inter_m2"] = grid_m.geometry.intersection(basin_m).area
    grid_m = grid_m[grid_m["inter_m2"] > 0]
    grid_m["weight"] = grid_m["inter_m2"] / grid_m["inter_m2"].sum()

    weights = xr.zeros_like(data2d, dtype=float)
    for _, row in grid_m.iterrows():
        weights.loc[dict(lat=row["lat"], lon=row["lon"])] = row["weight"]
    return weights


def compute_all_methods(precip, basin):
    """Basin-averaged series for the three methods (+ number of cells used)."""
    data2d = precip.isel(time=0)
    geom = basin.geometry.iloc[0]

    # The available grid must fully cover the basin
    xmin, ymin, xmax, ymax = grid_cells_gdf(data2d).total_bounds
    if not box(xmin, ymin, xmax, ymax).covers(geom):
        sys.exit("ERROR: the IMERG grid does not fully cover the basin. Enlarge BBOX.")

    results = {}

    weights = area_weights(data2d, basin)
    if weights is not None:
        valid_w = weights.where(precip.notnull())
        series = (precip * valid_w).sum(dim=["lat", "lon"]) / valid_w.sum(dim=["lat", "lon"])
        results["Area-weighted"] = (series, int((weights > 0).sum()))

    for flag in (True, False):
        mask = rasterize_basin_mask(data2d, geom, all_touched=flag)
        series = precip.where(mask).mean(dim=["lat", "lon"])
        results[f"all_touched={flag}"] = (series, int(mask.sum()))

    return results


def choose_method(results, requested):
    """Print a summary of the methods and return the selected one."""
    valid = [m for m in METHODS if m in results and not bool(results[m][0].isnull().all())]
    if not valid:
        sys.exit("ERROR: no basin-averaging method produced valid values.")

    print("\nBasin-averaging methods")
    print(f"  {'#':<3}{'method':<20}{'cells':>7}{'total (mm)':>13}")
    for i, m in enumerate(METHODS, 1):
        series, n_cells = results.get(m, (None, 0))
        total = "n/a" if m not in valid else f"{float(series.sum()):.1f}"
        print(f"  {i:<3}{m:<20}{n_cells:>7}{total:>13}")

    if requested is not None:
        if requested not in valid:
            sys.exit(f"ERROR: method '{requested}' is not applicable "
                     f"(no IMERG cell selected). Choose one of: {', '.join(valid)}")
        return requested

    default = "Area-weighted" if "Area-weighted" in valid else valid[0]
    while True:
        answer = input(f"\nChoose the method [1-3, Enter = {default}]: ").strip()
        if answer == "":
            return default
        if answer in ("1", "2", "3") and METHODS[int(answer) - 1] in valid:
            return METHODS[int(answer) - 1]
        print(f"Invalid choice. Available methods: {', '.join(valid)}")


# ============================================================
# 6. Outputs: Excel and plot
# ============================================================

def output_stem(time_scale, method, start_date, end_date):
    safe_method = method.replace("=", "_").replace(" ", "_")
    return (f"basin_precipitation_{time_scale.replace(' ', '')}_{safe_method}_"
            f"{start_date:%Y%m%d}_{end_date:%Y%m%d}")


def save_excel(series, path, info):
    df = pd.DataFrame({
        "date": pd.to_datetime(series["time"].values),
        "basin_averaged_precipitation_mm": series.values,
    })
    info_df = pd.DataFrame(list(info.items()), columns=["parameter", "value"])
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        df.to_excel(writer, sheet_name="precipitation", index=False)
        info_df.to_excel(writer, sheet_name="info", index=False)
    print(f"Excel file saved to: {path.relative_to(BASE_DIR)}")


def plot_series(series, time_scale, method, path, show):
    fig, ax = plt.subplots(figsize=(11, 5))
    times = pd.to_datetime(series["time"].values)
    ax.plot(times, series.values, marker="o", markersize=3, color="#1d3557")

    ax.xaxis.set_major_locator(mdates.AutoDateLocator(minticks=3, maxticks=10))
    span_days = (times.max() - times.min()).total_seconds() / 86400
    if span_days <= 2:
        fmt = "%d/%m/%Y\n%H:%M"
    elif span_days <= 120:
        fmt = "%d/%m/%Y"
    elif span_days <= 730:
        fmt = "%m/%Y"
    else:
        fmt = "%Y"
    ax.xaxis.set_major_formatter(mdates.DateFormatter(fmt))
    plt.setp(ax.get_xticklabels(), rotation=30, ha="right")
    ax.margins(x=0.02)

    ax.set_ylabel("Basin-averaged precipitation (mm)")
    ax.set_xlabel("Date")
    ax.set_title(f"Basin-averaged precipitation ({time_scale}) - method: {method}")
    ax.grid(axis="x", alpha=0.2)
    fig.tight_layout()

    fig.savefig(path, dpi=300, bbox_inches="tight")
    print(f"Figure saved to: {path.relative_to(BASE_DIR)}")
    if show:
        plt.show()
    plt.close(fig)


# ============================================================
# Main
# ============================================================

def parse_args():
    p = argparse.ArgumentParser(
        description="Download IMERG data and compute basin-averaged precipitation.")
    p.add_argument("--scale", default=TIME_SCALE,
                   help="30min, Daily or Monthly (default: %(default)s)")
    p.add_argument("--start", default=START_DATE, help="start date YYYY-MM-DD")
    p.add_argument("--end", default=END_DATE, help="end date YYYY-MM-DD")
    p.add_argument("--bbox", nargs=4, type=float, default=BBOX,
                   metavar=("LON_MIN", "LAT_MIN", "LON_MAX", "LAT_MAX"),
                   help="area of interest (default: shapefile extent + buffer)")
    p.add_argument("--method", default=METHOD,
                   help="weighted, true or false (default: ask)")
    p.add_argument("--show", action="store_true", default=SHOW_PLOT,
                   help="also show the plot on screen")
    return p.parse_args()


def main():
    args = parse_args()
    time_scale = normalize_time_scale(args.scale)
    start_date, end_date = parse_date(args.start), parse_date(args.end)
    method = normalize_method(args.method)
    if start_date > end_date:
        sys.exit("ERROR: the start date must be earlier than or equal to the end date.")

    OUT_DIR.mkdir(exist_ok=True)
    NC_DIR.mkdir(exist_ok=True)
    if not SHP_DIR.is_dir():
        sys.exit(f"ERROR: folder '{SHP_DIR.name}/' not found next to the script.")

    # Basin first: it can define the area of interest
    basin, basin_area_km2 = load_basin()
    bbox = tuple(args.bbox) if args.bbox else bbox_from_basin(basin, BBOX_BUFFER)

    print("\nSettings")
    print(f"  Temporal resolution: {time_scale}")
    print(f"  Period: {start_date} -> {end_date}")
    print(f"  Area (lon_min, lat_min, lon_max, lat_max): {bbox}\n")

    imerg_files = get_imerg_files(time_scale, start_date, end_date, bbox)

    print(f"\nLoading {len(imerg_files)} IMERG files...")
    precip = convert_to_precipitation_depth(load_imerg_stack(imerg_files, bbox), time_scale)

    results = compute_all_methods(precip, basin)
    method = choose_method(results, method)
    series, n_cells = results[method]
    print(f"\nSelected method: {method}")

    n_missing = int(series.isnull().sum())
    if n_missing:
        print(f"WARNING: {n_missing} timestep(s) with missing values (kept as empty cells).")

    stem = output_stem(time_scale, method, start_date, end_date)
    info = {
        "temporal_resolution": time_scale,
        "product": IMERG_PRODUCTS[time_scale]["short_name"],
        "version": IMERG_PRODUCTS[time_scale]["version"],
        "start_date": str(start_date),
        "end_date": str(end_date),
        "bbox (lon_min, lat_min, lon_max, lat_max)": str(bbox),
        "basin_area_km2": round(basin_area_km2, 2),
        "method": method,
        "imerg_cells_used": n_cells,
        "units": "mm per timestep",
        "missing_timesteps": n_missing,
        "total_precipitation_mm": round(float(series.sum()), 2),
    }
    save_excel(series, OUT_DIR / f"{stem}.xlsx", info)
    plot_series(series, time_scale, method, OUT_DIR / f"{stem}.png", args.show)
    print("\nDone.")


if __name__ == "__main__":
    main()

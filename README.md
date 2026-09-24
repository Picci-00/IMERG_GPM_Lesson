# IMERG GPM — Lesson on Satellite-Based Precipitation Estimation

Teaching material for a university lesson on precipitation estimated from **IMERG GPM (Global Precipitation Measurement, Final Run)**: a hands-on Jupyter Notebook to download, process, and visualize IMERG data over an example catchment.

## Repository contents

| File / folder | Description |
|---|---|
| `IMERG_notebook.ipynb` | Jupyter notebook with the hands-on exercises |
| `environment.yml` | Conda environment file listing all required Python libraries |
| `IMERG_setup.pdf` | Step-by-step guide to set up the environment, configure a NASA Earthdata account, and launch Jupyter Lab |
| `shapefile/` | Example shapefile of the "La Bruna" catchment (.shp, .shx, .dbf, .prj, .cpg), used in the notebook to compute basin-averaged precipitation |

The `La_Bruna` catchment is located in **central Italy (Umbria region, Valnerina area)**, roughly between **12.58–12.69 °E and 42.76–42.82 °N**. When the notebook asks you to draw the area of interest on the interactive map (Section 1), make sure your selected rectangle **includes this area**, otherwise the basin will fall outside the downloaded IMERG data and the basin-averaged precipitation steps (Sections 5 onwards) will fail.

The notebook automatically creates two additional folders in the same directory the first time it is run:

- `nc/` — where downloaded IMERG NetCDF files are stored (or where already-downloaded files can be placed)
- `output/` — where the maps, plots, and Excel files produced by the notebook are saved

## Requirements

- **Anaconda** (or Miniconda) installed on your computer
- An internet connection
- A free **NASA Earthdata** account (https://urs.earthdata.nasa.gov), with a `.netrc` file configured in the same folder as the notebook

Detailed instructions for all of these steps are provided in `IMERG_setup.pdf`.

## Getting started

1. Download or clone this repository into a folder on your computer (e.g. `Desktop/imerg_course`).
2. Follow the guide in `IMERG_setup.pdf` to:
   - create the Python environment from `environment.yml`;
   - create a NASA Earthdata account and the `.netrc` file;
   - launch Jupyter Lab.
3. Open `IMERG_notebook.ipynb` in Jupyter Lab and run the cells in order. When selecting the area of interest, remember it must cover the `La_Bruna` catchment (central Italy — see above) for the basin-averaging steps to work.

## Notebook structure

1. **Environment setup** — package installation, folder creation, NASA Earthdata credentials configuration (`.netrc`)
2. **Interactive selection** — temporal resolution (30-minute / daily / monthly), analysis period, area of interest (drawn on a map — must cover the `La_Bruna` catchment in central Italy)
3. **IMERG data search and download** — checks for locally available data and automatically downloads missing granules from NASA Earthdata
4. **Loading and cropping IMERG data** — opening the NetCDF/HDF5 files with `xarray` and cropping to the selected area
5. **Precipitation maps over the selected area** — mean and maximum statistics, with optional Excel export
6. **Loading the basin shapefile** — geometry validation and reprojection to WGS84
7. **IMERG grid coverage of the basin** — reconstruction of the satellite grid cells
8. **Precipitation maps over the basin**
9. **Basin-averaged precipitation with three methods**: `all_touched=True`, `all_touched=False`, and area-weighted averaging
10. **Comparison of the three methods** — time series and cumulative comparison
11. **Selection of the final method and export** — final basin-averaged precipitation time series exported to Excel

## Contacts

**Sofia Ortenzi** — CNR-IRPI, Perugia — sofia.ortenzi@cnr.it
**Lucio Di Matteo** — University of Perugia, Department of Physics and Geology — lucio.dimatteo@unipg.it

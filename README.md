# Catalogue-Converter-for-HAM-Radio-Trainer

TBD

## Setup (Windows)

1. Install Python dependencies and venv: `uv sync`
2. You will need a full installation of TeX Live to render the LaTeX Code contained in the exam questions.
3. CairoSVG Library: You will need to install [GTK for Windows](https://github.com/tschoonj/GTK-for-Windows-Runtime-Environment-Installer/releases) to be able to fully use CairoSVG. (And add the installation directory to your PATH.)

## Converting
1. Download current version of exam questions from [BNetzA](https://www.bundesnetzagentur.de/SharedDocs/Downloads/DE/Sachgebiete/Telekommunikation/Unternehmen_Institutionen/Frequenzen/Amateurfunk/Fragenkatalog/PruefungsfragenZIP.zip?__blob=publicationFile).
2. (optional) Generate 50ohm.de reference links: `uv run build_50ohm_links.py --catalog-zip PruefungsfragenZIP.zip`
3. Convert actual catalogue: `uv run bnetza_to_etq.py PruefungsfragenZIP.zip de.amateurfunk.2024.etq`

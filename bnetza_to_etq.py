#!/usr/bin/env python3
"""Konvertiert den BNetzA-Amateurfunk-Fragenkatalog in eine
HamRadioTrainer-.etq-Datei.

Usage:
  python bnetza_to_etq.py PruefungsfragenZIP.zip neuer_katalog.etq
"""


import subprocess
import shutil
import re
import json
import sys
import tempfile
import zipfile
import cairosvg
from pathlib import Path
from xml.sax.saxutils import escape
import xml.etree.ElementTree as ET
from collections import Counter

from etq_tool import pack

from bnetza_50ohm import add_50ohm_hints

from bnetza_chapter_ids import (
    normalize_catalog_ids,
    validate_bnetza_catalog,
    remap_appendix_ids,
)

from bnetza_latex import LatexRenderer

_LATEX_RENDERER = None

IMAGE_EXTENSIONS = [".svg", ".png", ".jpg", ".jpeg", ".gif"]


def find_json(directory: Path) -> Path:
    candidates = [
        path for path in directory.rglob("*.json")
        if path.stat().st_size > 1000
    ]
    if not candidates:
        raise RuntimeError("Keine JSON-Datei gefunden.")
    return max(candidates, key=lambda path: path.stat().st_size)


def find_image(directory: Path, stem: str) -> Path | None:
    for extension in IMAGE_EXTENSIONS:
        candidate = directory / f"{stem}{extension}"
        if candidate.is_file():
            return candidate
    return None


def latex_to_html(text: str) -> str:
    if _LATEX_RENDERER is None:
        raise RuntimeError(
            "Der LaTeX-Renderer wurde noch nicht initialisiert."
        )

    return _LATEX_RENDERER.render_text(text)


def xml_text(value: str) -> str:
    """HTML bleibt Text innerhalb des äußeren Katalog-XML."""
    return escape(value)


def image_html(reference: str) -> str:
    safe_reference = escape(reference, {'"': "&quot;", "'": "&apos;"})
    return f"<p><img src='{safe_reference}'></p>"


def clean_text(value) -> str:
    if value is None:
        return ""
    return str(value).strip().replace("\r\n", "\n")


def question_xml(question: dict, image_index: dict[str, Path]) -> str:
    number = escape(str(question.get("number", "")))
    text = latex_to_html(clean_text(question.get("question")))

    picture_question = clean_text(question.get("picture_question"))
    if picture_question and picture_question in image_index:
        text += image_html(f"img/{picture_question}{image_index[picture_question].suffix}")

    lines = [f'<question id="{number}">', f"<text>{xml_text(text)}</text>"]

    for letter in "abcd":
        answer = latex_to_html(clean_text(question.get(f"answer_{letter}")))
        picture = clean_text(question.get(f"picture_{letter}"))

        if picture and picture in image_index:
            answer += image_html(
                f"img/{picture}{image_index[picture].suffix}"
            )

        correct = ' correct="true"' if letter == "a" else ""
        lines.append(
            f'<answer type="bool"{correct}>{xml_text(answer)}</answer>'
        )

    lines.append("</question>")
    return "\n".join(lines)


CLASS_NAMES = {
    "1": "N",
    "2": "E",
    "3": "A",
}


def xml_attribute(value) -> str:
    return escape(
        str(value),
        {'"': "&quot;", "'": "&apos;"},
    )


def chapter_start(chapter_id: str, title: str) -> str:
    return (
        f'<chapter id="{xml_attribute(chapter_id)}" '
        f'name="{xml_attribute(title)}">'
    )


def build_sections(
    sections,
    image_index: dict[str, Path],
    path: tuple[int, ...] = (),
    class_filter: str | None = None,
    id_prefix: str = "s",
) -> list[str]:
    lines = []

    for index, section in enumerate(sections):
        current_path = path + (index,)

        questions = [
            question
            for question in section.get("questions", [])
            if (
                class_filter is None
                or str(question.get("class", "")).strip() == class_filter
            )
        ]

        children = build_sections(
            section.get("sections", []),
            image_index,
            current_path,
            class_filter=class_filter,
            id_prefix=id_prefix,
        )

        # Leere Themen nach der Klassenfilterung weglassen.
        if not questions and not children:
            continue

        title = clean_text(
            section.get("title", f"Abschnitt {index + 1}")
        )

        chapter_id = (
            id_prefix + "_" + "_".join(map(str, current_path))
        )

        lines.append(chapter_start(chapter_id, title))

        for question in questions:
            lines.append(question_xml(question, image_index))

        lines.extend(children)
        lines.append("</chapter>")

    return lines


def build_catalog(data, image_index: dict[str, Path]) -> str:
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        "<catalog>",
    ]

    for index, section in enumerate(data.get("sections", [])):
        title = clean_text(section.get("title", ""))

        if "Technische Kenntnisse" in title:
            # Unbekannte Klassen nicht stillschweigend verwerfen.
            technical_questions = list(source_questions(section))
            unknown = [
                str(question.get("number", "?"))
                for question in technical_questions
                if str(question.get("class", "")).strip()
                not in CLASS_NAMES
            ]

            if unknown:
                raise RuntimeError(
                    "Unbekannte oder fehlende Technik-Klasse bei: "
                    + ", ".join(unknown[:20])
                )

            for class_value, class_name in CLASS_NAMES.items():
                branch_title = (
                    f"Technische Kenntnisse Klasse {class_name}"
                )

                # Jeder Klassenbaum hat seinen eigenen ID-Namensraum.
                lines.extend(
                    build_sections(
                        [
                            {
                                **section,
                                "title": branch_title,
                            }
                        ],
                        image_index,
                        class_filter=class_value,
                        id_prefix=f"technik_{class_name.lower()}",
                    )
                )

        else:
            # Betrieb und Vorschriften behalten ihre Unterstruktur.
            prefix = "Prüfungsfragen im Prüfungsteil:"
            display_title = title.removeprefix(prefix).strip()

            lines.extend(
                build_sections(
                    [
                        {
                            **section,
                            "title": display_title,
                        }
                    ],
                    image_index,
                    id_prefix=f"teil_{index}",
                )
            )

    lines.extend(["</catalog>", ""])
    return "\n".join(lines)


def build_header(metadata: dict) -> str:
    edition = escape(str(metadata.get("edition", "")))
    issued = escape(str(metadata.get("issued_on", "")))
    valid_from = escape(str(metadata.get("valid_from", "")))

    return f"""<?xml version="1.0" encoding="UTF-8"?>
<catalogheader created="{issued}" version="1" revision="1">
    <name>Amateurfunkprüfung Deutschland</name>
    <description>Aus dem maschinenlesbaren Fragenkatalog der Bundesnetzagentur konvertierter Katalog.</description>
    <tags country="de" language="de" id="amateurfunk"/>
    <reference>
        <name>Bundesnetzagentur</name>
        <release>{edition}</release>
    </reference>
    <valid from="{valid_from}" until=""/>
    <application name="HamRadioTrainer"/>
    <options mixanswers="1" chapter_separator="" tree_expand_depth="1"/>
</catalogheader>
"""


def build_appendix(catalog_xml: str) -> str:

    catalog = ET.fromstring(catalog_xml)
    appendix = ET.Element("catalogappendix")

    exam = ET.SubElement(appendix, "exam", {"id": "DIAG"})
    ET.SubElement(exam, "name").text = (
        "Diagnose: alle Katalogbereiche"
    )
    ET.SubElement(exam, "name_short").text = "Alle Bereiche"

    for index, chapter in enumerate(
        catalog.findall("chapter"), start=1
    ):
        chapter_id = chapter.get("id")
        if not chapter_id:
            raise RuntimeError(
                "Hauptkapitel ohne ID in catalog.xml."
            )

        title = (
            chapter.get("name")
            or chapter.findtext("name")
            or chapter_id
        )

        part_id = f"P{index}"

        part = ET.SubElement(
            exam,
            "exam_part",
            {
                "id": part_id,
                "duration": (
                    "60"
                    if title == "Technische Kenntnisse Klasse A"
                    else "45"
                ),
                "type": "optional",
            },
        )

        ET.SubElement(part, "name").text = title
        ET.SubElement(part, "name_short").text = title

        ET.SubElement(
            part,
            "exam_section",
            {
                "chapter": chapter_id,
                "count": "25",
            },
        )

        ET.SubElement(
            exam,
            "criterion",
            {
                "id": part_id,
                "maxerrorpoints": "6",
            },
        )

    ET.indent(appendix, space="  ")

    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        + ET.tostring(appendix, encoding="unicode")
        + "\n"
    )


def svg_dimensions(svg_path: Path) -> tuple[int, int]:
    text = svg_path.read_text(encoding="utf-8", errors="ignore")

    width = re.search(
        r'<svg[^>]*\bwidth\s*=\s*["\']([\d.]+)', text, re.I
    )
    height = re.search(
        r'<svg[^>]*\bheight\s*=\s*["\']([\d.]+)', text, re.I
    )
    viewbox = re.search(
        r'<svg[^>]*\bviewBox\s*=\s*["\']\s*([\d.-]+)[\s,]+([\d.-]+)'
        r'[\s,]+([\d.-]+)[\s,]+([\d.-]+)',
        text,
        re.I,
    )

    if width and height:
        return (
            max(1, int(float(width.group(1)))),
            max(1, int(float(height.group(1)))),
        )

    if viewbox:
        return (
            max(1, int(float(viewbox.group(3)))),
            max(1, int(float(viewbox.group(4)))),
        )

    return 800, 600


def find_edge() -> str | None:
    candidates = [
        Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
        Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
        Path.home() / r"AppData\Local\Microsoft\Edge\Application\msedge.exe",
    ]

    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)

    return shutil.which("msedge")


def convert_svg_with_edge(svg_path: Path, png_path: Path) -> bool:
    edge = find_edge()

    if not edge:
        return False

    width, height = svg_dimensions(svg_path)
    render_width = min(1600, max(320, width + 40))
    render_height = min(1600, max(240, height + 40))

    html_path = svg_path.with_suffix(".conversion.html")
    html_path.write_text(
        f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<style>
    html, body {{
        margin: 0;
        padding: 0;
        background: white;
    }}
    img {{
        display: block;
        max-width: 100%;
        height: auto;
    }}
</style>
</head>
<body>
<img src="{svg_path.name}">
</body>
</html>
""",
        encoding="utf-8",
    )

    try:
        result = subprocess.run(
            [
                edge,
                "--headless",
                "--disable-gpu",
                "--no-first-run",
                "--no-default-browser-check",
                "--hide-scrollbars",
                f"--window-size={render_width},{render_height}",
                f"--screenshot={png_path}",
                html_path.as_uri(),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=20,
        )
    finally:
        html_path.unlink(missing_ok=True)

    return result.returncode == 0 and png_path.is_file()


def convert_svg_files(directory: Path):
    """Konvertiert SVGs mit CairoSVG; Edge dient als Fallback."""

    converted = 0
    failed = 0
    fallback_used = 0

    for svg_path in sorted(directory.rglob("*.svg")):
        png_path = svg_path.with_suffix(".png")

        if png_path.exists():
            continue

        try:
            cairosvg.svg2png(
                url=str(svg_path),
                write_to=str(png_path),
                output_width=800,
            )
            converted += 1

        except Exception as error:
            if convert_svg_with_edge(svg_path, png_path):
                fallback_used += 1
                print(f"Edge-Fallback erfolgreich: {svg_path.name}")
            else:
                failed += 1
                print(
                    f"Konvertierung fehlgeschlagen: {svg_path.name}: {error}"
                )

    print(f"SVG konvertiert mit CairoSVG: {converted}")
    print(f"SVG konvertiert mit Edge-Fallback: {fallback_used}")

    if failed:
        print(f"SVG-Fehler: {failed}")


def source_questions(node):
    """Liest alle Fragen unabhängig von der Abschnittstiefe."""
    if isinstance(node, dict):
        if "number" in node and "question" in node:
            yield node
            return

        for value in node.values():
            yield from source_questions(value)

    elif isinstance(node, list):
        for value in node:
            yield from source_questions(value)


def validate_catalog(data, catalog_xml: str):
    root = ET.fromstring(catalog_xml)

    chapter_ids = [
        chapter.get("id", "")
        for chapter in root.findall(".//chapter")
    ]

    duplicates = [
        chapter_id
        for chapter_id, count in Counter(chapter_ids).items()
        if count > 1
    ]

    if "" in chapter_ids:
        raise RuntimeError("Mindestens ein Kapitel hat keine ID.")

    if duplicates:
        raise RuntimeError(
            "Doppelte Kapitel-IDs: " + ", ".join(duplicates[:20])
        )

    source_ids = Counter(
        str(question["number"])
        for question in source_questions(data)
    )

    questions = root.findall(".//question")
    target_ids = Counter(question.get("id", "") for question in questions)

    if source_ids != target_ids:
        missing = list((source_ids - target_ids).elements())
        extra = list((target_ids - source_ids).elements())

        raise RuntimeError(
            "Fragen wurden nicht vollständig übernommen.\n"
            f"Quelle: {sum(source_ids.values())}\n"
            f"XML: {sum(target_ids.values())}\n"
            f"Fehlend, erste 20: {missing[:20]}\n"
            f"Zusätzlich, erste 20: {extra[:20]}"
        )

    for question in questions:
        qid = question.get("id")
        text = question.find("text")
        answers = question.findall("answer")

        if text is None:
            raise RuntimeError(f"{qid}: Fragetext fehlt.")

        if len(answers) != 4:
            raise RuntimeError(f"{qid}: Nicht genau vier Antworten.")

        if sum(a.get("correct") == "true" for a in answers) != 1:
            raise RuntimeError(f"{qid}: Nicht genau eine richtige Antwort.")

        for element in [text, *answers]:
            if len(element):
                raise RuntimeError(
                    f"{qid}: Echte XML-Unterelemente in <{element.tag}>. "
                    "HTML muss als Text escaped werden."
                )

    print(f"Fragen Quelle: {sum(source_ids.values())}")
    print(f"Fragen XML:    {len(questions)}")
    print(f"Kapitel XML:   {len(root.findall('.//chapter'))}")
    print("XML-Prüfung:   OK")


def main():
    
    global _LATEX_RENDERER
    
    if len(sys.argv) != 3:
        raise SystemExit(
            "Usage: python bnetza_to_etq.py "
            "PruefungsfragenZIP.zip neuer_katalog.etq"
        )

    zip_path = Path(sys.argv[1])
    output = Path(sys.argv[2])

    if not zip_path.is_file():
        raise SystemExit(f"ZIP-Datei nicht gefunden: {zip_path}")

    with tempfile.TemporaryDirectory() as temporary:
        temporary = Path(temporary)

        with zipfile.ZipFile(zip_path) as archive:
            archive.extractall(temporary)

        json_file = find_json(temporary)
        data = json.loads(json_file.read_text(encoding="utf-8-sig"))
        metadata = data.get("metadata", {})

        image_index = {}
        for path in temporary.rglob("*"):
            if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS:
                image_index[path.stem] = path

        convert_svg_files(temporary)

        image_index = build_image_index(temporary)

        image_types = Counter(
            path.suffix.lower()
            for path in image_index.values()
        )

        print("Ausgewählte Bildformate:", dict(image_types))

        for stem in ("AB407_q", "AB407_a", "NB401_a"):
            path = image_index.get(stem)
            print(
                f"Bildtest {stem}: "
                f"{path.name if path else 'NICHT GEFUNDEN'}"
            )

        debug_images = output.parent / ".image-diagnosis"
        debug_images.mkdir(exist_ok=True)

        for stem in ("AB407_q", "AB407_a", "NB401_a"):
            path = image_index.get(stem)
            if path is not None:
                shutil.copy2(path, debug_images / path.name)

        work = temporary / "etq"
        work.mkdir()
        (work / "header.xml").write_text(
            build_header(metadata), encoding="utf-8"
        )
        
        _LATEX_RENDERER = LatexRenderer(
            work_dir=work,
            cache_dir=output.parent / ".bnetza_latex_cache",
            dpi=192,
            display_dpi=96,
            font_pt=11,
            timeout=60,
        )
        
        
        # Zunächst mit dem bestehenden, funktionierenden Code erzeugen.
        raw_catalog_xml = build_catalog(data, image_index)

        # Technische Kapitel-IDs durch lokale BNetzA-Kennungen ersetzen.
        catalog_xml, chapter_id_map = normalize_catalog_ids(
            raw_catalog_xml
        )

        # Vollständigkeit, Kapitelpfade und Fragezuordnung prüfen.
        validate_bnetza_catalog(data, catalog_xml)

        (work / "catalog.xml").write_text(
            catalog_xml,
            encoding="utf-8",
        )

        output.with_suffix(".catalog.xml").write_text(
            catalog_xml,
            encoding="utf-8",
        )
        
        
        appendix_xml = build_appendix(raw_catalog_xml)

        appendix_xml = remap_appendix_ids(
            appendix_xml,
            chapter_id_map,
        )

        # Alle gefundenen 50ohm-Kursseiten als Fragehinweise ergänzen.
        appendix_xml = add_50ohm_hints(
            appendix_xml=appendix_xml,
            catalog_xml=catalog_xml,
            index_path=Path(__file__).resolve().parent / "50ohm_links.json",
        )

        (work / "appendix.xml").write_text(
            appendix_xml,
            encoding="utf-8",
        )

        # Zusätzlich zur Kontrolle neben dem ETQ-Export speichern.
        output.with_suffix(".appendix.xml").write_text(
            appendix_xml,
            encoding="utf-8",
        )
        

        image_dir = work / "img"
        image_dir.mkdir()

        copied = 0
        for stem, path in image_index.items():
            target = image_dir / path.name
            target.write_bytes(path.read_bytes())
            copied += 1

        pack(work, output)

    print(f"Fertig: {output}")
    print(f"Bilder: {copied}")


def build_image_index(directory: Path) -> dict[str, Path]:
    """Wählt pro Bildkennung bevorzugt PNG statt SVG."""
    priority = {
        ".png": 0,
        ".jpg": 1,
        ".jpeg": 2,
        ".gif": 3,
        ".svg": 4,
    }

    image_index = {}

    for path in sorted(directory.rglob("*")):
        if not path.is_file():
            continue

        suffix = path.suffix.lower()
        if suffix not in priority:
            continue

        previous = image_index.get(path.stem)

        if (
            previous is None
            or priority[suffix] < priority[previous.suffix.lower()]
        ):
            image_index[path.stem] = path

    return image_index


if __name__ == "__main__":
    main()
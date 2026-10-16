"""BNetzA chapter IDs for the HamRadioTrainer converter (Python 3.10+)."""
import re
from collections import Counter
import xml.etree.ElementTree as ET

QUESTION_ID = re.compile(r"[NEABV][A-Z][0-9]{3}")


def normalize_catalog_ids(catalog_xml):
    root = ET.fromstring(catalog_xml)
    if root.tag != "catalog":
        raise ValueError("Wurzelelement muss <catalog> sein.")
    mapping = {}
    paths = set()

    def walk(chapter, depth, parent_path):
        if depth > 2:
            raise ValueError("Mehr als drei Kapitelebenen: keine eindeutige BNetzA-Zuordnung.")
        questions = chapter.findall(".//question")
        ids = [q.get("id", "") for q in questions]
        if not ids or any(not QUESTION_ID.fullmatch(qid) for qid in ids):
            raise ValueError(f"Leeres Kapitel oder ungueltige Frage-ID: {chapter.attrib}")
        prefixes = {qid[:depth + 1] for qid in ids}
        if len(prefixes) != 1:
            raise ValueError(f"Kapitel mischt BNetzA-Gruppen: {chapter.attrib}: {sorted(prefixes)}")
        full_path = prefixes.pop()
        if full_path[:-1] != parent_path:
            raise ValueError(f"Inkonsistenter Kapitelpfad: {parent_path!r} -> {full_path!r}")
        if full_path in paths:
            raise ValueError(f"Doppelter vollstaendiger Kapitelpfad: {full_path}")
        paths.add(full_path)
        old_id = chapter.get("id", "")
        if old_id:
            mapping[old_id] = full_path
        chapter.set("id", full_path[-1])
        for child in chapter.findall("chapter"):
            walk(child, depth + 1, full_path)

    for chapter in root.findall("chapter"):
        walk(chapter, 0, "")
    ET.indent(root, space="  ")
    text = ET.tostring(root, encoding="unicode")
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + text + "\n", mapping


def source_questions(node):
    if isinstance(node, dict):
        if "number" in node and "question" in node:
            yield node
        else:
            for value in node.values():
                yield from source_questions(value)
    elif isinstance(node, list):
        for value in node:
            yield from source_questions(value)


def validate_bnetza_catalog(data, catalog_xml):
    root = ET.fromstring(catalog_xml)
    source = Counter(str(q["number"]) for q in source_questions(data))
    questions = root.findall(".//question")
    target = Counter(q.get("id", "") for q in questions)
    if source != target:
        raise ValueError(f"Fragenabweichung: fehlend={list((source-target).elements())[:20]}, "
                         f"zusaetzlich={list((target-source).elements())[:20]}")
    if any(n != 1 for n in target.values()):
        raise ValueError("Doppelte Frage-IDs.")
    paths = set()
    def walk(chapter, parent):
        local_id = chapter.get("id", "")
        if len(local_id) != 1:
            raise ValueError(f"Kapitel-ID ist nicht einstellig: {local_id!r}")
        path = parent + local_id
        if path in paths:
            raise ValueError(f"Doppelter Kapitelpfad: {path}")
        paths.add(path)
        for q in chapter.findall("question"):
            if not q.get("id", "").startswith(path):
                raise ValueError(f"Frage {q.get('id')} passt nicht zu {path}")
        for c in chapter.findall("chapter"):
            walk(c, path)
    for c in root.findall("chapter"):
        walk(c, "")
    for q in questions:
        text = q.find("text")
        answers = q.findall("answer")
        if text is None or len(answers) != 4:
            raise ValueError(f"Fragetext oder vier Antworten fehlen: {q.get('id')}")
        if sum(a.get("correct") == "true" for a in answers) != 1:
            raise ValueError(f"Nicht genau eine richtige Antwort: {q.get('id')}")
        if any(len(e) for e in [text, *answers]):
            raise ValueError(f"HTML muss XML-escaped sein: {q.get('id')}")
    print(f"Fragen Quelle: {sum(source.values())}")
    print(f"Fragen XML:    {len(questions)}")
    print(f"Kapitel XML:   {len(paths)}")
    print("Kapitelpfade:  " + ", ".join(c.get("id") for c in root.findall("chapter")))
    print("XML-Pruefung:  OK")


def remap_appendix_ids(appendix_xml, mapping):
    root = ET.fromstring(appendix_xml)
    for section in root.iter("exam_section"):
        refs = section.get("chapter", "").split("|")
        section.set("chapter", "|".join(mapping.get(ref, ref) for ref in refs))
    ET.indent(root, space="  ")
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="unicode") + "\n"

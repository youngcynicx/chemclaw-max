from __future__ import annotations

import hashlib
import json
import re
import uuid
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from xml.etree import ElementTree
from xml.sax.saxutils import escape

from .db import connect, init_db
from .inventory import persist_inventory_event
from .models import InventoryEvent
from .units import convert_amount, normalize_unit, units_compatible


MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PACKAGE_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
CELL_REF = re.compile(r"([A-Z]+)(\d+)")
INVALID_XML_CHARACTERS = re.compile(r"[\x00-\x08\x0B\x0C\x0E-\x1F]")
MAX_EXCEL_CELL_TEXT = 32_767
VALID_HAZARD_LEVELS = {"low", "medium", "high", "critical"}


@dataclass(frozen=True)
class _Sheet:
    name: str
    headers: list[str]
    rows: list[list[Any]]
    widths: list[float]
    numeric_columns: set[int]
    formula_columns: dict[int, str] | None = None


def read_inventory_workbook(path: str | Path) -> list[dict[str, Any]]:
    workbook_path = Path(path)
    if workbook_path.suffix.lower() != ".xlsx":
        raise ValueError("Inventory import requires an .xlsx file")
    with zipfile.ZipFile(workbook_path) as archive:
        shared_strings = _read_shared_strings(archive)
        sheet_path = _find_sheet_path(archive, preferred_name="Inventory")
        rows = _read_sheet_rows(archive, sheet_path, shared_strings)
    if not rows:
        raise ValueError("Inventory worksheet is empty")

    headers = [_canonical_header(value) for value in rows[0]]
    required = {"chemical_id", "name", "amount", "unit"}
    missing = required - set(headers)
    if missing:
        raise ValueError(f"Inventory worksheet is missing columns: {sorted(missing)}")
    column_index = {header: index for index, header in enumerate(headers) if header}

    parsed: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    seen_names: set[str] = set()
    for row_number, values in enumerate(rows[1:], start=2):
        if not any(value not in (None, "") for value in values):
            continue
        record = {
            header: values[index] if index < len(values) else None
            for header, index in column_index.items()
        }
        chemical_id = _required_cell_text(record, "chemical_id", row_number)
        name = _required_cell_text(record, "name", row_number)
        unit = normalize_unit(_required_cell_text(record, "unit", row_number))
        if unit is None:
            raise ValueError(f"Row {row_number}: unsupported unit")
        amount = _number(record.get("amount"), "amount", row_number)
        threshold = _number(record.get("threshold", 0), "threshold", row_number)
        max_auto_delta = _optional_number(
            record.get("max_auto_delta"),
            "max_auto_delta",
            row_number,
        )
        if amount < 0 or threshold < 0:
            raise ValueError(f"Row {row_number}: amount and threshold must be non-negative")
        if max_auto_delta is not None and max_auto_delta <= 0:
            raise ValueError(f"Row {row_number}: max_auto_delta must be positive")
        hazard_level = (
            _optional_cell_text(record.get("hazard_level")) or "low"
        ).lower()
        if hazard_level not in VALID_HAZARD_LEVELS:
            raise ValueError(f"Row {row_number}: invalid hazard_level")
        if chemical_id in seen_ids:
            raise ValueError(f"Row {row_number}: duplicate chemical_id {chemical_id}")
        if name.casefold() in seen_names:
            raise ValueError(f"Row {row_number}: duplicate chemical name {name}")
        seen_ids.add(chemical_id)
        seen_names.add(name.casefold())
        parsed.append(
            {
                "chemical_id": chemical_id,
                "name": name,
                "formula": _optional_cell_text(record.get("formula")),
                "location": _optional_cell_text(record.get("location")),
                "amount": amount,
                "unit": unit,
                "threshold": threshold,
                "hazard_level": hazard_level,
                "max_auto_delta": max_auto_delta,
            }
        )
    if not parsed:
        raise ValueError("Inventory worksheet contains no data rows")
    return parsed


def import_inventory_workbook(
    db_path: str | Path,
    workbook_path: str | Path,
    *,
    mode: str = "merge",
    imported_by: str = "excel_admin",
) -> dict[str, Any]:
    if mode not in {"create", "merge"}:
        raise ValueError('mode must be "create" or "merge"')
    init_db(db_path)
    source = Path(workbook_path)
    rows = read_inventory_workbook(source)
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        previous = conn.execute(
            """
            SELECT batch_id, row_count, event_count
            FROM import_batches
            WHERE content_sha256 = ?
            """,
            (digest,),
        ).fetchone()
        if previous is not None:
            return {
                "batch_id": previous["batch_id"],
                "row_count": previous["row_count"],
                "event_count": previous["event_count"],
                "idempotent_replay": True,
            }
        if mode == "create":
            chemical_count = conn.execute("SELECT COUNT(*) FROM chemicals").fetchone()[0]
            if chemical_count:
                raise ValueError("create mode requires an empty chemical inventory")

        name_owners = {
            row["name"].casefold(): row["chemical_id"]
            for row in conn.execute(
                "SELECT chemical_id, name FROM chemicals"
            ).fetchall()
        }
        batch_id = f"imp_{uuid.uuid4().hex[:12]}"
        event_count = 0
        for row in rows:
            folded_name = row["name"].casefold()
            name_owner = name_owners.get(folded_name)
            if name_owner is not None and name_owner != row["chemical_id"]:
                raise ValueError(
                    f'{row["chemical_id"]}: chemical name "{row["name"]}" '
                    f"is already used by {name_owner}"
                )
            existing = conn.execute(
                "SELECT * FROM chemicals WHERE chemical_id = ?",
                (row["chemical_id"],),
            ).fetchone()
            transaction_type: str | None
            if existing is None:
                conn.execute(
                    """
                    INSERT INTO chemicals
                      (chemical_id, name, formula, location, amount, unit, threshold,
                       hazard_level, max_auto_delta)
                    VALUES (?, ?, ?, ?, 0, ?, ?, ?, ?)
                    """,
                    (
                        row["chemical_id"],
                        row["name"],
                        row["formula"],
                        row["location"],
                        row["unit"],
                        row["threshold"],
                        row["hazard_level"],
                        row["max_auto_delta"],
                    ),
                )
                name_owners[folded_name] = row["chemical_id"]
                transaction_type = "opening_balance" if row["amount"] > 0 else None
            else:
                if not units_compatible(row["unit"], existing["unit"]):
                    raise ValueError(
                        f'{row["chemical_id"]}: imported unit is incompatible with '
                        f'stored unit "{existing["unit"]}"'
                    )
                current_amount = float(existing["amount"])
                stored_unit = existing["unit"]
                imported_amount = convert_amount(
                    row["amount"],
                    row["unit"],
                    stored_unit,
                )
                imported_threshold = convert_amount(
                    row["threshold"],
                    row["unit"],
                    stored_unit,
                )
                imported_max_auto_delta = (
                    convert_amount(
                        row["max_auto_delta"],
                        row["unit"],
                        stored_unit,
                    )
                    if row["max_auto_delta"] is not None
                    else None
                )
                transaction_type = (
                    "adjust"
                    if abs(imported_amount - current_amount) > 1e-12
                    else None
                )
                conn.execute(
                    """
                    UPDATE chemicals
                    SET name = ?, formula = ?, location = ?, threshold = ?,
                        hazard_level = ?, max_auto_delta = ?,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE chemical_id = ?
                    """,
                    (
                        row["name"],
                        row["formula"],
                        row["location"],
                        imported_threshold,
                        row["hazard_level"],
                        imported_max_auto_delta,
                        row["chemical_id"],
                    ),
                )
                old_name = str(existing["name"]).casefold()
                if old_name != folded_name:
                    name_owners.pop(old_name, None)
                    name_owners[folded_name] = row["chemical_id"]

            if transaction_type is None:
                continue
            message_id = f"msg_{uuid.uuid4().hex[:12]}"
            source_message_id = f"{digest}:{row['chemical_id']}"
            raw_text = (
                f"Excel {transaction_type} for {row['name']}: "
                f"{row['amount']} {row['unit']}"
            )
            conn.execute(
                """
                INSERT INTO message_logs
                  (message_id, source, source_message_id, raw_text, parsed_intent)
                VALUES (?, 'excel_import', ?, ?, 'inventory_update')
                """,
                (message_id, source_message_id, raw_text),
            )
            event = InventoryEvent(
                event_type="inventory_update",
                event_subtype=transaction_type,
                item_type="chemical",
                item_id=row["chemical_id"],
                item_name=row["name"],
                amount=row["amount"],
                unit=row["unit"],
                transaction_type=transaction_type,
                confidence=1.0,
                requires_confirmation=False,
                raw_text=raw_text,
            )
            event_id = persist_inventory_event(
                conn,
                message_id,
                event,
                user_confirmed=True,
                confirmed_by=imported_by,
            )
            status = conn.execute(
                "SELECT status FROM event_proposals WHERE event_id = ?",
                (event_id,),
            ).fetchone()[0]
            if status != "applied":
                audit = conn.execute(
                    """
                    SELECT reason
                    FROM audit_logs
                    WHERE event_id = ?
                    ORDER BY rowid DESC LIMIT 1
                    """,
                    (event_id,),
                ).fetchone()
                raise ValueError(
                    f"{row['chemical_id']}: import event was not applied: "
                    f"{audit['reason'] if audit else status}"
                )
            event_count += 1

        conn.execute(
            """
            INSERT INTO import_batches
              (batch_id, source_path, content_sha256, mode,
               imported_by, row_count, event_count)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                batch_id,
                str(source.resolve()),
                digest,
                mode,
                imported_by,
                len(rows),
                event_count,
            ),
        )
        conn.commit()
        return {
            "batch_id": batch_id,
            "row_count": len(rows),
            "event_count": event_count,
            "idempotent_replay": False,
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def export_inventory_workbook(
    db_path: str | Path,
    output_path: str | Path,
) -> Path:
    init_db(db_path)
    conn = connect(db_path)
    try:
        inventory = conn.execute(
            """
            SELECT chemical_id, name, formula, location, amount, unit, threshold,
                   hazard_level, max_auto_delta, updated_at
            FROM chemicals
            ORDER BY name
            """
        ).fetchall()
        transactions = conn.execute(
            """
            SELECT transaction_id, event_id, message_id, chemical_id, user_id,
                   project_id, transaction_type, amount_delta, unit,
                   confirmation_status, created_at, raw_text
            FROM inventory_transactions
            ORDER BY created_at, rowid
            """
        ).fetchall()
        events = conn.execute(
            """
            SELECT event_id, message_id, event_type, event_subtype, status,
                   policy_version, schema_version, supersedes_event_id,
                   confirmed_by, created_at, updated_at
            FROM event_proposals
            ORDER BY created_at, rowid
            """
        ).fetchall()
    finally:
        conn.close()

    inventory_rows = [
        [
            row["chemical_id"],
            row["name"],
            row["formula"],
            row["location"],
            row["amount"],
            row["unit"],
            row["threshold"],
            bool(row["amount"] <= row["threshold"]),
            row["hazard_level"],
            row["max_auto_delta"],
            row["updated_at"],
        ]
        for row in inventory
    ]
    sheets = [
        _Sheet(
            "Inventory",
            [
                "chemical_id",
                "name",
                "formula",
                "location",
                "amount",
                "unit",
                "threshold",
                "low_stock",
                "hazard_level",
                "max_auto_delta",
                "updated_at",
            ],
            inventory_rows,
            [16, 22, 14, 24, 12, 10, 12, 12, 14, 16, 22],
            {4, 6, 9},
            {7: "E{row}<=G{row}"},
        ),
        _Sheet(
            "Transactions",
            [
                "transaction_id",
                "event_id",
                "message_id",
                "chemical_id",
                "user_id",
                "project_id",
                "transaction_type",
                "amount_delta",
                "unit",
                "confirmation_status",
                "created_at",
                "raw_text",
            ],
            [list(row) for row in transactions],
            [19, 19, 19, 16, 14, 14, 18, 14, 10, 20, 22, 44],
            {7},
        ),
        _Sheet(
            "Events",
            [
                "event_id",
                "message_id",
                "event_type",
                "event_subtype",
                "status",
                "policy_version",
                "schema_version",
                "supersedes_event_id",
                "confirmed_by",
                "created_at",
                "updated_at",
            ],
            [list(row) for row in events],
            [19, 19, 18, 18, 22, 24, 16, 19, 18, 22, 22],
            set(),
        ),
        _Sheet(
            "README",
            ["field", "value"],
            [
                ["workbook_type", "ChemClaw-Lab generated inventory projection"],
                ["source_of_truth", "SQLite; this workbook is a disposable view"],
                ["generated_at_utc", datetime.now(timezone.utc).isoformat()],
                ["import_sheet", "Inventory"],
                [
                    "editing_rule",
                    "Edit Inventory values, then import explicitly; "
                    "do not treat the file as live state.",
                ],
            ],
            [24, 78],
            set(),
        ),
    ]
    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    _write_xlsx(destination, sheets)
    return destination


def _find_sheet_path(
    archive: zipfile.ZipFile,
    *,
    preferred_name: str,
) -> str:
    workbook = ElementTree.fromstring(archive.read("xl/workbook.xml"))
    relationships = ElementTree.fromstring(
        archive.read("xl/_rels/workbook.xml.rels")
    )
    targets = {
        relation.attrib["Id"]: relation.attrib["Target"]
        for relation in relationships.findall(f"{{{PACKAGE_REL_NS}}}Relationship")
    }
    candidates: list[tuple[str, str]] = []
    sheets = workbook.find(f"{{{MAIN_NS}}}sheets")
    if sheets is None:
        raise ValueError("Workbook contains no worksheets")
    for sheet in sheets:
        relationship_id = sheet.attrib[f"{{{REL_NS}}}id"]
        candidates.append((sheet.attrib["name"], targets[relationship_id]))
    selected = next(
        (target for name, target in candidates if name.casefold() == preferred_name.casefold()),
        candidates[0][1],
    )
    selected = selected.lstrip("/")
    if selected.startswith("xl/"):
        return selected
    return f"xl/{selected}"


def _read_shared_strings(archive: zipfile.ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in archive.namelist():
        return []
    root = ElementTree.fromstring(archive.read("xl/sharedStrings.xml"))
    return [
        "".join(node.text or "" for node in item.iter(f"{{{MAIN_NS}}}t"))
        for item in root.findall(f"{{{MAIN_NS}}}si")
    ]


def _read_sheet_rows(
    archive: zipfile.ZipFile,
    sheet_path: str,
    shared_strings: list[str],
) -> list[list[Any]]:
    root = ElementTree.fromstring(archive.read(sheet_path))
    sheet_data = root.find(f"{{{MAIN_NS}}}sheetData")
    if sheet_data is None:
        return []
    output: list[list[Any]] = []
    for row in sheet_data.findall(f"{{{MAIN_NS}}}row"):
        values: dict[int, Any] = {}
        for cell in row.findall(f"{{{MAIN_NS}}}c"):
            reference = cell.attrib.get("r", "A1")
            column = _column_number(reference)
            values[column] = _read_cell_value(cell, shared_strings)
        if values:
            width = max(values) + 1
            output.append([values.get(index) for index in range(width)])
        else:
            output.append([])
    return output


def _read_cell_value(
    cell: ElementTree.Element,
    shared_strings: list[str],
) -> Any:
    cell_type = cell.attrib.get("t")
    if cell_type == "inlineStr":
        inline = cell.find(f"{{{MAIN_NS}}}is")
        if inline is None:
            return ""
        return "".join(node.text or "" for node in inline.iter(f"{{{MAIN_NS}}}t"))
    value_node = cell.find(f"{{{MAIN_NS}}}v")
    if value_node is None or value_node.text is None:
        return None
    value = value_node.text
    if cell_type == "s":
        return shared_strings[int(value)]
    if cell_type == "b":
        return value == "1"
    if cell_type in {"str", "e"}:
        return value
    try:
        number = float(value)
        return int(number) if number.is_integer() else number
    except ValueError:
        return value


def _write_xlsx(path: Path, sheets: list[_Sheet]) -> None:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", _content_types_xml(len(sheets)))
        archive.writestr("_rels/.rels", _root_relationships_xml())
        archive.writestr("xl/workbook.xml", _workbook_xml(sheets))
        archive.writestr(
            "xl/_rels/workbook.xml.rels",
            _workbook_relationships_xml(len(sheets)),
        )
        archive.writestr("xl/styles.xml", _styles_xml())
        for index, sheet in enumerate(sheets, start=1):
            archive.writestr(
                f"xl/worksheets/sheet{index}.xml",
                _worksheet_xml(sheet),
            )


def _worksheet_xml(sheet: _Sheet) -> str:
    all_rows = [sheet.headers, *sheet.rows]
    last_column = _column_name(max(len(sheet.headers), 1))
    last_row = max(len(all_rows), 1)
    row_xml: list[str] = []
    for row_index, values in enumerate(all_rows, start=1):
        cells: list[str] = []
        for column_index, value in enumerate(values):
            reference = f"{_column_name(column_index + 1)}{row_index}"
            style = 1 if row_index == 1 else (2 if column_index in sheet.numeric_columns else 0)
            formula = None
            if (
                row_index > 1
                and sheet.formula_columns
                and column_index in sheet.formula_columns
            ):
                formula = sheet.formula_columns[column_index].format(row=row_index)
            cells.append(_cell_xml(reference, value, style=style, formula=formula))
        row_xml.append(f'<row r="{row_index}">{"".join(cells)}</row>')
    columns = "".join(
        f'<col min="{index}" max="{index}" width="{width}" customWidth="1"/>'
        for index, width in enumerate(sheet.widths, start=1)
    )
    conditional = ""
    if sheet.name == "Inventory" and len(sheet.rows):
        conditional = (
            f'<conditionalFormatting sqref="H2:H{last_row}">'
            '<cfRule type="cellIs" dxfId="0" operator="equal" priority="1">'
            "<formula>TRUE</formula></cfRule></conditionalFormatting>"
            f'<conditionalFormatting sqref="I2:I{last_row}">'
            '<cfRule type="expression" dxfId="1" priority="2">'
            '<formula>OR($I2="high",$I2="critical")</formula>'
            "</cfRule></conditionalFormatting>"
        )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<worksheet xmlns="{MAIN_NS}">'
        f'<dimension ref="A1:{last_column}{last_row}"/>'
        '<sheetViews><sheetView workbookViewId="0" showGridLines="0">'
        '<pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/>'
        "</sheetView></sheetViews>"
        '<sheetFormatPr defaultRowHeight="18"/>'
        f"<cols>{columns}</cols>"
        f'<sheetData>{"".join(row_xml)}</sheetData>'
        f'<autoFilter ref="A1:{last_column}{last_row}"/>'
        f"{conditional}"
        "</worksheet>"
    )


def _cell_xml(
    reference: str,
    value: Any,
    *,
    style: int,
    formula: str | None,
) -> str:
    style_attribute = f' s="{style}"' if style else ""
    formula_xml = f"<f>{escape(formula)}</f>" if formula else ""
    if isinstance(value, bool):
        return (
            f'<c r="{reference}"{style_attribute} t="b">'
            f"{formula_xml}<v>{1 if value else 0}</v></c>"
        )
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return (
            f'<c r="{reference}"{style_attribute}>'
            f"{formula_xml}<v>{value}</v></c>"
        )
    if value is None:
        return f'<c r="{reference}"{style_attribute}/>'
    text = escape(_excel_text(value))
    return (
        f'<c r="{reference}"{style_attribute} t="inlineStr">'
        f'<is><t xml:space="preserve">{text}</t></is></c>'
    )


def _content_types_xml(sheet_count: int) -> str:
    worksheets = "".join(
        '<Override '
        f'PartName="/xl/worksheets/sheet{index}.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.'
        'spreadsheetml.worksheet+xml"/>'
        for index in range(1, sheet_count + 1)
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" '
        'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.'
        'spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/styles.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
        f"{worksheets}</Types>"
    )


def _root_relationships_xml() -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<Relationships xmlns="{PACKAGE_REL_NS}">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/'
        'relationships/officeDocument" '
        'Target="xl/workbook.xml"/>'
        "</Relationships>"
    )


def _workbook_xml(sheets: list[_Sheet]) -> str:
    sheet_nodes = "".join(
        f'<sheet name="{escape(sheet.name)}" sheetId="{index}" r:id="rId{index}"/>'
        for index, sheet in enumerate(sheets, start=1)
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<workbook xmlns="{MAIN_NS}" xmlns:r="{REL_NS}">'
        f"<sheets>{sheet_nodes}</sheets>"
        '<calcPr calcId="191029" fullCalcOnLoad="1" forceFullCalc="1"/>'
        "</workbook>"
    )


def _workbook_relationships_xml(sheet_count: int) -> str:
    sheet_relationships = "".join(
        f'<Relationship Id="rId{index}" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
        f'Target="worksheets/sheet{index}.xml"/>'
        for index in range(1, sheet_count + 1)
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<Relationships xmlns="{PACKAGE_REL_NS}">'
        f"{sheet_relationships}"
        f'<Relationship Id="rId{sheet_count + 1}" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" '
        'Target="styles.xml"/>'
        "</Relationships>"
    )


def _styles_xml() -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<styleSheet xmlns="{MAIN_NS}">'
        '<fonts count="2"><font><sz val="10"/><name val="Aptos"/></font>'
        '<font><b/><color rgb="FFFFFFFF"/><sz val="10"/><name val="Aptos"/></font></fonts>'
        '<fills count="3"><fill><patternFill patternType="none"/></fill>'
        '<fill><patternFill patternType="gray125"/></fill>'
        '<fill><patternFill patternType="solid"><fgColor rgb="FF0F766E"/>'
        '<bgColor indexed="64"/></patternFill></fill></fills>'
        '<borders count="2"><border><left/><right/><top/><bottom/><diagonal/></border>'
        '<border><left/><right/><top/><bottom style="thin"><color rgb="FFD1D5DB"/>'
        '</bottom><diagonal/></border></borders>'
        '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" '
        'fillId="0" borderId="0"/></cellStyleXfs>'
        '<cellXfs count="3">'
        '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
        '<xf numFmtId="0" fontId="1" fillId="2" borderId="1" xfId="0" '
        'applyFont="1" applyFill="1" applyBorder="1"><alignment vertical="center"/></xf>'
        '<xf numFmtId="4" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>'
        "</cellXfs>"
        '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
        '<dxfs count="2">'
        '<dxf><font><color rgb="FF991B1B"/></font><fill>'
        '<patternFill patternType="solid"><fgColor rgb="FFFEE2E2"/>'
        '<bgColor rgb="FFFEE2E2"/></patternFill></fill></dxf>'
        '<dxf><font><color rgb="FF9A3412"/></font><fill>'
        '<patternFill patternType="solid"><fgColor rgb="FFFFEDD5"/>'
        '<bgColor rgb="FFFFEDD5"/></patternFill></fill></dxf>'
        "</dxfs>"
        "</styleSheet>"
    )


def _column_number(reference: str) -> int:
    match = CELL_REF.fullmatch(reference)
    if match is None:
        raise ValueError(f"Invalid cell reference: {reference}")
    value = 0
    for character in match.group(1):
        value = value * 26 + ord(character) - ord("A") + 1
    return value - 1


def _column_name(index: int) -> str:
    output = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        output = chr(ord("A") + remainder) + output
    return output


def _canonical_header(value: Any) -> str:
    if value is None:
        return ""
    return re.sub(r"[^a-z0-9]+", "_", str(value).strip().lower()).strip("_")


def _excel_text(value: Any) -> str:
    text = INVALID_XML_CHARACTERS.sub("\uFFFD", str(value))
    if len(text) > MAX_EXCEL_CELL_TEXT:
        marker = "\u2026 [truncated]"
        text = text[: MAX_EXCEL_CELL_TEXT - len(marker)] + marker
    return text


def _required_cell_text(
    record: dict[str, Any],
    field: str,
    row_number: int,
) -> str:
    value = _optional_cell_text(record.get(field))
    if value is None:
        raise ValueError(f"Row {row_number}: {field} is required")
    return value


def _optional_cell_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _number(value: Any, field: str, row_number: int) -> float:
    if value in (None, ""):
        if field == "threshold":
            return 0.0
        raise ValueError(f"Row {row_number}: {field} is required")
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Row {row_number}: {field} must be numeric") from exc


def _optional_number(
    value: Any,
    field: str,
    row_number: int,
) -> float | None:
    if value in (None, ""):
        return None
    return _number(value, field, row_number)

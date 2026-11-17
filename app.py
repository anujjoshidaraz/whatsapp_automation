"""
WhatsApp Automation Dashboard
Uses DingTalk Sheet as database, Playwright for WhatsApp automation.
"""
import os
import re
import subprocess
import threading
import time
import json
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, Response, session, redirect, url_for, send_from_directory
from flask_session import Session

# ─── DingTalk Sheet Config ───────────────────────────────────────────────────
SHEET_NODE_ID = "P7QG4Yx2Jp7DmjDGIql1dOpXV9dEq3XD"
GROUPS_SHEET_ID = "kgqie6hm"
HISTORY_SHEET_ID = "st-0bc3eace-196969"

# Barcode Tracker sheet (existing)
BARCODE_SHEET_NODE_ID = "EpGBa2Lm8azLbBLwTzjp705DWgN7R35y"
BARCODE_SHEET_ID = "st-52d05d2f-402912"

# Seller Sheets (consolidated under one node with subsheets)
SELLER_NODE_ID = "AR4GpnMqJzM93n94TLnqa5E5VKe0xjE3"
SELLER_MAP_SHEET_ID = "kgqie6hm"  # Subsheet: Tracking→Seller mapping
SELLER_GROUPS_SHEET_ID = "st-efab65a2-37769"  # Subsheet: Seller→WhatsApp Group mapping

# Package History Sheet (tracks when packages are loaded into dashboard)
PACKAGE_HISTORY_NODE_ID = "EpGBa2Lm8azLbBLwTzjp705DWgN7R35y"
PACKAGE_HISTORY_SHEET_ID = "st-700fb79b-272758"

# Comments Sheet (tracking-based comments)
COMMENTS_NODE_ID = "EpGBa2Lm8azLbBLwTzjp705DWgN7R35y"
COMMENTS_SHEET_ID = "st-700fb79b-272759"

# Authentication Sheet (rider credentials)
AUTH_NODE_ID = "G1DKw2zgV2RNb5N7Td5z2dpPVB5r9YAn"
AUTH_SHEET_ID = "st-301c4f0c-121671"

# Find dws-core executable (works on both Windows and Linux)
def _find_dws_core():
    """Find dws-core executable for current platform."""
    import platform
    system = platform.system().lower()
    
    if system == 'windows':
        return os.path.join(os.path.expanduser("~"), ".qoderwork", "bin", "ext", "dws-core-windows-amd64.exe")
    elif system == 'linux':
        # Check if installed in /usr/local/bin (Docker)
        linux_path = '/usr/local/bin/dws'
        if os.path.exists(linux_path):
            return linux_path
        # Fallback to home directory
        return os.path.join(os.path.expanduser("~"), ".qoderwork", "bin", "dws")
    else:
        # macOS or other
        return os.path.join(os.path.expanduser("~"), ".qoderwork", "bin", "dws")

DWS_PATH = _find_dws_core()

# ─── Database Schema Definition ──────────────────────────────────────────────
# Structured schema for all DingTalk Sheets used by the application
# Each sheet has: columns (with types), required fields, and validation rules

DATABASE_SCHEMA = {
    # Authentication Sheet - Rider credentials
    "auth": {
        "node_id": AUTH_NODE_ID,
        "sheet_id": AUTH_SHEET_ID,
        "description": "Rider login credentials",
        "columns": {
            "A": {"name": "phone", "type": "string", "required": True, "pattern": r"^\d{10}$", "description": "10-digit phone number"},
            "B": {"name": "name", "type": "string", "required": True, "description": "Rider full name"},
            "C": {"name": "password", "type": "string", "required": True, "min_length": 4, "description": "PIN/password (min 4 chars)"}
        },
        "primary_key": "phone",
        "start_row": 2
    },

    # Groups Sheet - WhatsApp groups management
    "groups": {
        "node_id": SHEET_NODE_ID,
        "sheet_id": GROUPS_SHEET_ID,
        "description": "WhatsApp groups for message sending",
        "columns": {
            "A": {"name": "group_name", "type": "string", "required": True, "description": "WhatsApp group name"},
            "B": {"name": "phone", "type": "string", "required": False, "description": "Group phone/contact number"},
            "C": {"name": "notes", "type": "string", "required": False, "description": "Additional notes"},
            "D": {"name": "status", "type": "enum", "required": True, "values": ["active", "inactive"], "default": "active", "description": "Group status"}
        },
        "primary_key": "group_name",
        "start_row": 2
    },

    # History Sheet - WhatsApp message history
    "history": {
        "node_id": SHEET_NODE_ID,
        "sheet_id": HISTORY_SHEET_ID,
        "description": "Sent WhatsApp messages history",
        "columns": {
            "A": {"name": "timestamp", "type": "datetime", "required": True, "format": "%Y-%m-%d %H:%M:%S", "description": "Message sent timestamp"},
            "B": {"name": "group_name", "type": "string", "required": True, "description": "Target WhatsApp group"},
            "C": {"name": "message", "type": "string", "required": True, "max_length": 5000, "description": "Message content"},
            "D": {"name": "status", "type": "enum", "required": True, "values": ["sent", "failed", "queued"], "description": "Delivery status"},
            "E": {"name": "error", "type": "string", "required": False, "description": "Error message if failed"}
        },
        "primary_key": "timestamp",
        "start_row": 2
    },

    # Barcode Tracker Sheet - Package scans
    "barcode_tracker": {
        "node_id": BARCODE_SHEET_NODE_ID,
        "sheet_id": BARCODE_SHEET_ID,
        "description": "Package barcode scan records",
        "columns": {
            "A": {"name": "rider_name", "type": "string", "required": True, "description": "Rider who scanned"},
            "B": {"name": "hub", "type": "string", "required": False, "description": "Hub location"},
            "C": {"name": "tracking", "type": "string", "required": True, "pattern": r"^[A-Z0-9]+$", "description": "Tracking number"},
            "D": {"name": "scanned_at", "type": "datetime", "required": True, "format": "%Y-%m-%d %H:%M:%S", "description": "Scan timestamp"},
            "E": {"name": "status", "type": "enum", "required": True, "values": ["Pending", "Delivered", "RTO", "In Transit", "Exception"], "description": "Package status"},
            "F": {"name": "reason_category", "type": "string", "required": False, "description": "Issue category"},
            "G": {"name": "sub_reason", "type": "string", "required": False, "description": "Specific reason"},
            "H": {"name": "notes", "type": "string", "required": False, "max_length": 1000, "description": "Additional notes"},
            "I": {"name": "comments", "type": "string", "required": False, "description": "Comments"}
        },
        "primary_key": "tracking",
        "start_row": 2
    },

    # Seller Tracking Map - Tracking to seller mapping
    "seller_map": {
        "node_id": SELLER_NODE_ID,
        "sheet_id": SELLER_MAP_SHEET_ID,
        "description": "Tracking number to seller name mapping",
        "columns": {
            "A": {"name": "tracking", "type": "string", "required": True, "pattern": r"^[A-Z0-9]+$", "description": "Tracking number"},
            "B": {"name": "seller_name", "type": "string", "required": True, "description": "Seller name"}
        },
        "primary_key": "tracking",
        "start_row": 2
    },

    # Seller Groups - Seller to WhatsApp group mapping
    "seller_groups": {
        "node_id": SELLER_NODE_ID,
        "sheet_id": SELLER_GROUPS_SHEET_ID,
        "description": "Seller to WhatsApp group mapping",
        "columns": {
            "A": {"name": "seller_name", "type": "string", "required": True, "description": "Seller name"},
            "B": {"name": "whatsapp_group", "type": "string", "required": True, "description": "WhatsApp group name"}
        },
        "primary_key": "seller_name",
        "start_row": 2
    },

    # Package History - Package tracking history
    "package_history": {
        "node_id": PACKAGE_HISTORY_NODE_ID,
        "sheet_id": PACKAGE_HISTORY_SHEET_ID,
        "description": "Package scan history with seller info",
        "columns": {
            "A": {"name": "tracking", "type": "string", "required": True, "pattern": r"^[A-Z0-9]+$", "description": "Tracking number"},
            "B": {"name": "rider_name", "type": "string", "required": True, "description": "Rider name"},
            "C": {"name": "seller_name", "type": "string", "required": False, "description": "Seller name"},
            "D": {"name": "status", "type": "enum", "required": True, "values": ["Pending", "Delivered", "RTO", "In Transit", "Exception"], "description": "Package status"},
            "E": {"name": "scanned_at", "type": "datetime", "required": True, "format": "%Y-%m-%d %H:%M:%S", "description": "Scan timestamp"},
            "F": {"name": "recorded_at", "type": "datetime", "required": True, "format": "%Y-%m-%d %H:%M:%S", "description": "Record creation timestamp"}
        },
        "primary_key": "tracking",
        "start_row": 2
    },

    # Comments Sheet - Tracking-based comments
    "comments": {
        "node_id": COMMENTS_NODE_ID,
        "sheet_id": COMMENTS_SHEET_ID,
        "description": "Comments on tracking numbers",
        "columns": {
            "A": {"name": "tracking", "type": "string", "required": True, "pattern": r"^[A-Z0-9]+$", "description": "Tracking number"},
            "B": {"name": "comment", "type": "string", "required": True, "max_length": 2000, "description": "Comment text"},
            "C": {"name": "author", "type": "string", "required": True, "description": "Comment author"},
            "D": {"name": "timestamp", "type": "datetime", "required": True, "format": "%Y-%m-%d %H:%M:%S", "description": "Comment timestamp"}
        },
        "primary_key": None,  # Multiple comments per tracking
        "start_row": 2
    }
}


def validate_field(sheet_name, column, value):
    """Validate a field value against the schema.
    Returns (is_valid, error_message) tuple.
    """
    schema = DATABASE_SCHEMA.get(sheet_name)
    if not schema:
        return True, None  # No schema defined, skip validation

    col_schema = schema["columns"].get(column)
    if not col_schema:
        return True, None  # Column not in schema, skip validation

    # Check required
    if col_schema.get("required") and (value is None or str(value).strip() == ""):
        return False, f"{col_schema['name']} is required"

    # Skip further validation if empty and not required
    if value is None or str(value).strip() == "":
        return True, None

    value_str = str(value).strip()

    # Type validation
    if col_schema["type"] == "string":
        if "min_length" in col_schema and len(value_str) < col_schema["min_length"]:
            return False, f"{col_schema['name']} must be at least {col_schema['min_length']} characters"
        if "max_length" in col_schema and len(value_str) > col_schema["max_length"]:
            return False, f"{col_schema['name']} must be at most {col_schema['max_length']} characters"
        if "pattern" in col_schema and not re.match(col_schema["pattern"], value_str):
            return False, f"{col_schema['name']} format is invalid"

    elif col_schema["type"] == "enum":
        if value_str not in col_schema.get("values", []):
            return False, f"{col_schema['name']} must be one of: {', '.join(col_schema['values'])}"

    elif col_schema["type"] == "datetime":
        try:
            datetime.strptime(value_str, col_schema.get("format", "%Y-%m-%d %H:%M:%S"))
        except ValueError:
            return False, f"{col_schema['name']} must be in format: {col_schema.get('format', '%Y-%m-%d %H:%M:%S')}"

    return True, None


def validate_row(sheet_name, row_data):
    """Validate a complete row against the schema.
    row_data: dict with column letters as keys (e.g., {"A": "value", "B": "value"})
    Returns (is_valid, list_of_errors) tuple.
    """
    schema = DATABASE_SCHEMA.get(sheet_name)
    if not schema:
        return True, []

    errors = []
    for col_letter, col_schema in schema["columns"].items():
        value = row_data.get(col_letter, "")
        is_valid, error_msg = validate_field(sheet_name, col_letter, value)
        if not is_valid:
            errors.append(error_msg)

    return len(errors) == 0, errors


# ─── WhatsApp Module ─────────────────────────────────────────────────────────
try:
    from whatsapp_sender import send_message_to_group, is_logged_in, setup_login, reconnect, get_connection_state
    WHATSAPP_AVAILABLE = True
    print("WhatsApp module loaded successfully")
except Exception as e:
    WHATSAPP_AVAILABLE = False
    print(f"Warning: WhatsApp module failed to load: {type(e).__name__}: {e}")

app = Flask(__name__)
app.secret_key = "whatsapp-dashboard-secret-key-2026"  # Change this in production

# ─── Persistent Session Configuration ─────────────────────────────────────────
# Store sessions on filesystem so they persist across server restarts
SESSION_DIR = os.path.join(os.path.expanduser("~"), ".qoderwork", "flask_sessions")
os.makedirs(SESSION_DIR, exist_ok=True)
app.config["SESSION_TYPE"] = "filesystem"
app.config["SESSION_FILE_DIR"] = SESSION_DIR
app.config["SESSION_PERMANENT"] = True
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=7)  # Session lasts 7 days
Session(app)

# ─── Authentication ──────────────────────────────────────────────────────────

def get_rider_credentials():
    """Read rider credentials from DingTalk Sheet.
    Sheet structure: A=Phone, B=Name, C=Password
    Returns dict: {phone: {"password": password, "name": name}}
    """
    result = run_dws(["sheet", "range", "read", "--node", AUTH_NODE_ID,
                      "--sheet-id", AUTH_SHEET_ID, "--range", "A2:C100"])
    if "error" in result:
        return {}
    cells = result.get("cells", [])
    credentials = {}
    for row in cells:
        if not row or not row[0].get("value", "").strip():
            continue
        phone = row[0].get("value", "").strip()
        rider_name = row[1].get("value", "").strip() if len(row) > 1 else ""
        password = row[2].get("value", "").strip() if len(row) > 2 else ""
        if phone and password:
            credentials[phone] = {"password": password, "name": rider_name}
    return credentials

def register_rider(phone, name, password):
    """Register a new rider in the auth sheet.
    Sheet structure: A=Phone, B=Name, C=Password
    """
    # Validate against schema
    row_data = {"A": phone, "B": name, "C": password}
    is_valid, errors = validate_row("auth", row_data)
    if not is_valid:
        return {"error": "; ".join(errors)}

    # Find next empty row
    result = run_dws(["sheet", "range", "read", "--node", AUTH_NODE_ID,
                      "--sheet-id", AUTH_SHEET_ID, "--range", "A:A"])
    next_row = 2
    if "error" not in result:
        existing = result.get("cells", [])
        for i, row in enumerate(existing):
            if not row or not row[0].get("value", "").strip():
                next_row = i + 2
                break
        else:
            next_row = len(existing) + 2

    values = [[
        {"type": "text", "text": phone},
        {"type": "text", "text": name},
        {"type": "text", "text": password}
    ]]
    write_result = run_dws(["sheet", "range", "update", "--node", AUTH_NODE_ID,
                           "--sheet-id", AUTH_SHEET_ID, "--range", f"A{next_row}:C{next_row}",
                           "--values", json.dumps(values), "--timeout", "60"])
    if "error" in write_result:
        return write_result
    return {"ok": True, "row": next_row}

def check_phone_exists(phone):
    """Check if a phone number already exists in the auth sheet."""
    credentials = get_rider_credentials()
    return phone in credentials

def login_required(f):
    """Decorator to protect routes that require authentication."""
    from functools import wraps
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get("authenticated"):
            # For API routes, return 401 JSON
            if request.path.startswith("/api/"):
                return jsonify({"error": "Unauthorized", "login_required": True}), 401
            # For page routes, redirect to login
            return redirect(url_for("login_page"))
        return f(*args, **kwargs)
    return decorated_function

# ─── Helper Functions ────────────────────────────────────────────────────────
def run_dws(args, timeout=60):
    """Run dws command and return parsed JSON."""
    cmd = [DWS_PATH] + args + ["--format", "json"]
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=timeout,
                                encoding='utf-8', errors='replace')
        stdout = result.stdout or ""
        stderr = result.stderr or ""
        if result.returncode != 0:
            return {"error": stderr.strip() or stdout.strip() or f"exit code {result.returncode}"}
        return json.loads(stdout) if stdout.strip() else {}
    except subprocess.TimeoutExpired:
        return {"error": "Command timed out"}
    except json.JSONDecodeError as e:
        return {"error": f"JSON parse error: {e}"}
    except Exception as e:
        return {"error": str(e)}

def get_groups():
    """Read all groups from the Groups sheet."""
    result = run_dws(["sheet", "range", "read", "--node", SHEET_NODE_ID, 
                      "--sheet-id", GROUPS_SHEET_ID, "--range", "A2:D100"])
    if "error" in result:
        return []
    cells = result.get("cells", [])
    groups = []
    for row in cells:
        if not row or not row[0].get("value", "").strip():
            continue
        groups.append({
            "group_name": row[0].get("value", "").strip(),
            "phone": row[1].get("value", "").strip() if len(row) > 1 else "",
            "notes": row[2].get("value", "").strip() if len(row) > 2 else "",
            "status": row[3].get("value", "").strip() if len(row) > 3 else "active"
        })
    return groups

def get_scans():
    """Read all scans from the Barcode Tracker sheet.
    Columns: A=Rider Name, B=Hub, C=Tracking Number, D=Scanned At, E=Status,
             F=Reason Category, G=Sub-Reason, H=Notes, I=Comments
    """
    result = run_dws(["sheet", "range", "read", "--node", BARCODE_SHEET_NODE_ID,
                      "--sheet-id", BARCODE_SHEET_ID, "--range", "A2:I500"])
    if "error" in result:
        return []
    cells = result.get("cells", [])
    scans = []
    for i, row in enumerate(cells):
        if not row or not row[0].get("value", "").strip():
            continue
        scans.append({
            "row": i + 2,
            "rider_name": row[0].get("value", "").strip(),
            "hub": row[1].get("value", "").strip() if len(row) > 1 else "",
            "tracking": row[2].get("value", "").strip() if len(row) > 2 else "",
            "scanned_at": row[3].get("value", "").strip() if len(row) > 3 else "",
            "status": row[4].get("value", "").strip() if len(row) > 4 else "",
            "reason_category": row[5].get("value", "").strip() if len(row) > 5 else "",
            "sub_reason": row[6].get("value", "").strip() if len(row) > 6 else "",
            "notes": row[7].get("value", "").strip() if len(row) > 7 else "",
            "comments": row[8].get("value", "").strip() if len(row) > 8 else "",
        })
    return scans


def extract_remarks(pkg):
    """Extract the rider's manual remarks from the notes field.

    Legacy rows store notes as "Category > SubReason | Remarks"; newer rows
    store only the remarks. Returns an empty string when there are none.
    """
    notes = (pkg.get("notes") or "").strip()
    if not notes:
        return ""
    cat = (pkg.get("reason_category") or "").strip()
    sub = (pkg.get("sub_reason") or "").strip()
    prefix = cat + (" > " + sub if cat and sub else "")
    if prefix and notes.startswith(prefix):
        notes = notes[len(prefix):].strip()
        notes = notes.lstrip(">-| \t:")
        pipe_idx = notes.find("|")
        if pipe_idx != -1:
            notes = notes[pipe_idx + 1:].strip()
    if not notes:
        return ""
    if notes == cat or notes == sub:
        return ""
    if cat and sub and notes == f"{cat} > {sub}":
        return ""
    return notes

def get_sent_packages():
    """Get packages that have been sent via WhatsApp, from the History sheet."""
    result = run_dws(["sheet", "range", "read", "--node", SHEET_NODE_ID,
                      "--sheet-id", HISTORY_SHEET_ID, "--range", "A2:E500"])
    if "error" in result:
        return []
    cells = result.get("cells", [])
    sent = []
    for i, row in enumerate(cells):
        if not row or not row[0].get("value", "").strip():
            continue
        status = row[3].get("value", "").strip() if len(row) > 3 else ""
        if status.lower() == "sent":
            sent.append({
                "row": i + 2,
                "timestamp": row[0].get("value", "").strip(),
                "group_name": row[1].get("value", "").strip() if len(row) > 1 else "",
                "message": row[2].get("value", "").strip() if len(row) > 2 else "",
                "status": status,
            })
    return sent

def parse_sent_messages(sent_packages):
    """Parse History sheet batch messages into individual package entries.
    Each History row contains a batch message with multiple tracking numbers.
    This extracts individual packages with their tracking, reason, and notes.
    """
    individual = []
    for sp in sent_packages:
        message = sp.get("message", "")
        lines = message.split("\n")
        for line in lines:
            line = line.strip()
            if not line or " - " not in line:
                continue
            # Skip header lines like "Packages for Seller (N items):"
            if line.lower().startswith("packages for "):
                continue
            
            # Extract tracking (before first " - ")
            dash_idx = line.index(" - ")
            tracking = line[:dash_idx].strip()
            rest = line[dash_idx + 3:]
            
            # Extract reason
            reason_category = ""
            sub_reason = ""
            reason_match = None
            for prefix in ["Reason: ", "Reason:"]:
                if prefix in rest:
                    idx = rest.index(prefix) + len(prefix)
                    reason_match = idx
                    # Reason goes until next " | " or end of line
                    end_idx = rest.find(" | ", idx)
                    reason_text = rest[idx:end_idx].strip() if end_idx > idx else rest[idx:].strip()
                    if " > " in reason_text:
                        parts = reason_text.split(" > ", 1)
                        reason_category = parts[0].strip()
                        sub_reason = parts[1].strip()
                    else:
                        reason_category = reason_text
                    break
            
            # Extract note
            note = ""
            for prefix in ["Note: ", "Note:"]:
                if prefix in rest:
                    idx = rest.index(prefix) + len(prefix)
                    note = rest[idx:].strip()
                    # Remove trailing status bracket like [Pending]
                    if " [" in note:
                        bracket_idx = note.rfind(" [")
                        note = note[:bracket_idx].strip()
                    break
            
            individual.append({
                "tracking": tracking,
                "reason_category": reason_category,
                "sub_reason": sub_reason,
                "notes": note,
                "group_name": sp.get("group_name", ""),
                "timestamp": sp.get("timestamp", ""),
                "status": "Sent",
            })
    return individual

def add_to_history(group_name, message, status, error=""):
    """Add a record to the History sheet. Appends to the next empty row."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    values = [[
        {"type": "text", "text": timestamp},
        {"type": "text", "text": group_name},
        {"type": "text", "text": message[:5000]},  # Truncate very long messages (5000 char limit)
        {"type": "text", "text": status},
        {"type": "text", "text": error[:200] if error else ""}
    ]]
    # Find next empty row by reading column A and finding the last row with data
    read_result = run_dws(["sheet", "range", "read", "--node", SHEET_NODE_ID,
                          "--sheet-id", HISTORY_SHEET_ID, "--range", "A2:A1000"])
    next_row = 2  # Default to row 2 if no data
    if "error" not in read_result:
        existing = read_result.get("cells", [])
        # Find the last row with actual data
        last_data_row = 1  # Row 1 is header, so start from 1 (meaning row 2 in sheet)
        for i, row in enumerate(existing):
            cell_value = ""
            if row and len(row) > 0:
                cell_value = row[0].get("value", "").strip()
            if cell_value:
                last_data_row = i + 2  # +2 because: i is 0-indexed, and we start from row 2
        next_row = last_data_row + 1  # Next empty row after the last data
    
    range_str = f"A{next_row}:E{next_row}"
    print(f"[HISTORY] Writing to row {next_row}: {group_name} - {message[:50]}...")
    run_dws(["sheet", "range", "update", "--node", SHEET_NODE_ID,
             "--sheet-id", HISTORY_SHEET_ID, "--range", range_str,
             "--values", json.dumps(values), "--timeout", "60"])

def get_queued_messages():
    """Get all messages with status 'queued' from History."""
    result = run_dws(["sheet", "range", "read", "--node", SHEET_NODE_ID,
                      "--sheet-id", HISTORY_SHEET_ID, "--range", "A2:E1000"])
    if "error" in result:
        return []
    cells = result.get("cells", [])
    queued = []
    for i, row in enumerate(cells):
        if not row or len(row) < 4:
            continue
        status = row[3].get("value", "").strip()
        if status == "queued":
            queued.append({
                "row": i + 2,  # Excel row number (1-indexed, +2 for header)
                "timestamp": row[0].get("value", "").strip(),
                "group_name": row[1].get("value", "").strip(),
                "message": row[2].get("value", "").strip(),
                "status": status
            })
    return queued

def update_history_status(row, status, error=""):
    """Update status and error for a specific row in History."""
    values = [[
        {"type": "text", "text": status},
        {"type": "text", "text": error[:200] if error else ""}
    ]]
    range_str = f"D{row}:E{row}"
    run_dws(["sheet", "range", "update", "--node", SHEET_NODE_ID,
             "--sheet-id", HISTORY_SHEET_ID, "--range", range_str,
             "--values", json.dumps(values), "--timeout", "60"])

def get_seller_map():
    """Read tracking→seller mappings from Seller Tracking Map sheet.
    Columns: A=Tracking Number, B=Seller Name
    Returns dict: {tracking_number: seller_name}
    """
    result = run_dws(["sheet", "range", "read", "--node", SELLER_NODE_ID,
                      "--sheet-id", SELLER_MAP_SHEET_ID, "--range", "A2:B5000"])
    if "error" in result:
        return {}
    cells = result.get("cells", [])
    mapping = {}
    for row in cells:
        if not row or not row[0].get("value", "").strip():
            continue
        tracking = row[0].get("value", "").strip()
        seller = row[1].get("value", "").strip() if len(row) > 1 else ""
        if tracking and seller:
            mapping[tracking] = seller
    return mapping

def get_seller_groups_mapping():
    """Read seller→whatsapp group mappings from Seller Groups sheet.
    Columns: A=Seller Name, B=WhatsApp Group
    Returns dict: {seller_name: whatsapp_group}
    """
    result = run_dws(["sheet", "range", "read", "--node", SELLER_NODE_ID,
                      "--sheet-id", SELLER_GROUPS_SHEET_ID, "--range", "A2:B200"])
    if "error" in result:
        return {}
    cells = result.get("cells", [])
    mapping = {}
    for row in cells:
        if not row or not row[0].get("value", "").strip():
            continue
        seller = row[0].get("value", "").strip()
        group = row[1].get("value", "").strip() if len(row) > 1 else ""
        if seller and group:
            mapping[seller] = group
    return mapping

def add_seller_group_mapping(seller_name, whatsapp_group):
    """Add or update a seller→whatsapp group mapping."""
    # Check if seller already exists
    result = run_dws(["sheet", "range", "read", "--node", SELLER_NODE_ID,
                      "--sheet-id", SELLER_GROUPS_SHEET_ID, "--range", "A2:B200"])
    target_row = None
    if "error" not in result:
        cells = result.get("cells", [])
        for i, row in enumerate(cells):
            if row and row[0].get("value", "").strip().lower() == seller_name.lower():
                target_row = i + 2  # existing row
                break
        if not target_row:
            # Find first empty row
            for i, row in enumerate(cells):
                if not row or not row[0].get("value", "").strip():
                    target_row = i + 2
                    break
            if not target_row:
                target_row = len(cells) + 2

    values = [[
        {"type": "text", "text": seller_name},
        {"type": "text", "text": whatsapp_group}
    ]]
    range_str = f"A{target_row}:B{target_row}"
    return run_dws(["sheet", "range", "update", "--node", SELLER_NODE_ID,
                    "--sheet-id", SELLER_GROUPS_SHEET_ID, "--range", range_str,
                    "--values", json.dumps(values), "--timeout", "60"])

_PKG_CACHE_TTL = 2  # seconds
_pkg_cache = {"data": None, "ts": 0}

def get_packages_with_sellers():
    """Get scanned packages enriched with seller info.
    Combines Barcode Tracker scans with Seller Tracking Map.
    Also marks packages as sent if they appear in the History sheet.
    Includes comments from the Comments sheet.
    Cached for 2 seconds to allow frequent client refreshes.
    """
    global _pkg_cache
    now = time.time()
    if _pkg_cache["data"] is not None and (now - _pkg_cache["ts"]) < _PKG_CACHE_TTL:
        return _pkg_cache["data"]

    scans = get_scans()
    seller_map = get_seller_map()
    seller_groups = get_seller_groups_mapping()
    comments_map = get_all_comments_map()

    # Get sent tracking numbers from History sheet
    sent_raw = get_sent_packages()
    sent_trackings = set()
    for sp in sent_raw:
        for line in sp.get("message", "").split("\n"):
            line = line.strip()
            if line and " - " in line and not line.lower().startswith("packages for "):
                tracking = line[:line.index(" - ")].strip()
                sent_trackings.add(tracking)
    # Debug to file
    with open("debug_log.txt", "a") as f:
        f.write(f"\n[{datetime.now()}] History entries: {len(sent_raw)}, Sent trackings: {sent_trackings}\n")

    packages = []
    for scan in scans:
        tracking = scan["tracking"]
        seller = seller_map.get(tracking, "")
        group = seller_groups.get(seller, "") if seller else ""
        pkg_comments = comments_map.get(tracking, "")
        is_sent = tracking in sent_trackings
        # Debug to file
        with open("debug_log.txt", "a") as f:
            f.write(f"  Package {tracking}: is_sent={is_sent}\n")
        packages.append({
            **scan,
            "seller_name": seller,
            "whatsapp_group": group,
            "has_seller": bool(seller),
            "has_group": bool(group),
            "is_sent": is_sent,
            "tracking_comments": pkg_comments,
        })

    # Sync to package history in background thread (non-blocking)
    threading.Thread(target=sync_package_history, daemon=True).start()

    _pkg_cache["data"] = packages
    _pkg_cache["ts"] = now
    return packages

# ─── Package History Sync ────────────────────────────────────────────────────
_sync_lock = threading.Lock()
_sync_last_run = 0

def sync_package_history():
    """Robust sync: ensures every scanned package from Barcode Tracker exists in Package History.
    Also enriches seller names from SellerMap. Runs at most once per 10 seconds.
    """
    global _sync_last_run
    now = time.time()
    if now - _sync_last_run < 10:
        return  # Throttle: don't sync more than once per 10 seconds
    if not _sync_lock.acquire(blocking=False):
        return  # Another sync is already running
    try:
        _sync_last_run = now
        _do_sync_package_history()
    except Exception as e:
        print(f"[SYNC] Package history sync error: {e}")
    finally:
        _sync_lock.release()

def _do_sync_package_history():
    """Internal: perform the actual sync logic."""
    # 1. Read all scans from Barcode Tracker
    scans = get_scans()
    if not scans:
        return

    # 2. Read seller map for enrichment
    seller_map = get_seller_map()

    # 3. Read existing Package History entries (bounded range to avoid empty-sheet errors)
    result = run_dws(["sheet", "range", "read", "--node", PACKAGE_HISTORY_NODE_ID,
                      "--sheet-id", PACKAGE_HISTORY_SHEET_ID, "--range", "A2:F5000"])
    # Use composite key (tracking|scanned_at|rider) to identify unique scans
    existing_keys = set()
    existing_by_tracking = {}  # {tracking: row_number} for seller updates
    if "error" not in result:
        cells = result.get("cells", [])
        for i, row in enumerate(cells):
            if row and row[0].get("value", "").strip():
                t = row[0].get("value", "").strip()
                r = row[1].get("value", "").strip() if len(row) > 1 else ""
                s = row[4].get("value", "").strip() if len(row) > 4 else ""
                key = f"{t}|{s}|{r}"
                existing_keys.add(key)
                existing_by_tracking[t] = i + 2  # Excel row

    # 4. Find next empty row in Package History
    next_row = 2
    if existing_by_tracking:
        next_row = max(existing_by_tracking.values()) + 1

    # 5. For each scan, check if it needs to be added
    new_rows = []  # List of (row_number, tracking, scan, seller)
    trackings_to_check_seller = []  # Trackings that exist, for seller update

    for scan in scans:
        tracking = scan["tracking"]
        if not tracking:
            continue
        seller = seller_map.get(tracking, "")
        key = f"{tracking}|{scan.get('scanned_at', '')}|{scan.get('rider_name', '')}"

        if key in existing_keys:
            # This exact scan already exists — just check seller
            if seller and tracking in existing_by_tracking:
                trackings_to_check_seller.append((existing_by_tracking[tracking], seller, tracking))
        else:
            # New scan — add to Package History
            new_rows.append((next_row, tracking, scan, seller))
            next_row += 1

    # 6. Write new rows
    for row_num, tracking, scan, seller in new_rows:
        values = [[
            {"type": "text", "text": tracking},
            {"type": "text", "text": scan.get("rider_name", "")},
            {"type": "text", "text": seller},
            {"type": "text", "text": scan.get("status", "")},
            {"type": "text", "text": scan.get("scanned_at", "")},
            {"type": "text", "text": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
        ]]
        range_str = f"A{row_num}:F{row_num}"
        write_result = run_dws(["sheet", "range", "update", "--node", PACKAGE_HISTORY_NODE_ID,
                                "--sheet-id", PACKAGE_HISTORY_SHEET_ID, "--range", range_str,
                                "--values", json.dumps(values), "--timeout", "60"])
        if "error" in write_result:
            print(f"[SYNC] Failed to write {tracking} to Package History: {write_result['error']}")
        else:
            print(f"[SYNC] Added {tracking} to Package History (row {row_num})")

    # 7. Update seller names for existing entries (only if seller changed)
    if trackings_to_check_seller:
        # Re-read to get current seller values
        result2 = run_dws(["sheet", "range", "read", "--node", PACKAGE_HISTORY_NODE_ID,
                           "--sheet-id", PACKAGE_HISTORY_SHEET_ID, "--range", "A2:C5000"])
        if "error" not in result2:
            cells2 = result2.get("cells", [])
            current_sellers = {}
            for i, row in enumerate(cells2):
                if row and row[0].get("value", "").strip():
                    t = row[0].get("value", "").strip()
                    s = row[2].get("value", "").strip() if len(row) > 2 else ""
                    current_sellers[t] = (s, i + 2)

            # Deduplicate: only update each tracking once
            updated = set()
            for row_num, seller, tracking in trackings_to_check_seller:
                if tracking in updated:
                    continue
                if tracking in current_sellers:
                    current_seller, _ = current_sellers[tracking]
                    if current_seller != seller:
                        values = [[{"type": "text", "text": seller}]]
                        range_str = f"C{row_num}:C{row_num}"
                        upd_result = run_dws(["sheet", "range", "update", "--node", PACKAGE_HISTORY_NODE_ID,
                                              "--sheet-id", PACKAGE_HISTORY_SHEET_ID, "--range", range_str,
                                              "--values", json.dumps(values), "--timeout", "60"])
                        if "error" not in upd_result:
                            print(f"[SYNC] Updated seller for {tracking}: '{current_seller}' -> '{seller}'")
                            updated.add(tracking)

    if new_rows or trackings_to_check_seller:
        print(f"[SYNC] Sync complete: {len(new_rows)} new, {len(trackings_to_check_seller)} checked for seller updates")

def get_package_history():
    """Read all entries from the Package History sheet."""
    result = run_dws(["sheet", "range", "read", "--node", PACKAGE_HISTORY_NODE_ID,
                      "--sheet-id", PACKAGE_HISTORY_SHEET_ID, "--range", "A2:F5000"])
    if "error" in result:
        return []
    cells = result.get("cells", [])
    history = []
    for row in cells:
        if not row or not row[0].get("value", "").strip():
            continue
        history.append({
            "tracking": row[0].get("value", "").strip(),
            "rider_name": row[1].get("value", "").strip() if len(row) > 1 else "",
            "seller_name": row[2].get("value", "").strip() if len(row) > 2 else "",
            "status": row[3].get("value", "").strip() if len(row) > 3 else "",
            "scanned_at": row[4].get("value", "").strip() if len(row) > 4 else "",
            "recorded_at": row[5].get("value", "").strip() if len(row) > 5 else "",
        })
    return history

def get_comments_by_tracking(tracking):
    """Get all comments for a specific tracking number from the Comments sheet.
    Columns: A=Tracking, B=Comment, C=Author, D=Timestamp
    Returns list of comment dicts.
    """
    result = run_dws(["sheet", "range", "read", "--node", COMMENTS_NODE_ID,
                      "--sheet-id", COMMENTS_SHEET_ID, "--range", "A2:D5000"])
    if "error" in result:
        return []
    cells = result.get("cells", [])
    comments = []
    for row in cells:
        if not row or not row[0].get("value", "").strip():
            continue
        if row[0].get("value", "").strip() == tracking:
            comments.append({
                "tracking": row[0].get("value", "").strip(),
                "comment": row[1].get("value", "").strip() if len(row) > 1 else "",
                "author": row[2].get("value", "").strip() if len(row) > 2 else "",
                "timestamp": row[3].get("value", "").strip() if len(row) > 3 else "",
            })
    return comments

def add_comment_by_tracking(tracking, comment, author="dashboard"):
    """Add a comment to the Comments sheet keyed by tracking number.
    Columns: A=Tracking, B=Comment, C=Author, D=Timestamp
    Returns dict with ok status.
    """
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    # Find next empty row (also verifies sheet exists)
    read_result = run_dws(["sheet", "range", "read", "--node", COMMENTS_NODE_ID,
                          "--sheet-id", COMMENTS_SHEET_ID, "--range", "A1:A1000"])
    if "error" in read_result:
        return {"error": "Comments sheet not found. Please create the Comments sheet first."}
    next_row = 2
    existing = read_result.get("cells", [])
    for i, row in enumerate(existing):
        if not row or not row[0].get("value", "").strip():
            next_row = i + 1
            break
    else:
        next_row = len(existing) + 2 if existing else 2

    values = [[
        {"type": "text", "text": tracking},
        {"type": "text", "text": comment},
        {"type": "text", "text": author},
        {"type": "text", "text": timestamp}
    ]]
    range_str = f"A{next_row}:D{next_row}"
    result = run_dws(["sheet", "range", "update", "--node", COMMENTS_NODE_ID,
                    "--sheet-id", COMMENTS_SHEET_ID, "--range", range_str,
                    "--values", json.dumps(values), "--timeout", "60"])
    if "error" in result:
        return {"error": result["error"]}
    return {"ok": True, "tracking": tracking, "comment": comment, "author": author, "timestamp": timestamp}

def get_all_comments_map():
    """Get all comments from the Comments sheet as a dict: {tracking: combined_comment_string}"""
    result = run_dws(["sheet", "range", "read", "--node", COMMENTS_NODE_ID,
                      "--sheet-id", COMMENTS_SHEET_ID, "--range", "A2:D5000"])
    if "error" in result:
        return {}
    cells = result.get("cells", [])
    comments_map = {}
    for row in cells:
        if not row or not row[0].get("value", "").strip():
            continue
        tracking = row[0].get("value", "").strip()
        comment = row[1].get("value", "").strip() if len(row) > 1 else ""
        author = row[2].get("value", "").strip() if len(row) > 2 else ""
        timestamp = row[3].get("value", "").strip() if len(row) > 3 else ""
        if tracking and comment:
            entry = f"[{timestamp}] {comment}"
            if author:
                entry += f" ({author})"
            if tracking in comments_map:
                comments_map[tracking] += "\n" + entry
            else:
                comments_map[tracking] = entry
    return comments_map

# ─── Authentication ───────────────────────────────────────────────────────────
LOGIN_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Daraz - WhatsApp Dashboard Login</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800;900&display=swap" rel="stylesheet">
    <script>
        tailwind.config = {
            theme: {
                extend: {
                    fontFamily: { sans: ['Inter', 'system-ui', 'sans-serif'] },
                    colors: {
                        brand: { 50:'#FFF7ED',100:'#FFEDD5',200:'#FDBA74',300:'#FB923C',400:'#F97316',500:'#F85C2C',600:'#EA580C',700:'#C2410C' },
                    },
                    animation: {
                        'blob': 'blob 8s infinite',
                        'fade-up': 'fadeUp 0.6s cubic-bezier(0.16,1,0.3,1)',
                        'shimmer': 'shimmer 2s linear infinite',
                    },
                    keyframes: {
                        blob: {
                            '0%,100%': { transform: 'translate(0,0) scale(1)' },
                            '33%': { transform: 'translate(30px,-40px) scale(1.1)' },
                            '66%': { transform: 'translate(-20px,20px) scale(0.95)' },
                        },
                        fadeUp: {
                            '0%': { opacity: '0', transform: 'translateY(24px) scale(0.97)' },
                            '100%': { opacity: '1', transform: 'translateY(0) scale(1)' },
                        },
                        shimmer: {
                            '0%': { backgroundPosition: '-200% 0' },
                            '100%': { backgroundPosition: '200% 0' },
                        },
                    },
                }
            }
        }
    </script>
    <style>
        /* Glassmorphism card */
        .glass-card {
            background: rgba(255,255,255,0.72);
            backdrop-filter: blur(24px) saturate(180%);
            -webkit-backdrop-filter: blur(24px) saturate(180%);
            border: 1px solid rgba(255,255,255,0.5);
        }
        /* Input autofill override */
        input:-webkit-autofill,
        input:-webkit-autofill:hover,
        input:-webkit-autofill:focus {
            -webkit-box-shadow: 0 0 0 1000px #fff inset;
            -webkit-text-fill-color: #1e1e1e;
            transition: background-color 5000s ease-in-out 0s;
        }
        /* PIN dots animation */
        @keyframes dotPulse { 0%,100%{opacity:.4} 50%{opacity:1} }
        .pin-dot { animation: dotPulse 1.5s ease-in-out infinite; }
        .pin-dot:nth-child(2) { animation-delay: .2s; }
        .pin-dot:nth-child(3) { animation-delay: .4s; }
        .pin-dot:nth-child(4) { animation-delay: .6s; }
        /* Error shake */
        @keyframes shake { 0%,100%{transform:translateX(0)} 25%{transform:translateX(-5px)} 75%{transform:translateX(5px)} }
        .shake { animation: shake 0.35s ease-in-out; }
        /* Success checkmark */
        @keyframes checkPop { 0%{transform:scale(0)} 60%{transform:scale(1.2)} 100%{transform:scale(1)} }
        .check-pop { animation: checkPop 0.4s cubic-bezier(0.16,1,0.3,1); }
        /* Gradient text */
        .gradient-text {
            background: linear-gradient(135deg, #F85C2C 0%, #FB923C 50%, #F97316 100%);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            background-clip: text;
        }
        /* Button shimmer */
        .btn-shimmer {
            background-size: 200% 100%;
            background-image: linear-gradient(90deg, #F85C2C 0%, #FB923C 25%, #F85C2C 50%, #FB923C 75%, #F85C2C 100%);
        }
        /* Floating particles */
        .particle {
            position: absolute;
            border-radius: 50%;
            pointer-events: none;
            opacity: 0.15;
        }
    </style>
</head>
<body class="min-h-screen flex items-center justify-center p-4 font-sans relative overflow-hidden bg-[#FAFAF9]">

    <!-- Animated gradient background blobs -->
    <div class="fixed inset-0 -z-10">
        <div class="absolute top-[-10%] right-[-5%] w-[500px] h-[500px] bg-brand-200/30 rounded-full blur-3xl animate-blob"></div>
        <div class="absolute bottom-[-15%] left-[-10%] w-[600px] h-[600px] bg-orange-200/25 rounded-full blur-3xl animate-blob" style="animation-delay:-3s"></div>
        <div class="absolute top-[40%] left-[30%] w-[400px] h-[400px] bg-amber-100/30 rounded-full blur-3xl animate-blob" style="animation-delay:-5s"></div>
        <div class="absolute inset-0 bg-gradient-to-br from-white/80 via-transparent to-orange-50/50"></div>
    </div>

    <!-- Main Card -->
    <div class="w-full max-w-[420px] animate-fade-up">
        <div class="glass-card rounded-3xl shadow-[0_8px_40px_rgba(248,92,44,0.10),0_2px_12px_rgba(0,0,0,0.04)] p-8 sm:p-10">

            <!-- Logo & Brand -->
            <div class="text-center mb-8">
                <div class="inline-flex items-center justify-center w-20 h-20 mb-5">
                    <img src="/logo.png" alt="Daraz" class="w-full h-full object-contain">
                </div>
                <h1 class="text-2xl font-bold text-gray-900 tracking-tight">WhatsApp Dashboard</h1>
                <p class="text-sm text-gray-500 mt-1.5">Sign in to manage your operations</p>
            </div>

            <!-- Status Badge -->
            <div class="flex items-center justify-center gap-2 bg-emerald-50 border border-emerald-200/60 rounded-full py-2 px-4 mb-7 w-fit mx-auto">
                <span class="relative flex h-2 w-2">
                    <span class="animate-ping absolute inline-flex h-full w-full rounded-full bg-emerald-400 opacity-75"></span>
                    <span class="relative inline-flex rounded-full h-2 w-2 bg-emerald-500"></span>
                </span>
                <span class="text-xs font-semibold text-emerald-700">Connected to WhatsApp Business</span>
            </div>

            <!-- Error Message -->
            <div id="errorMsg" class="hidden mb-5 bg-red-50 border border-red-200 text-red-700 text-sm font-medium px-4 py-3 rounded-xl flex items-start gap-2.5">
                <svg class="w-4 h-4 mt-0.5 shrink-0" fill="currentColor" viewBox="0 0 20 20"><path fill-rule="evenodd" d="M10 18a8 8 0 100-16 8 8 0 000 16zM8.28 7.22a.75.75 0 00-1.06 1.06L8.94 10l-1.72 1.72a.75.75 0 101.06 1.06L10 11.06l1.72 1.72a.75.75 0 101.06-1.06L11.06 10l1.72-1.72a.75.75 0 00-1.06-1.06L10 8.94 8.28 7.22z" clip-rule="evenodd"/></svg>
                <span id="errorText"></span>
            </div>

            <!-- Login Form -->
            <form id="loginForm" class="space-y-4">

                <!-- Saved Phone Display -->
                <div id="phoneDisplay" class="hidden items-center justify-between bg-gray-50 border border-gray-200 rounded-xl px-4 py-3.5 mb-1">
                    <div class="flex items-center gap-3">
                        <div class="w-9 h-9 rounded-full bg-brand-100 flex items-center justify-center">
                            <svg class="w-4.5 h-4.5 text-brand-600" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" d="M10.5 1.5H8.25A2.25 2.25 0 006 3.75v16.5a2.25 2.25 0 002.25 2.25h7.5A2.25 2.25 0 0018 20.25V3.75a2.25 2.25 0 00-2.25-2.25H13.5m-3 0V3h3V1.5m-3 0h3m-3 18.75h3"/></svg>
                        </div>
                        <span id="phoneText" class="text-sm font-semibold text-gray-800 tracking-wide"></span>
                    </div>
                    <button type="button" id="changePhoneBtn" class="text-xs font-semibold text-brand-600 hover:text-brand-700 transition-colors px-2 py-1 rounded-lg hover:bg-brand-50">Change</button>
                </div>

                <!-- Phone Input -->
                <div id="phoneGroup" class="space-y-1.5">
                    <label for="phone" class="block text-sm font-semibold text-gray-700">Phone Number</label>
                    <div class="relative">
                        <div class="absolute inset-y-0 left-0 pl-3.5 flex items-center pointer-events-none">
                            <svg class="w-4.5 h-4.5 text-gray-400" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" d="M2.25 6.75c0 8.284 6.716 15 15 15h2.25a2.25 2.25 0 002.25-2.25v-1.372c0-.516-.351-.966-.852-1.091l-4.423-1.106c-.44-.11-.902.055-1.173.417l-.97 1.293c-.282.376-.769.542-1.21.38a12.035 12.035 0 01-7.143-7.143c-.162-.441.004-.928.38-1.21l1.293-.97c.363-.271.527-.734.417-1.173L6.963 3.102a1.125 1.125 0 00-1.091-.852H4.5A2.25 2.25 0 002.25 4.5v2.25z"/></svg>
                        </div>
                        <input type="tel" id="phone" name="phone" required autocomplete="username" placeholder="e.g., 98XXXXXXXX"
                            class="w-full pl-10 pr-4 py-3 bg-white border-2 border-gray-200 rounded-xl text-sm font-medium text-gray-900 placeholder:text-gray-400 focus:outline-none focus:border-brand-500 focus:ring-4 focus:ring-brand-500/10 transition-all duration-200">
                    </div>
                </div>

                <!-- PIN Input -->
                <div class="space-y-1.5">
                    <label for="pin" class="block text-sm font-semibold text-gray-700">PIN</label>
                    <div class="relative">
                        <div class="absolute inset-y-0 left-0 pl-3.5 flex items-center pointer-events-none">
                            <svg class="w-4.5 h-4.5 text-gray-400" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" d="M16.5 10.5V6.75a4.5 4.5 0 10-9 0v3.75m-.75 11.25h10.5a2.25 2.25 0 002.25-2.25v-6.75a2.25 2.25 0 00-2.25-2.25H6.75a2.25 2.25 0 00-2.25 2.25v6.75a2.25 2.25 0 002.25 2.25z"/></svg>
                        </div>
                        <input type="password" id="pin" name="pin" required autocomplete="current-password" placeholder="Enter your PIN"
                            class="w-full pl-10 pr-4 py-3 bg-white border-2 border-gray-200 rounded-xl text-sm font-medium text-gray-900 placeholder:text-gray-400 focus:outline-none focus:border-brand-500 focus:ring-4 focus:ring-brand-500/10 transition-all duration-200 tracking-widest">
                    </div>
                </div>

                <!-- Remember Me -->
                <div id="rememberMeGroup" class="flex items-center gap-2.5 pt-1">
                    <input type="checkbox" id="rememberMe" checked
                        class="w-4 h-4 rounded border-gray-300 text-brand-500 focus:ring-brand-500/20 focus:ring-offset-0 cursor-pointer accent-brand-500">
                    <label for="rememberMe" class="text-sm text-gray-600 cursor-pointer select-none">Remember me on this device</label>
                </div>

                <!-- Login Button -->
                <button type="submit" id="loginBtn"
                    class="w-full py-3.5 px-6 rounded-xl text-white text-sm font-bold tracking-wide uppercase shadow-lg shadow-brand-500/25 hover:shadow-xl hover:shadow-brand-500/30 hover:-translate-y-0.5 active:translate-y-0 transition-all duration-200 btn-shimmer mt-2">
                    Sign In
                </button>
            </form>

            <!-- Footer -->
            <div class="mt-7 pt-5 border-t border-gray-100 text-center">
                <p class="text-xs text-gray-400"><span class="font-semibold text-brand-500">Daraz</span> Nepal &middot; WhatsApp Operations</p>
            </div>
        </div>
    </div>

    <script>
        // Remember Me functionality
        const REMEMBER_KEY = 'daraz_whatsapp_remember_phone';

        function initRememberMe() {
            const savedPhone = localStorage.getItem(REMEMBER_KEY);
            const phoneDisplay = document.getElementById('phoneDisplay');
            const phoneGroup = document.getElementById('phoneGroup');
            const phoneText = document.getElementById('phoneText');
            const phoneInput = document.getElementById('phone');
            const rememberMeGroup = document.getElementById('rememberMeGroup');

            if (savedPhone) {
                phoneText.textContent = savedPhone;
                phoneDisplay.classList.remove('hidden');
                phoneDisplay.classList.add('flex');
                phoneGroup.style.display = 'none';
                rememberMeGroup.style.display = 'none';
                phoneInput.value = savedPhone;
                document.getElementById('pin').focus();
            }

            document.getElementById('changePhoneBtn').addEventListener('click', () => {
                localStorage.removeItem(REMEMBER_KEY);
                phoneDisplay.classList.add('hidden');
                phoneDisplay.classList.remove('flex');
                phoneGroup.style.display = 'block';
                rememberMeGroup.style.display = 'flex';
                phoneInput.value = '';
                phoneInput.focus();
            });
        }

        function savePhone(phone, remember) {
            if (remember) localStorage.setItem(REMEMBER_KEY, phone);
            else localStorage.removeItem(REMEMBER_KEY);
        }

        function showError(msg) {
            const el = document.getElementById('errorMsg');
            document.getElementById('errorText').textContent = msg;
            el.classList.remove('hidden');
            el.classList.add('shake');
            setTimeout(() => el.classList.remove('shake'), 400);
        }

        function hideError() {
            document.getElementById('errorMsg').classList.add('hidden');
        }

        initRememberMe();

        document.getElementById('loginForm').addEventListener('submit', async (e) => {
            e.preventDefault();
            const btn = document.getElementById('loginBtn');
            const phone = document.getElementById('phone').value.trim();
            const pin = document.getElementById('pin').value;
            const rememberMe = document.getElementById('rememberMe').checked;

            if (!phone || !pin) {
                showError('Please enter both phone number and PIN');
                return;
            }

            btn.disabled = true;
            btn.innerHTML = '<span class="inline-flex items-center gap-2"><svg class="animate-spin h-4 w-4" viewBox="0 0 24 24"><circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4" fill="none"/><path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"/></svg>Signing in...</span>';
            btn.classList.add('opacity-90');
            hideError();

            try {
                const resp = await fetch('/api/rider/login', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({phone: phone, pin: pin})
                });
                const data = await resp.json();
                if (data.ok) {
                    savePhone(phone, rememberMe);
                    btn.innerHTML = '<span class="inline-flex items-center gap-2 check-pop"><svg class="w-5 h-5" fill="none" stroke="currentColor" stroke-width="2.5" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" d="M4.5 12.75l6 6 9-13.5"/></svg>Welcome!</span>';
                    btn.classList.remove('btn-shimmer');
                    btn.style.background = 'linear-gradient(135deg, #22C55E 0%, #16A34A 100%)';
                    btn.style.boxShadow = '0 8px 24px rgba(34,197,94,0.3)';
                    setTimeout(() => { window.location.href = '/'; }, 600);
                } else {
                    showError(data.error || 'Invalid credentials');
                    btn.disabled = false;
                    btn.innerHTML = 'Sign In';
                    btn.classList.remove('opacity-90');
                }
            } catch (err) {
                showError('Network error. Please try again.');
                btn.disabled = false;
                btn.innerHTML = 'Sign In';
                btn.classList.remove('opacity-90');
            }
        });
    </script>
</body>
</html>"""

SIGNUP_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Sign Up - Daraz WhatsApp Dashboard</title>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800&display=swap" rel="stylesheet">
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        
        body {
            font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
            min-height: 100vh;
            background: linear-gradient(135deg, #FFF5F0 0%, #FFFFFF 50%, #FFF8F5 100%);
            display: flex;
            flex-direction: column;
            align-items: center;
            justify-content: center;
            padding: 20px;
            position: relative;
            overflow: hidden;
        }
        
        body::before {
            content: '';
            position: absolute;
            top: -50%;
            right: -20%;
            width: 600px;
            height: 600px;
            background: radial-gradient(circle, rgba(248, 92, 44, 0.08) 0%, transparent 70%);
            border-radius: 50%;
            pointer-events: none;
        }
        
        body::after {
            content: '';
            position: absolute;
            bottom: -30%;
            left: -10%;
            width: 500px;
            height: 500px;
            background: radial-gradient(circle, rgba(248, 92, 44, 0.06) 0%, transparent 70%);
            border-radius: 50%;
            pointer-events: none;
        }
        
        .signup-card {
            background: white;
            border-radius: 20px;
            box-shadow: 0 20px 60px rgba(248, 92, 44, 0.12), 0 8px 24px rgba(0, 0, 0, 0.06);
            padding: 48px 44px 40px;
            width: 100%;
            max-width: 420px;
            position: relative;
            animation: cardSlideIn 0.5s cubic-bezier(0.16, 1, 0.3, 1);
            z-index: 1;
        }
        
        @keyframes cardSlideIn {
            from { opacity: 0; transform: translateY(20px) scale(0.98); }
            to { opacity: 1; transform: translateY(0) scale(1); }
        }
        
        .daraz-logo {
            text-align: center;
            margin-bottom: 24px;
        }
        
        .daraz-logo svg {
            width: 64px;
            height: 64px;
            filter: drop-shadow(0 4px 12px rgba(248, 92, 44, 0.25));
        }
        
        .signup-title {
            text-align: center;
            margin-top: 16px;
            margin-bottom: 6px;
        }
        
        .signup-title h1 {
            font-size: 22px;
            font-weight: 700;
            color: #1a1a1a;
            letter-spacing: -0.3px;
        }
        
        .signup-subtitle {
            text-align: center;
            color: #666;
            font-size: 14px;
            font-weight: 400;
            margin-bottom: 28px;
        }
        
        .form-group {
            margin-bottom: 16px;
        }
        
        .form-group label {
            display: block;
            color: #444;
            font-size: 13px;
            font-weight: 600;
            margin-bottom: 8px;
        }
        
        .form-group input {
            width: 100%;
            padding: 14px 16px;
            background: #fafafa;
            border: 2px solid #e8e8e8;
            border-radius: 12px;
            font-size: 15px;
            font-weight: 400;
            color: #333;
            transition: all 0.25s ease;
            outline: none;
        }
        
        .form-group input::placeholder {
            color: #aaa;
        }
        
        .form-group input:focus {
            background: white;
            border-color: #F85C2C;
            box-shadow: 0 0 0 4px rgba(248, 92, 44, 0.1);
        }
        
        .signup-btn {
            width: 100%;
            padding: 15px;
            background: linear-gradient(135deg, #F85C2C 0%, #E84E1B 100%);
            color: white;
            border: none;
            border-radius: 12px;
            font-size: 15px;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.25s ease;
            margin-top: 8px;
            text-transform: uppercase;
            letter-spacing: 0.8px;
            box-shadow: 0 4px 14px rgba(248, 92, 44, 0.3);
        }
        
        .signup-btn:hover {
            transform: translateY(-2px);
            box-shadow: 0 8px 20px rgba(248, 92, 44, 0.4);
        }
        
        .signup-btn:active {
            transform: translateY(0);
        }
        
        .signup-btn:disabled {
            background: #ffb899;
            cursor: not-allowed;
            transform: none;
            box-shadow: none;
        }
        
        .error-msg {
            background: linear-gradient(135deg, #fff5f5 0%, #ffe8e8 100%);
            color: #d32f2f;
            padding: 12px 16px;
            border-radius: 10px;
            font-size: 13px;
            font-weight: 500;
            margin-bottom: 20px;
            display: none;
            border: 1px solid #ffcdd2;
        }
        
        .error-msg.show {
            display: block;
            animation: shake 0.4s ease-in-out;
        }
        
        @keyframes shake {
            0%, 100% { transform: translateX(0); }
            25% { transform: translateX(-6px); }
            75% { transform: translateX(6px); }
        }
        
        .login-link {
            text-align: center;
            margin-top: 24px;
            padding-top: 20px;
            border-top: 1px solid #f0f0f0;
            font-size: 14px;
            color: #666;
        }
        
        .login-link a {
            color: #F85C2C;
            text-decoration: none;
            font-weight: 600;
        }
        
        .login-link a:hover {
            text-decoration: underline;
        }
        
        @media (max-width: 480px) {
            .signup-card {
                padding: 36px 24px 32px;
                border-radius: 16px;
            }
            
            .daraz-logo svg {
                width: 56px;
                height: 56px;
            }
            
            .signup-title h1 {
                font-size: 20px;
            }
        }
    </style>
</head>
<body>
    <div class="signup-card">
        <div class="daraz-logo">
            <svg viewBox="0 0 160 200" xmlns="http://www.w3.org/2000/svg">
                <path d="M75 5 L145 45 Q150 48 150 53 L150 155 Q150 160 145 163 L80 195 Q75 197 70 195 L15 163 Q10 160 10 155 L10 95 Q10 90 15 87 L70 57 Q75 54 75 49 Z" fill="#F85C2C"/>
                <polygon points="75,85 120,110 75,135" fill="white"/>
            </svg>
        </div>
        
        <div class="signup-title">
            <h1>Create Account</h1>
        </div>
        <p class="signup-subtitle">Daraz WhatsApp Dashboard</p>
        
        <div class="error-msg" id="errorMsg"></div>
        
        <form id="signupForm">
            <div class="form-group">
                <label for="phone">Phone Number (10 digits)</label>
                <input type="tel" id="phone" name="phone" required placeholder="e.g., 98XXXXXXXX" maxlength="10">
            </div>
            <div class="form-group">
                <label for="name">Full Name</label>
                <input type="text" id="name" name="name" required placeholder="Enter your full name">
            </div>
            <div class="form-group">
                <label for="pin">PIN</label>
                <input type="password" id="pin" name="pin" required placeholder="Create a PIN (min 4 characters)">
            </div>
            <div class="form-group">
                <label for="confirmPin">Confirm PIN</label>
                <input type="password" id="confirmPin" name="confirmPin" required placeholder="Confirm your PIN">
            </div>
            <button type="submit" class="signup-btn" id="signupBtn">Sign Up</button>
        </form>
        
        <div class="login-link">Already have an account? <a href="/login">Login here</a></div>
    </div>
    
    <script>
        document.getElementById('signupForm').addEventListener('submit', async (e) => {
            e.preventDefault();
            const btn = document.getElementById('signupBtn');
            const errorMsg = document.getElementById('errorMsg');
            const phone = document.getElementById('phone').value.trim();
            const name = document.getElementById('name').value.trim();
            const pin = document.getElementById('pin').value;
            const confirmPin = document.getElementById('confirmPin').value;

            if (!phone || !name || !pin || !confirmPin) {
                errorMsg.textContent = 'Please fill in all fields';
                errorMsg.classList.add('show');
                return;
            }

            if (!phone.match(/^\d{10}$/)) {
                errorMsg.textContent = 'Phone number must be exactly 10 digits';
                errorMsg.classList.add('show');
                return;
            }

            if (pin.length < 4) {
                errorMsg.textContent = 'PIN must be at least 4 characters';
                errorMsg.classList.add('show');
                return;
            }

            if (pin !== confirmPin) {
                errorMsg.textContent = 'PINs do not match';
                errorMsg.classList.add('show');
                return;
            }

            btn.disabled = true;
            btn.textContent = 'Signing up...';
            errorMsg.classList.remove('show');

            try {
                const resp = await fetch('/api/rider/signup', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({phone: phone, name: name, pin: pin})
                });
                const data = await resp.json();
                if (data.ok) {
                    window.location.href = '/';
                } else {
                    errorMsg.textContent = data.error || 'Signup failed';
                    errorMsg.classList.add('show');
                    btn.disabled = false;
                    btn.textContent = 'Sign Up';
                }
            } catch (err) {
                errorMsg.textContent = 'Network error. Please try again.';
                errorMsg.classList.add('show');
                btn.disabled = false;
                btn.textContent = 'Sign Up';
            }
        });
    </script>
</body>
</html>"""

@app.route("/login")
def login_page():
    return Response(LOGIN_HTML, content_type="text/html")

@app.route("/api/rider/login", methods=["POST"])
def rider_login():
    data = request.json
    phone = (data.get("phone") or "").strip()
    pin = (data.get("pin") or "").strip()
    if not phone or not pin:
        return jsonify({"ok": False, "error": "Phone and PIN required"}), 400
    credentials = get_rider_credentials()
    if phone in credentials and credentials[phone]["password"] == pin:
        session.permanent = True  # Mark session as permanent for persistence
        session["authenticated"] = True
        session["rider_name"] = credentials[phone]["name"] or phone
        session["phone"] = phone
        return jsonify({"ok": True, "rider_name": session["rider_name"], "phone": phone})
    return jsonify({"ok": False, "error": "Invalid phone or PIN"}), 401

@app.route("/api/rider/logout", methods=["POST"])
def rider_logout():
    session.clear()
    return jsonify({"ok": True})

@app.route("/api/rider/session")
def rider_session():
    if session.get("authenticated"):
        return jsonify({"ok": True, "rider_name": session.get("rider_name"), "phone": session.get("phone")})
    return jsonify({"ok": False, "authenticated": False}), 401

@app.route("/api/rider/signup", methods=["POST"])
def rider_signup():
    data = request.json
    phone = (data.get("phone") or "").strip()
    name = (data.get("name") or "").strip()
    pin = (data.get("pin") or "").strip()
    if not phone or not name or not pin:
        return jsonify({"ok": False, "error": "Phone, name, and PIN required"}), 400
    if check_phone_exists(phone):
        return jsonify({"ok": False, "error": "Phone already registered"}), 409
    result = register_rider(phone, name, pin)
    if "error" in result:
        return jsonify({"ok": False, "error": result["error"]}), 500
    # Auto-login after signup
    session.permanent = True  # Mark session as permanent for persistence
    session["authenticated"] = True
    session["rider_name"] = name
    session["phone"] = phone
    return jsonify({"ok": True, "rider_name": name, "phone": phone})

# ─── API Routes ──────────────────────────────────────────────────────────────
@app.route("/api/schema")
@login_required
def api_schema():
    """Get the database schema definition."""
    # Convert schema to serializable format
    schema_output = {}
    for sheet_name, sheet_def in DATABASE_SCHEMA.items():
        schema_output[sheet_name] = {
            "description": sheet_def["description"],
            "node_id": sheet_def["node_id"],
            "sheet_id": sheet_def["sheet_id"],
            "primary_key": sheet_def["primary_key"],
            "start_row": sheet_def["start_row"],
            "columns": {}
        }
        for col_letter, col_def in sheet_def["columns"].items():
            schema_output[sheet_name]["columns"][col_letter] = {
                "name": col_def["name"],
                "type": col_def["type"],
                "required": col_def.get("required", False),
                "description": col_def.get("description", "")
            }
            # Add type-specific constraints
            if col_def["type"] == "string":
                if "pattern" in col_def:
                    schema_output[sheet_name]["columns"][col_letter]["pattern"] = col_def["pattern"]
                if "min_length" in col_def:
                    schema_output[sheet_name]["columns"][col_letter]["min_length"] = col_def["min_length"]
                if "max_length" in col_def:
                    schema_output[sheet_name]["columns"][col_letter]["max_length"] = col_def["max_length"]
            elif col_def["type"] == "enum":
                schema_output[sheet_name]["columns"][col_letter]["values"] = col_def.get("values", [])
            elif col_def["type"] == "datetime":
                schema_output[sheet_name]["columns"][col_letter]["format"] = col_def.get("format", "")

    return jsonify({"ok": True, "schema": schema_output})


@app.route("/logo.png")
def logo():
    return send_from_directory(os.path.dirname(os.path.abspath(__file__)), "daraz_logo.png")


@app.route("/")
@login_required
def index():
    # Inject timestamp into HTML to force browser to see it as new content
    html_with_ts = HTML_PAGE.replace('</title>', f'</title>\n<!-- v{int(time.time())} -->')
    resp = Response(html_with_ts, content_type='text/html')
    resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    resp.headers['Pragma'] = 'no-cache'
    resp.headers['Expires'] = '0'
    resp.headers['ETag'] = str(time.time())
    return resp

@app.route("/api/groups")
@login_required
def api_groups():
    """Get all groups."""
    groups = get_groups()
    return jsonify({"ok": True, "groups": groups})

@app.route("/api/groups", methods=["POST"])
@login_required
def api_add_group():
    """Add a new group."""
    data = request.json
    group_name = (data.get("group_name") or "").strip()
    phone = (data.get("phone") or "").strip()
    notes = (data.get("notes") or "").strip()

    # Validate against schema
    row_data = {"A": group_name, "B": phone, "C": notes, "D": "active"}
    is_valid, errors = validate_row("groups", row_data)
    if not is_valid:
        return jsonify({"ok": False, "error": "; ".join(errors)}), 400

    # Find next empty row
    read_result = run_dws(["sheet", "range", "read", "--node", SHEET_NODE_ID,
                          "--sheet-id", GROUPS_SHEET_ID, "--range", "A:A"])
    next_row = 2
    if "error" not in read_result:
        existing = read_result.get("cells", [])
        for i, row in enumerate(existing):
            if not row or not row[0].get("value", "").strip():
                next_row = i + 1
                break
        else:
            next_row = len(existing) + 1

    values = [[
        {"type": "text", "text": group_name},
        {"type": "text", "text": phone},
        {"type": "text", "text": notes},
        {"type": "text", "text": "active"}
    ]]
    range_str = f"A{next_row}:D{next_row}"
    result = run_dws(["sheet", "range", "update", "--node", SHEET_NODE_ID,
                      "--sheet-id", GROUPS_SHEET_ID, "--range", range_str,
                      "--values", json.dumps(values), "--timeout", "60"])
    
    if "error" in result:
        return jsonify({"ok": False, "error": result["error"]}), 500
    return jsonify({"ok": True, "row": next_row})

@app.route("/api/queue", methods=["POST"])
@login_required
def api_queue_message():
    """Queue a message to be sent."""
    data = request.json
    group_names = data.get("group_names", [])
    message = (data.get("message") or "").strip()
    
    if not group_names or not message:
        return jsonify({"ok": False, "error": "group_names and message required"}), 400
    
    queued = []
    for group_name in group_names:
        add_to_history(group_name, message, "queued")
        queued.append(group_name)
    
    return jsonify({"ok": True, "queued": queued, "count": len(queued)})

@app.route("/api/queue")
@login_required
def api_get_queue():
    """Get all queued messages."""
    queued = get_queued_messages()
    return jsonify({"ok": True, "queued": queued, "count": len(queued)})

@app.route("/api/send-batch", methods=["POST"])
@login_required
def api_send_batch():
    """Send all queued messages."""
    if not WHATSAPP_AVAILABLE:
        return jsonify({"ok": False, "error": "WhatsApp module not installed"}), 500
    
    queued = get_queued_messages()
    if not queued:
        return jsonify({"ok": True, "sent": 0, "failed": 0, "message": "No queued messages"})
    
    results = {"sent": 0, "failed": 0, "details": []}
    
    for item in queued:
        group_name = item["group_name"]
        message = item["message"]
        row = item["row"]
        
        try:
            result = send_message_to_group(group_name, message)
            if result.get("success"):
                update_history_status(row, "sent")
                results["sent"] += 1
                results["details"].append({"group": group_name, "status": "sent"})
            else:
                error = result.get("error", "Unknown error")
                update_history_status(row, "failed", error)
                results["failed"] += 1
                results["details"].append({"group": group_name, "status": "failed", "error": error})
        except Exception as e:
            update_history_status(row, "failed", str(e))
            results["failed"] += 1
            results["details"].append({"group": group_name, "status": "failed", "error": str(e)})
    
    return jsonify({"ok": True, **results})

@app.route("/api/history")
@login_required
def api_history():
    """Get message history."""
    result = run_dws(["sheet", "range", "read", "--node", SHEET_NODE_ID,
                      "--sheet-id", HISTORY_SHEET_ID, "--range", "A2:E100"])
    if "error" in result:
        return jsonify({"ok": False, "error": result["error"]}), 500
    
    cells = result.get("cells", [])
    history = []
    for row in cells:
        if not row or not row[0].get("value", "").strip():
            continue
        history.append({
            "timestamp": row[0].get("value", "").strip(),
            "group_name": row[1].get("value", "").strip() if len(row) > 1 else "",
            "message": row[2].get("value", "").strip() if len(row) > 2 else "",
            "status": row[3].get("value", "").strip() if len(row) > 3 else "",
            "error": row[4].get("value", "").strip() if len(row) > 4 else ""
        })
    
    # Return most recent first
    history.reverse()
    return jsonify({"ok": True, "history": history})

@app.route("/api/whatsapp-status")
@login_required
def api_whatsapp_status():
    """Check WhatsApp connection status with detailed state."""
    if not WHATSAPP_AVAILABLE:
        return jsonify({"ok": False, "error": "WhatsApp module not installed"})
    try:
        state = get_connection_state()
        import time as _time
        # If we haven't checked recently, do a lightweight check
        if _time.time() - state["last_check"] > 60:
            logged_in = is_logged_in()
        else:
            logged_in = state["logged_in"]
        return jsonify({
            "ok": True,
            "logged_in": logged_in,
            "last_success": state["last_success"],
            "consecutive_failures": state["consecutive_failures"],
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

@app.route("/api/whatsapp-reconnect", methods=["POST"])
@login_required
def api_whatsapp_reconnect():
    """Force reconnect WhatsApp Web."""
    if not WHATSAPP_AVAILABLE:
        return jsonify({"ok": False, "error": "WhatsApp module not installed"})
    try:
        result = reconnect()
        return jsonify(result)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

@app.route("/api/scans")
@login_required
def api_scans():
    """Get all barcode scans from Barcode Tracker sheet."""
    scans = get_scans()
    return jsonify({"ok": True, "scans": scans, "count": len(scans)})

@app.route("/api/sent-packages")
@login_required
def api_sent_packages():
    """Get packages that have been sent via WhatsApp.
    Returns both raw batch entries and parsed individual packages.
    """
    raw = get_sent_packages()
    parsed = parse_sent_messages(raw)
    return jsonify({"ok": True, "packages": parsed, "count": len(parsed)})

@app.route("/api/packages")
@login_required
def api_packages():
    """Get scanned packages enriched with seller info. Supports ?seller= filter."""
    packages = get_packages_with_sellers()
    seller_filter = request.args.get("seller", "").strip()
    if seller_filter:
        packages = [p for p in packages if p["seller_name"].lower() == seller_filter.lower()]
    return jsonify({"ok": True, "packages": packages, "count": len(packages)})

@app.route("/api/package-history")
@login_required
def api_package_history():
    """Get all entries from the Package History sheet."""
    history = get_package_history()
    return jsonify({"ok": True, "history": history, "count": len(history)})

@app.route("/api/sync-history", methods=["POST"])
@login_required
def api_sync_history():
    """Manually trigger a Package History sync (bypasses throttle)."""
    global _sync_last_run
    _sync_last_run = 0  # Reset throttle so sync runs immediately
    threading.Thread(target=sync_package_history, daemon=True).start()
    return jsonify({"ok": True, "message": "Sync started"})

@app.route("/api/comments", methods=["GET"])
@login_required
def api_get_comments():
    """Get all comments for a specific tracking number.
    Query param: ?tracking=XXX
    """
    tracking = request.args.get("tracking", "").strip()
    if not tracking:
        return jsonify({"ok": False, "error": "tracking param required"}), 400
    comments = get_comments_by_tracking(tracking)
    return jsonify({"ok": True, "comments": comments, "count": len(comments)})

@app.route("/api/comments", methods=["POST"])
@login_required
def api_add_comment_tracking():
    """Add a comment keyed by tracking number to the Comments sheet.
    Body: {tracking: str, comment: str, author: str (optional)}
    """
    data = request.json
    tracking = (data.get("tracking") or "").strip()
    comment = (data.get("comment") or "").strip()
    author = (data.get("author") or "dashboard").strip()
    if not tracking or not comment:
        return jsonify({"ok": False, "error": "tracking and comment required"}), 400
    result = add_comment_by_tracking(tracking, comment, author)
    if "error" in result:
        return jsonify({"ok": False, "error": result["error"]}), 500
    _pkg_cache["data"] = None  # Invalidate cache so next refresh picks up comment
    return jsonify({"ok": True})

@app.route("/api/comment", methods=["POST"])
@login_required
def api_add_comment():
    """Add a comment to a package. Writes to column I of the Barcode Tracker sheet.
    The comment is appended with timestamp and author info.
    Body: {row: int, comment: str}
    """
    data = request.json
    row = data.get("row")
    comment = (data.get("comment") or "").strip()
    if not row or not comment:
        return jsonify({"ok": False, "error": "row and comment required"}), 400
    
    # Build the comment string with timestamp
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    new_comment = f"[{timestamp}] {comment}"
    
    # Read existing comments for this row
    result = run_dws(["sheet", "range", "read", "--node", BARCODE_SHEET_NODE_ID,
                      "--sheet-id", BARCODE_SHEET_ID, "--range", f"I{row}:I{row}"])
    existing = ""
    if "error" not in result:
        cells = result.get("cells", [])
        if cells and cells[0]:
            existing = cells[0][0].get("value", "").strip()
    
    # Append to existing comments
    if existing:
        full_comment = existing + "\n" + new_comment
    else:
        full_comment = new_comment
    
    # Write back
    values = [[{"type": "text", "text": full_comment}]]
    write_result = run_dws(["sheet", "range", "update", "--node", BARCODE_SHEET_NODE_ID,
                           "--sheet-id", BARCODE_SHEET_ID, "--range", f"I{row}:I{row}",
                           "--values", json.dumps(values), "--timeout", "60"])
    if "error" in write_result:
        return jsonify({"ok": False, "error": write_result["error"]}), 500
    
    # Also write to the tracking-based Comments sheet
    try:
        scans = get_scans()
        tracking_for_row = ""
        for s in scans:
            if s["row"] == row:
                tracking_for_row = s["tracking"]
                break
        if tracking_for_row:
            add_comment_by_tracking(tracking_for_row, comment, "dashboard")
    except Exception:
        pass  # Don't fail if cross-write fails
    
    _pkg_cache["data"] = None  # Invalidate cache so next refresh picks up comment
    return jsonify({"ok": True, "comment": full_comment})

@app.route("/api/sellers")
@login_required
def api_sellers():
    """Get unique seller names from the seller map, with package counts."""
    seller_map = get_seller_map()
    seller_groups = get_seller_groups_mapping()
    packages = get_scans()

    # Count packages per seller
    seller_counts = {}
    for pkg in packages:
        seller = seller_map.get(pkg["tracking"], "")
        if seller:
            seller_counts[seller] = seller_counts.get(seller, 0) + 1

    # Build seller list with group info
    sellers = []
    all_seller_names = sorted(set(list(seller_map.values()) + list(seller_groups.keys())))
    for name in all_seller_names:
        sellers.append({
            "name": name,
            "package_count": seller_counts.get(name, 0),
            "whatsapp_group": seller_groups.get(name, ""),
            "has_group": name in seller_groups,
        })
    return jsonify({"ok": True, "sellers": sellers})

@app.route("/api/seller-groups", methods=["GET"])
@login_required
def api_get_seller_groups():
    """Get all seller→whatsapp group mappings."""
    mapping = get_seller_groups_mapping()
    groups = [{"seller": k, "whatsapp_group": v} for k, v in mapping.items()]
    return jsonify({"ok": True, "groups": groups})

@app.route("/api/seller-groups", methods=["POST"])
@login_required
def api_set_seller_group():
    """Set or update a seller→whatsapp group mapping."""
    data = request.json
    seller = (data.get("seller_name") or "").strip()
    group = (data.get("whatsapp_group") or "").strip()
    if not seller or not group:
        return jsonify({"ok": False, "error": "seller_name and whatsapp_group required"}), 400
    result = add_seller_group_mapping(seller, group)
    if "error" in result:
        return jsonify({"ok": False, "error": result["error"]}), 500
    return jsonify({"ok": True, "seller": seller, "whatsapp_group": group})

@app.route("/api/generate-message", methods=["POST"])
@login_required
def api_generate_message():
    """Generate a WhatsApp message for selected packages without sending it.
    Used for preview/edit before sending."""
    data = request.json
    seller = (data.get("seller_name") or "").strip()
    package_rows = data.get("package_rows", [])

    if not seller or not package_rows:
        return jsonify({"ok": False, "error": "seller_name and package_rows required"}), 400

    # Get seller's WhatsApp group
    seller_groups = get_seller_groups_mapping()
    group = seller_groups.get(seller, "")
    if not group:
        return jsonify({"ok": False, "error": f"No WhatsApp group set for seller '{seller}'"}), 400

    # Build message from selected packages
    scans = get_scans()
    selected = [s for s in scans if s["row"] in package_rows]
    if not selected:
        return jsonify({"ok": False, "error": "No matching packages found"}), 404

    lines = [f"Packages for {seller} ({len(selected)} items):", ""]
    for pkg in selected:
        line = f"{pkg['tracking']} - {pkg['rider_name']} ({pkg['hub']})"
        if pkg.get('reason_category'):
            reason_str = pkg['reason_category']
            if pkg.get('sub_reason'):
                reason_str += f" > {pkg['sub_reason']}"
            line += f" | Reason: {reason_str}"
        remarks = extract_remarks(pkg)
        if remarks:
            line += f" | Note: {remarks}"
        if pkg['status']:
            line += f" [{pkg['status']}]"
        lines.append(line)

    message = "\n".join(lines)
    return jsonify({"ok": True, "message": message, "seller": seller, "group": group, "package_count": len(selected)})


@app.route("/api/send-to-seller", methods=["POST"])
@login_required
def api_send_to_seller():
    """Send selected packages to a seller's WhatsApp group."""
    if not WHATSAPP_AVAILABLE:
        return jsonify({"ok": False, "error": "WhatsApp module not installed"}), 500

    data = request.json
    seller = (data.get("seller_name") or "").strip()
    package_rows = data.get("package_rows", [])  # row numbers from Barcode Tracker
    custom_message = (data.get("message") or "").strip()  # optional edited message

    if not seller or not package_rows:
        return jsonify({"ok": False, "error": "seller_name and package_rows required"}), 400

    # Get seller's WhatsApp group
    seller_groups = get_seller_groups_mapping()
    group = seller_groups.get(seller, "")
    if not group:
        return jsonify({"ok": False, "error": f"No WhatsApp group set for seller '{seller}'"}), 400

    # Use custom message if provided, otherwise generate from selected packages
    if custom_message:
        message = custom_message
        scans = get_scans()
        selected = [s for s in scans if s["row"] in package_rows]
        if not selected:
            return jsonify({"ok": False, "error": "No matching packages found"}), 404
    else:
        # Build message from selected packages
        scans = get_scans()
        selected = [s for s in scans if s["row"] in package_rows]
        if not selected:
            return jsonify({"ok": False, "error": "No matching packages found"}), 404

        lines = [f"Packages for {seller} ({len(selected)} items):", ""]
        for pkg in selected:
            line = f"{pkg['tracking']} - {pkg['rider_name']} ({pkg['hub']})"
            if pkg.get('reason_category'):
                reason_str = pkg['reason_category']
                if pkg.get('sub_reason'):
                    reason_str += f" > {pkg['sub_reason']}"
                line += f" | Reason: {reason_str}"
            remarks = extract_remarks(pkg)
            if remarks:
                line += f" | Note: {remarks}"
            if pkg['status']:
                line += f" [{pkg['status']}]"
            lines.append(line)

        message = "\n".join(lines)

    # Send to WhatsApp group
    try:
        result = send_message_to_group(group, message)
        status = "sent" if result.get("success") else "failed"
        error = "" if result.get("success") else result.get("error", "Unknown error")

        # Log to history (this is the source of truth for sent status)
        add_to_history(group, message, status, error)

        return jsonify({"ok": result.get("success", False), "status": status,
                        "group": group, "package_count": len(selected),
                        "error": error})
    except Exception as e:
        add_to_history(group, message, "failed", str(e))
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/send-to-multiple-sellers", methods=["POST"])
@login_required
def api_send_to_multiple_sellers():
    """Send selected packages to multiple sellers' WhatsApp groups at once.
    Expects: { "packages_by_seller": { "seller1": [row1, row2], "seller2": [row3] } }
    """
    if not WHATSAPP_AVAILABLE:
        return jsonify({"ok": False, "error": "WhatsApp module not installed"}), 500

    data = request.json
    packages_by_seller = data.get("packages_by_seller", {})  # {seller: [rows]}

    if not packages_by_seller:
        return jsonify({"ok": False, "error": "packages_by_seller required"}), 400

    # Get seller groups mapping
    seller_groups = get_seller_groups_mapping()
    scans = get_scans()

    results = {"sent": 0, "failed": 0, "details": []}

    for seller, package_rows in packages_by_seller.items():
        seller = seller.strip()
        if not seller or not package_rows:
            continue

        # Get seller's WhatsApp group
        group = seller_groups.get(seller, "")
        if not group:
            results["failed"] += 1
            results["details"].append({
                "seller": seller,
                "status": "failed",
                "error": f"No WhatsApp group set for seller '{seller}'"
            })
            continue

        # Build message for this seller
        selected = [s for s in scans if s["row"] in package_rows]
        if not selected:
            results["failed"] += 1
            results["details"].append({
                "seller": seller,
                "status": "failed",
                "error": "No matching packages found"
            })
            continue

        lines = [f"Packages for {seller} ({len(selected)} items):", ""]
        for pkg in selected:
            line = f"{pkg['tracking']} - {pkg['rider_name']} ({pkg['hub']})"
            if pkg.get('reason_category'):
                reason_str = pkg['reason_category']
                if pkg.get('sub_reason'):
                    reason_str += f" > {pkg['sub_reason']}"
                line += f" | Reason: {reason_str}"
            remarks = extract_remarks(pkg)
            if remarks:
                line += f" | Note: {remarks}"
            if pkg['status']:
                line += f" [{pkg['status']}]"
            lines.append(line)

        message = "\n".join(lines)

        # Send to WhatsApp group
        try:
            result = send_message_to_group(group, message)
            status = "sent" if result.get("success") else "failed"
            error = "" if result.get("success") else result.get("error", "Unknown error")

            # Log to history
            add_to_history(group, message, status, error)

            if result.get("success"):
                results["sent"] += 1
            else:
                results["failed"] += 1

            results["details"].append({
                "seller": seller,
                "group": group,
                "status": status,
                "package_count": len(selected),
                "error": error
            })
        except Exception as e:
            add_to_history(group, message, "failed", str(e))
            results["failed"] += 1
            results["details"].append({
                "seller": seller,
                "status": "failed",
                "error": str(e)
            })

    return jsonify({
        "ok": results["failed"] == 0,
        "sent": results["sent"],
        "failed": results["failed"],
        "details": results["details"]
    })


# ─── HTML Dashboard ──────────────────────────────────────────────────────────
HTML_PAGE = r"""<!DOCTYPE html>
<html lang="en" data-theme="light">
<head>
<meta charset="UTF-8">
<meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate">
<meta http-equiv="Pragma" content="no-cache">
<meta http-equiv="Expires" content="0">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>WhatsApp Automation</title>
<style>
/* ══════════════════════════════════════════════════════════
   CSS CUSTOM PROPERTIES — THEME SYSTEM
   ═══════════════════════════════════════════════════════════ */
:root {
  --primary: #F85C2C;
  --primary-hover: #E8531F;
  --primary-light: rgba(248,92,44,0.12);
  --primary-gradient: linear-gradient(135deg, #F85C2C 0%, #FB923C 100%);
  --success: #22C55E;
  --success-bg: rgba(34,197,94,0.12);
  --danger: #EF4444;
  --danger-bg: rgba(239,68,68,0.12);
  --warning: #F59E0B;
  --warning-bg: rgba(245,158,11,0.12);
  --info: #3B82F6;
  --info-bg: rgba(59,130,246,0.12);
  --radius: 14px;
  --radius-sm: 10px;
  --radius-xs: 6px;
  --shadow: 0 2px 8px rgba(0,0,0,0.06);
  --shadow-md: 0 4px 16px rgba(0,0,0,0.08);
  --shadow-lg: 0 12px 32px rgba(0,0,0,0.12);
  --shadow-glow: 0 0 20px rgba(248,92,44,0.15);
  --transition: 0.25s cubic-bezier(0.4, 0, 0.2, 1);
  --font: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
  --glass-bg: rgba(255,255,255,0.72);
  --glass-border: rgba(255,255,255,0.5);
  --glass-blur: blur(20px) saturate(180%);
}

/* Dark theme (default) */
[data-theme="dark"] {
  --bg: #0a0a0a;
  --bg-card: #141414;
  --bg-card-hover: #1c1c1c;
  --bg-input: #161922;
  --bg-sidebar: rgba(20,20,20,0.95);
  --border: rgba(255,255,255,0.08);
  --border-light: rgba(255,255,255,0.12);
  --text: #F5F5F5;
  --text-secondary: #A3A3A3;
  --text-muted: #737373;
  --header-bg: linear-gradient(135deg, #F85C2C 0%, #FB923C 100%);
  --scrollbar-track: #141414;
  --scrollbar-thumb: #3A3D4A;
  --glass-bg: rgba(20,20,20,0.8);
  --glass-border: rgba(255,255,255,0.1);
  --glass-blur: blur(20px) saturate(180%);
}

/* Light theme */
[data-theme="light"] {
  --bg: #f7f7f7;
  --bg-card: #FFFFFF;
  --bg-card-hover: #F9FAFB;
  --bg-input: #F3F4F6;
  --bg-sidebar: rgba(255,255,255,0.95);
  --border: rgba(0,0,0,0.06);
  --border-light: rgba(0,0,0,0.1);
  --text: #171717;
  --text-secondary: #525252;
  --text-muted: #737373;
  --header-bg: linear-gradient(135deg, #F85C2C 0%, #FB923C 100%);
  --shadow: 0 2px 8px rgba(0,0,0,0.06);
  --shadow-md: 0 4px 16px rgba(0,0,0,0.08);
  --shadow-lg: 0 12px 32px rgba(0,0,0,0.12);
  --scrollbar-track: #F3F4F6;
  --scrollbar-thumb: #D1D5DB;
  --glass-bg: rgba(255,255,255,0.72);
  --glass-border: rgba(255,255,255,0.5);
  --glass-blur: blur(20px) saturate(180%);
}

/* ═══════════════════════════════════════════════════════════
   RESET & BASE
   ═══════════════════════════════════════════════════════════ */
*, *::before, *::after { margin: 0; padding: 0; box-sizing: border-box; }

html { font-size: 15px; }

body {
  font-family: var(--font);
  background: var(--bg);
  color: var(--text);
  min-height: 100vh;
  display: flex;
  transition: background var(--transition), color var(--transition);
}

/* Scrollbar */
::-webkit-scrollbar { width: 6px; height: 6px; }
::-webkit-scrollbar-track { background: var(--scrollbar-track); }
::-webkit-scrollbar-thumb { background: var(--scrollbar-thumb); border-radius: 3px; }

/* ═══════════════════════════════════════════════════════════
   SIDEBAR
   ═══════════════════════════════════════════════════════════ */
.sidebar {
  width: 250px;
  background: var(--bg-sidebar);
  backdrop-filter: var(--glass-blur);
  -webkit-backdrop-filter: var(--glass-blur);
  border-right: 1px solid var(--border);
  display: flex;
  flex-direction: column;
  position: fixed;
  top: 0; left: 0; bottom: 0;
  z-index: 100;
  transition: transform 0.3s cubic-bezier(0.4,0,0.2,1), background var(--transition), box-shadow 0.3s ease;
  box-shadow: 4px 0 24px rgba(0,0,0,0.06);
}

.sidebar::before {
  content: '';
  position: absolute;
  top: 0; left: 0; right: 0;
  width: 3px;
  background: var(--primary-gradient);
  opacity: 0.8;
}

.sidebar-header {
  padding: 22px 20px;
  display: flex;
  align-items: center;
  gap: 12px;
  border-bottom: 1px solid var(--border);
  position: relative;
}

.sidebar-header::after {
  content: '';
  position: absolute;
  bottom: -1px; left: 20px; right: 20px;
  height: 1px;
  background: linear-gradient(90deg, transparent, var(--primary-light), transparent);
}

.sidebar-logo {
  width: 40px; height: 40px;
  background: var(--primary-gradient);
  border-radius: 12px;
  display: flex; align-items: center; justify-content: center;
  flex-shrink: 0;
  box-shadow: 0 4px 12px rgba(248,92,44,0.3);
  transition: transform var(--transition);
}

.sidebar-logo:hover { transform: scale(1.05) rotate(-2deg); }

.sidebar-logo svg { width: 20px; height: 20px; fill: white; }

.sidebar-title {
  font-size: 15px;
  font-weight: 700;
  background: var(--primary-gradient);
  -webkit-background-clip: text;
  -webkit-text-fill-color: transparent;
  background-clip: text;
  line-height: 1.2;
}

.sidebar-subtitle {
  font-size: 11px;
  color: var(--text-muted);
  font-weight: 400;
}

.sidebar-nav { flex: 1; padding: 14px 12px; overflow-y: auto; }

.sidebar-nav::-webkit-scrollbar { width: 4px; }
.sidebar-nav::-webkit-scrollbar-track { background: transparent; }
.sidebar-nav::-webkit-scrollbar-thumb { background: var(--border); border-radius: 4px; }
.sidebar-nav::-webkit-scrollbar-thumb:hover { background: var(--text-muted); }

.nav-item {
  display: flex;
  align-items: center;
  gap: 11px;
  padding: 11px 14px;
  border-radius: var(--radius-sm);
  color: var(--text-secondary);
  font-size: 13.5px;
  font-weight: 500;
  cursor: pointer;
  transition: all var(--transition);
  margin-bottom: 3px;
  border: none;
  background: none;
  width: 100%;
  text-align: left;
  position: relative;
  overflow: hidden;
}

.nav-item::before {
  content: '';
  position: absolute;
  inset: 0;
  background: var(--primary-gradient);
  opacity: 0;
  transition: opacity var(--transition);
}

.nav-item:hover { background: var(--bg-card-hover); color: var(--text); }
.nav-item:hover::before { opacity: 0.04; }

.nav-item.active { color: var(--primary); font-weight: 600; }
.nav-item.active::before { opacity: 0.08; }
.nav-item.active::after {
  content: '';
  position: absolute;
  left: 0; top: 20%; bottom: 20%;
  width: 3px;
  background: var(--primary-gradient);
  border-radius: 0 3px 3px 0;
}

.nav-item svg { width: 18px; height: 18px; flex-shrink: 0; opacity: 0.7; position: relative; z-index: 1; transition: transform var(--transition); }
.nav-item:hover svg { transform: scale(1.1); }
.nav-item.active svg { opacity: 1; color: var(--primary); }

.nav-item span { position: relative; z-index: 1; }

.nav-badge {
  margin-left: auto;
  background: var(--border);
  color: var(--text-secondary);
  font-size: 11px;
  font-weight: 600;
  padding: 3px 9px;
  border-radius: 10px;
  min-width: 22px;
  text-align: center;
  position: relative;
  z-index: 1;
  transition: all var(--transition);
}

.nav-item.active .nav-badge { background: var(--primary-gradient); color: white; box-shadow: 0 2px 6px rgba(248,92,44,0.3); }

.sidebar-footer {
  padding: 16px;
  border-top: 1px solid var(--border);
  position: relative;
}

.sidebar-footer::before {
  content: '';
  position: absolute;
  top: -1px; left: 20px; right: 20px;
  height: 1px;
  background: linear-gradient(90deg, transparent, var(--border), transparent);
}

.wa-status {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12.5px;
  color: var(--text-secondary);
  padding: 10px 12px;
  background: var(--bg-input);
  border-radius: var(--radius-sm);
  border: 1px solid var(--border);
  transition: all var(--transition);
}

.wa-status:hover { border-color: var(--primary); }

.wa-dot {
  width: 8px; height: 8px;
  border-radius: 50%;
  background: var(--text-muted);
  flex-shrink: 0;
  transition: background var(--transition);
}

.wa-dot.connected { background: var(--success); box-shadow: 0 0 8px rgba(34,197,94,0.5); animation: pulse-green 2s infinite; }
.wa-dot.disconnected { background: var(--danger); box-shadow: 0 0 8px rgba(239,68,68,0.4); }

@keyframes pulse-green {
  0%, 100% { box-shadow: 0 0 8px rgba(34,197,94,0.5); }
  50% { box-shadow: 0 0 12px rgba(34,197,94,0.7); }
}

.theme-toggle {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 7px;
  margin-top: 10px;
  padding: 10px;
  background: var(--bg-input);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  cursor: pointer;
  color: var(--text-secondary);
  font-size: 12px;
  font-weight: 500;
  transition: all var(--transition);
  width: 100%;
}

.theme-toggle:hover { border-color: var(--primary); color: var(--primary); background: var(--primary-light); transform: translateY(-1px); }
.theme-toggle svg { width: 16px; height: 16px; }

/* ═══════════════════════════════════════════════════════════
   MAIN CONTENT
   ═══════════════════════════════════════════════════════════ */
.main {
  flex: 1;
  margin-left: 240px;
  min-height: 100vh;
  display: flex;
  flex-direction: column;
}

.content {
  flex: 1;
  padding: 28px;
  max-width: 1200px;
  width: 100%;
  margin: 0 auto;
}

.page-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 24px;
}

.page-title {
  font-size: 22px;
  font-weight: 700;
  color: var(--text);
}

.page-actions { display: flex; gap: 8px; }

/* ═══════════════════════════════════════════════════════════
   CARDS
   ═══════════════════════════════════════════════════════════ */
.card {
  background: var(--bg-card);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 22px;
  transition: all var(--transition);
  position: relative;
  overflow: hidden;
  backdrop-filter: blur(10px) saturate(180%);
}

.card::before {
  content: '';
  position: absolute;
  top: 0;
  left: 0;
  right: 0;
  height: 3px;
  background: var(--primary-gradient);
  opacity: 0;
  transition: opacity var(--transition);
}

.card:hover {
  transform: translateY(-2px);
  box-shadow: var(--shadow-lg);
  border-color: rgba(248,92,44,0.2);
}

.card:hover::before {
  opacity: 1;
}

.card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 18px;
}

.card-title {
  font-size: 15px;
  font-weight: 700;
  color: var(--text);
  display: flex;
  align-items: center;
  gap: 8px;
}

.card-title svg { width: 18px; height: 18px; color: var(--primary); }

.card-badge {
  background: var(--border);
  color: var(--text-secondary);
  font-size: 11px;
  font-weight: 600;
  padding: 3px 10px;
  border-radius: 10px;
  transition: all var(--transition);
  box-shadow: 0 1px 3px rgba(0,0,0,0.05);
}

/* ═══════════════════════════════════════════════════════════
   GRID LAYOUTS
   ═══════════════════════════════════════════════════════════ */
.grid-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; margin-bottom: 20px; }
.grid-3 { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 20px; margin-bottom: 20px; }

/* ═══════════════════════════════════════════════════════════
   SELLER LIST
   ═══════════════════════════════════════════════════════════ */
.seller-list { max-height: 380px; overflow-y: auto; }

.seller-item {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 12px 14px;
  border-radius: var(--radius-sm);
  margin-bottom: 6px;
  background: var(--bg-input);
  border: 1.5px solid transparent;
  cursor: pointer;
  transition: all var(--transition);
  position: relative;
}

.seller-item:hover { 
  background: var(--bg-card-hover); 
  border-color: var(--border);
  transform: translateX(2px);
}

.seller-item.active { 
  border-color: var(--primary); 
  background: var(--primary-light);
}

.seller-item.active::before {
  content: '';
  position: absolute;
  left: 0;
  top: 50%;
  transform: translateY(-50%);
  width: 3px;
  height: 60%;
  background: var(--primary-gradient);
  border-radius: 0 3px 3px 0;
}

.seller-avatar {
  width: 34px; height: 34px;
  border-radius: 50%;
  background: var(--primary-gradient);
  color: white;
  display: flex; align-items: center; justify-content: center;
  font-size: 13px;
  font-weight: 700;
  flex-shrink: 0;
  box-shadow: 0 2px 8px rgba(248,92,44,0.25);
}

.seller-info { flex: 1; min-width: 0; }
.seller-name { font-size: 13.5px; font-weight: 600; color: var(--text); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.seller-meta { font-size: 11.5px; color: var(--text-muted); margin-top: 1px; }

.seller-pkg {
  font-size: 12px;
  font-weight: 700;
  color: var(--primary);
  background: var(--primary-light);
  padding: 3px 10px;
  border-radius: 8px;
  flex-shrink: 0;
  box-shadow: 0 1px 3px rgba(248,92,44,0.15);
}

.seller-group-tag {
  font-size: 10.5px;
  padding: 3px 8px;
  border-radius: 6px;
  font-weight: 600;
  flex-shrink: 0;
  transition: all var(--transition);
}

.seller-group-tag.has-group { background: var(--success-bg); color: var(--success); box-shadow: 0 1px 3px rgba(34,197,94,0.15); }
.seller-group-tag.no-group { background: var(--bg-input); color: var(--text-muted); border: 1px dashed var(--border); }

/* ═══════════════════════════════════════════════════════════
   FORM ELEMENTS
   ═══════════════════════════════════════════════════════════ */
.form-group { margin-bottom: 14px; }
.form-label { display: block; font-size: 12px; font-weight: 600; color: var(--text-secondary); margin-bottom: 6px; text-transform: uppercase; letter-spacing: 0.5px; }

.form-row { display: flex; gap: 10px; align-items: flex-end; }

input[type="text"], input[type="number"], select {
  width: 100%;
  padding: 10px 14px;
  background: var(--bg-input);
  border: 1.5px solid var(--border);
  border-radius: var(--radius-sm);
  color: var(--text);
  font-size: 13.5px;
  font-family: var(--font);
  transition: border-color var(--transition), box-shadow var(--transition);
  outline: none;
}

input:focus, select:focus {
  border-color: var(--primary);
  box-shadow: 0 0 0 3px var(--primary-light);
}

input::placeholder { color: var(--text-muted); }

/* Buttons */
.btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  padding: 10px 18px;
  border: none;
  border-radius: var(--radius-sm);
  font-size: 13.5px;
  font-weight: 600;
  font-family: var(--font);
  cursor: pointer;
  transition: all var(--transition);
  white-space: nowrap;
}

.btn svg { width: 16px; height: 16px; }

.btn-primary { 
  background: var(--primary-gradient); 
  color: white; 
  position: relative;
  overflow: hidden;
}

.btn-primary::before {
  content: '';
  position: absolute;
  top: 0;
  left: -100%;
  width: 100%;
  height: 100%;
  background: linear-gradient(90deg, transparent, rgba(255,255,255,0.2), transparent);
  transition: left 0.5s ease;
}

.btn-primary:hover::before {
  left: 100%;
}

.btn-primary:hover { 
  transform: translateY(-2px); 
  box-shadow: 0 6px 20px rgba(248,92,44,0.35);
}

.btn-success { 
  background: linear-gradient(135deg, #22C55E 0%, #16A34A 100%); 
  color: white; 
  position: relative;
  overflow: hidden;
}

.btn-success::before {
  content: '';
  position: absolute;
  top: 0;
  left: -100%;
  width: 100%;
  height: 100%;
  background: linear-gradient(90deg, transparent, rgba(255,255,255,0.2), transparent);
  transition: left 0.5s ease;
}

.btn-success:hover::before {
  left: 100%;
}

.btn-success:hover { 
  transform: translateY(-2px); 
  box-shadow: 0 6px 20px rgba(34,197,94,0.35);
}

.btn-ghost { 
  background: transparent; 
  color: var(--text-secondary); 
  border: 1.5px solid var(--border); 
  transition: all var(--transition);
}

.btn-ghost:hover { 
  border-color: var(--primary); 
  color: var(--primary); 
  background: var(--primary-light);
  transform: translateY(-1px);
}

.btn-sm { padding: 7px 14px; font-size: 12.5px; }
.btn-lg { padding: 13px 28px; font-size: 14.5px; }

.btn:disabled { opacity: 0.4; cursor: not-allowed; transform: none !important; box-shadow: none !important; }

.btn-icon {
  width: 32px; height: 32px;
  padding: 0;
  display: flex; align-items: center; justify-content: center;
  background: var(--bg-input);
  border: 1px solid var(--border);
  border-radius: var(--radius-xs);
  color: var(--text-muted);
  cursor: pointer;
  transition: all var(--transition);
}

.btn-icon:hover { 
  color: var(--primary); 
  border-color: var(--primary); 
  background: var(--primary-light);
  transform: scale(1.08);
  box-shadow: 0 0 12px rgba(248,92,44,0.2);
}

.btn-icon svg { width: 15px; height: 15px; }

/* ═══════════════════════════════════════════════════════════
   PACKAGES
   ═══════════════════════════════════════════════════════════ */
.filter-bar {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 14px 16px;
  background: var(--bg-input);
  border-radius: var(--radius-sm);
  margin-bottom: 14px;
  flex-wrap: wrap;
  border: 1px solid var(--border);
  backdrop-filter: blur(10px) saturate(180%);
}

.filter-bar label { font-size: 12.5px; color: var(--text-muted); font-weight: 500; }

.filter-bar select { width: auto; min-width: 160px; padding: 7px 12px; font-size: 12.5px; }

.filter-checkbox {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12.5px;
  color: var(--text-secondary);
  cursor: pointer;
  margin-left: auto;
}

.filter-checkbox input { width: 15px; height: 15px; accent-color: var(--primary); cursor: pointer; }

.pkg-list { max-height: 420px; overflow-y: auto; }

.pkg-item {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 12px 14px;
  border-radius: var(--radius-sm);
  margin-bottom: 6px;
  background: var(--bg-input);
  border: 1.5px solid transparent;
  cursor: pointer;
  transition: all var(--transition);
  position: relative;
}

.pkg-item:hover { 
  background: var(--bg-card-hover); 
  border-color: var(--border);
  transform: translateX(2px);
}

.pkg-item.selected { 
  border-color: var(--primary); 
  background: var(--primary-light);
}

.pkg-item.selected::before {
  content: '';
  position: absolute;
  left: 0;
  top: 50%;
  transform: translateY(-50%);
  width: 3px;
  height: 60%;
  background: var(--primary-gradient);
  border-radius: 0 3px 3px 0;
}

.pkg-check {
  width: 20px; height: 20px;
  border: 2px solid var(--border);
  border-radius: 5px;
  display: flex; align-items: center; justify-content: center;
  flex-shrink: 0;
  transition: all var(--transition);
}

.pkg-item.selected .pkg-check {
  background: var(--primary-gradient);
  border-color: var(--primary);
  box-shadow: 0 2px 8px rgba(248,92,44,0.3);
}

.pkg-check svg { width: 12px; height: 12px; fill: white; opacity: 0; transition: opacity var(--transition); }
.pkg-item.selected .pkg-check svg { opacity: 1; }

.pkg-info { flex: 1; min-width: 0; }
.pkg-tracking { font-size: 13.5px; font-weight: 600; color: var(--text); font-family: 'SF Mono', 'Fira Code', monospace; letter-spacing: 0.3px; }
.pkg-meta { font-size: 11.5px; color: var(--text-muted); margin-top: 2px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }

.pkg-tags { display: flex; gap: 6px; flex-shrink: 0; }

.tag {
  font-size: 10.5px;
  padding: 3px 9px;
  border-radius: 6px;
  font-weight: 600;
  white-space: nowrap;
  transition: all var(--transition);
}

.tag-seller { background: var(--info-bg); color: var(--info); box-shadow: 0 1px 3px rgba(59,130,246,0.15); }
.tag-status { background: var(--bg-input); color: var(--text-secondary); border: 1px solid var(--border); }
.tag-noseller { background: var(--danger-bg); color: var(--danger); box-shadow: 0 1px 3px rgba(239,68,68,0.15); }

/* ═══════════════════════════════════════════════════════════
   SEND BAR
   ═══════════════════════════════════════════════════════════ */
.send-bar {
  display: flex;
  align-items: center;
  gap: 16px;
  padding: 16px 22px;
  background: var(--bg-card);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  margin-bottom: 20px;
  transition: all var(--transition);
  backdrop-filter: blur(10px) saturate(180%);
}

.send-bar.has-selection { 
  border-color: var(--primary); 
  box-shadow: 0 0 0 3px var(--primary-light), 0 4px 16px rgba(248,92,44,0.15);
}

.send-info { flex: 1; font-size: 13.5px; color: var(--text-secondary); }
.send-info strong { color: var(--text); }
.send-info .highlight { color: var(--primary); font-weight: 700; }

/* ═══════════════════════════════════════════════════════════
   HISTORY
   ═══════════════════════════════════════════════════════════ */
.history-list { max-height: 320px; overflow-y: auto; }

.history-item {
  padding: 14px 16px;
  border-radius: var(--radius-sm);
  margin-bottom: 8px;
  background: var(--bg-input);
  border-left: 3px solid var(--border);
  transition: all var(--transition);
}

.history-item:hover {
  transform: translateX(2px);
  background: var(--bg-card-hover);
}

.history-item.status-sent { border-left-color: var(--success); }
.history-item.status-failed { border-left-color: var(--danger); }
.history-item.status-queued { border-left-color: var(--warning); }

.history-time { font-size: 11px; color: var(--text-muted); margin-bottom: 4px; }
.history-group { font-size: 13px; font-weight: 700; color: var(--primary); margin-bottom: 4px; }
.history-msg { font-size: 12.5px; color: var(--text-secondary); line-height: 1.5; white-space: pre-line; }

.history-status {
  display: inline-block;
  margin-top: 8px;
  font-size: 11px;
  font-weight: 700;
  padding: 3px 10px;
  border-radius: 6px;
  text-transform: uppercase;
  letter-spacing: 0.5px;
  transition: all var(--transition);
}

.history-status.sent { background: var(--success-bg); color: var(--success); box-shadow: 0 1px 3px rgba(34,197,94,0.15); }
.history-status.failed { background: var(--danger-bg); color: var(--danger); box-shadow: 0 1px 3px rgba(239,68,68,0.15); }
.history-status.queued { background: var(--warning-bg); color: var(--warning); box-shadow: 0 1px 3px rgba(245,158,11,0.15); }

/* ═══════════════════════════════════════════════════════════
   MAPPING LIST
   ═══════════════════════════════════════════════════════════ */
.mapping-item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 10px 14px;
  background: var(--bg-input);
  border-radius: var(--radius-sm);
  margin-bottom: 6px;
  font-size: 13px;
  border: 1px solid transparent;
  transition: all var(--transition);
}

.mapping-item:hover {
  border-color: var(--border);
  transform: translateX(2px);
}

.mapping-seller { font-weight: 600; color: var(--primary); }
.mapping-group { color: var(--success); font-weight: 500; }
.mapping-arrow { color: var(--text-muted); margin: 0 8px; }

/* ═══════════════════════════════════════════════════════════
   EMPTY / LOADING STATES
   ═══════════════════════════════════════════════════════════ */
.empty-state {
  text-align: center;
  padding: 40px 20px;
  color: var(--text-muted);
  font-size: 13.5px;
}

.empty-state svg { width: 40px; height: 40px; margin-bottom: 12px; opacity: 0.3; }

.loading {
  text-align: center;
  padding: 24px;
  color: var(--text-muted);
  font-size: 13px;
}

.spinner {
  display: inline-block;
  width: 20px; height: 20px;
  border: 2.5px solid var(--border);
  border-top-color: var(--primary);
  border-radius: 50%;
  animation: spin 0.7s linear infinite;
  vertical-align: middle;
  margin-right: 8px;
}

@keyframes spin { to { transform: rotate(360deg); } }
@keyframes pulse {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.6; }
}

/* ═══════════════════════════════════════════════════════════
   TOAST NOTIFICATIONS
   ═══════════════════════════════════════════════════════════ */
.toast-container {
  position: fixed;
  bottom: 24px;
  right: 24px;
  z-index: 9999;
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.toast {
  padding: 14px 20px;
  border-radius: var(--radius-sm);
  color: white;
  font-size: 13.5px;
  font-weight: 500;
  box-shadow: var(--shadow-lg);
  transform: translateX(120%);
  opacity: 0;
  transition: all 0.35s cubic-bezier(0.4, 0, 0.2, 1);
  max-width: 380px;
  display: flex;
  align-items: center;
  gap: 8px;
  backdrop-filter: blur(10px) saturate(180%);
}

.toast.show { transform: translateX(0); opacity: 1; }
.toast.success { background: linear-gradient(135deg, #22C55E 0%, #16A34A 100%); }
.toast.error { background: linear-gradient(135deg, #EF4444 0%, #DC2626 100%); }
.toast.info { background: linear-gradient(135deg, #3B82F6 0%, #2563EB 100%); }

/* ═══════════════════════════════════════════════════════════
   MOBILE HAMBURGER
   ═══════════════════════════════════════════════════════════ */
.mobile-header {
  display: none;
  position: fixed;
  top: 0; left: 0; right: 0;
  height: 56px;
  background: var(--bg-card);
  border-bottom: 1px solid var(--border);
  z-index: 200;
  padding: 0 16px;
  align-items: center;
  justify-content: space-between;
  backdrop-filter: blur(20px) saturate(180%);
}

.mobile-header::before {
  content: '';
  position: absolute;
  bottom: -1px;
  left: 0;
  right: 0;
  height: 2px;
  background: var(--primary-gradient);
  opacity: 0.5;
}

.hamburger {
  width: 36px; height: 36px;
  display: flex; align-items: center; justify-content: center;
  background: none; border: none;
  color: var(--text);
  cursor: pointer;
  border-radius: var(--radius-xs);
}

.hamburger:hover { background: var(--bg-input); }
.hamburger svg { width: 22px; height: 22px; }

.overlay {
  display: none;
  position: fixed;
  inset: 0;
  background: rgba(0,0,0,0.5);
  z-index: 90;
}

.overlay.show { display: block; }

/* ═══════════════════════════════════════════════════════════
   RESPONSIVE
   ═══════════════════════════════════════════════════════════ */
@media (max-width: 900px) {
  .grid-2 { grid-template-columns: 1fr; }
  .grid-3 { grid-template-columns: 1fr; }
}

@media (max-width: 768px) {
  .sidebar { transform: translateX(-100%); }
  .sidebar.open { transform: translateX(0); box-shadow: var(--shadow-lg); }
  .main { margin-left: 0; }
  .mobile-header { display: flex; }
  .content { padding: 72px 16px 24px; }
  .form-row { flex-direction: column; }
  .form-row .btn { width: 100%; }
  .send-bar { flex-direction: column; text-align: center; }
  .send-bar .btn { width: 100%; }
  .filter-bar { flex-direction: column; align-items: stretch; }
  .filter-checkbox { margin-left: 0; }
  .pkg-tags { flex-wrap: wrap; }
}

/* ═══════════════════════════════════════════════════════════
   DASHBOARD SCAN / SENT ITEMS
   ═══════════════════════════════════════════════════════════ */
.dash-scan-item {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 11px 14px;
  border-radius: var(--radius-sm);
  margin-bottom: 5px;
  background: var(--bg-input);
  border: 1.5px solid transparent;
  transition: all var(--transition);
}

.dash-scan-item:hover { background: var(--bg-card-hover); }
.dash-scan-item.selected { border-color: var(--primary); background: var(--primary-light); }

.dash-check {
  width: 20px; height: 20px;
  border: 2px solid var(--border);
  border-radius: 5px;
  display: flex; align-items: center; justify-content: center;
  flex-shrink: 0;
  cursor: pointer;
  transition: all var(--transition);
}

.dash-check.checked {
  background: var(--primary);
  border-color: var(--primary);
}

.dash-check svg { width: 12px; height: 12px; opacity: 0; transition: opacity var(--transition); }
.dash-check.checked svg { opacity: 1; }

.dash-scan-track {
  font-size: 13px;
  font-weight: 600;
  color: var(--text);
  font-family: 'SF Mono', 'Fira Code', monospace;
  min-width: 140px;
  flex-shrink: 0;
}

.dash-scan-rider {
  font-size: 12.5px;
  color: var(--text-secondary);
  min-width: 100px;
  flex-shrink: 0;
}

.dash-scan-note {
  font-size: 12px;
  color: var(--primary);
  flex: 1;
  min-width: 0;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  font-style: italic;
}

.dash-scan-note:empty { display: none; }

.dash-scan-seller {
  font-size: 10.5px;
  padding: 2px 8px;
  border-radius: 5px;
  background: rgba(245, 114, 36, 0.1);
  color: var(--primary);
  font-weight: 600;
  white-space: nowrap;
  flex-shrink: 0;
}

.dash-scan-status {
  font-size: 10.5px;
  padding: 3px 9px;
  border-radius: 6px;
  font-weight: 600;
  white-space: nowrap;
  flex-shrink: 0;
}

.dash-scan-status.pending { background: var(--warning-bg); color: var(--warning); }
.dash-scan-status.delivered { background: var(--success-bg); color: var(--success); }
.dash-scan-status.default { background: var(--bg-input); color: var(--text-secondary); border: 1px solid var(--border); }

.dash-scan-reason {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 11px;
  padding: 4px 10px;
  border-radius: 12px;
  background: #FEF3C7;
  border: 1px solid #FCD34D;
  color: #92400E;
  font-weight: 500;
  cursor: pointer;
  transition: all 0.2s;
  flex-shrink: 0;
  max-width: 200px;
  overflow: hidden;
  text-overflow: ellipsis;
}
.dash-scan-reason:hover {
  background: #FDE68A;
  border-color: #F59E0B;
}
.dash-scan-reason.expanded {
  max-width: none;
  white-space: normal;
  background: #FEF3C7;
  border-color: #F59E0B;
}

.dash-scan-reason .reason-icon {
  width: 14px;
  height: 14px;
  flex-shrink: 0;
  opacity: 0.7;
}

.dash-scan-reason .reason-text {
  overflow: hidden;
  text-overflow: ellipsis;
}

.dash-scan-reason .sub-reason {
  opacity: 0.6;
  font-size: 10px;
}

.dash-scan-reason .sub-reason::before {
  content: '›';
  margin: 0 4px;
  opacity: 0.5;
}

[data-theme="dark"] .dash-scan-reason {
  background: linear-gradient(135deg, rgba(239, 68, 68, 0.12), rgba(245, 114, 36, 0.12));
  border-color: rgba(239, 68, 68, 0.2);
  color: #F87171;
}

.dash-scan-reason:empty { display: none; }

.dash-scan-meta {
  font-size: 11px;
  color: var(--text-muted);
  flex-shrink: 0;
}

/* ═══════════════════════════════════════════════════════════
   RIDER GROUP SECTIONS
   ═══════════════════════════════════════════════════════════ */
.rider-group {
  margin-bottom: 16px;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  overflow: hidden;
  background: var(--bg-card);
}

.rider-group-header {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 12px 16px;
  background: var(--bg-input);
  cursor: pointer;
  user-select: none;
  border-bottom: 1px solid var(--border);
  transition: background var(--transition);
}

.rider-group-header:hover {
  background: var(--bg-card-hover);
}

.rider-group-avatar {
  width: 32px;
  height: 32px;
  border-radius: 50%;
  background: var(--primary);
  color: white;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 12px;
  font-weight: 700;
  flex-shrink: 0;
}

.rider-group-name {
  font-weight: 600;
  font-size: 14px;
  flex: 1;
}

.rider-group-hub {
  font-size: 11px;
  color: var(--text-muted);
  font-weight: 500;
}

.rider-group-count {
  font-size: 11px;
  padding: 2px 8px;
  border-radius: 10px;
  background: var(--primary-light);
  color: var(--primary);
  font-weight: 600;
}

.rider-group-chevron {
  transition: transform 0.2s ease;
  color: var(--text-muted);
}

.rider-group.collapsed .rider-group-chevron {
  transform: rotate(-90deg);
}

.rider-group-body {
  max-height: 2000px;
  overflow: hidden;
  transition: max-height 0.3s ease;
}

.rider-group.collapsed .rider-group-body {
  max-height: 0;
}

/* ═══════════════════════════════════════════════════════════
   COMMENT ICON & MODAL
   ═══════════════════════════════════════════════════════════ */
.comment-icon-btn {
  position: relative;
  width: 28px;
  height: 28px;
  border-radius: 50%;
  border: none;
  background: var(--bg-card);
  color: var(--text-muted);
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  transition: all var(--transition);
  flex-shrink: 0;
}

.comment-icon-btn:hover {
  background: var(--primary-light);
  color: var(--primary);
  transform: scale(1.05);
}

.comment-icon-btn.has-comments {
  background: var(--primary-light);
  color: var(--primary);
}

.comment-icon-btn svg {
  width: 14px;
  height: 14px;
}

.comment-badge {
  position: absolute;
  top: -4px;
  right: -4px;
  min-width: 16px;
  height: 16px;
  border-radius: 8px;
  background: var(--primary);
  color: white;
  font-size: 9px;
  font-weight: 700;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 0 4px;
  border: 2px solid var(--bg-card);
}

/* Comment Modal */
.comment-modal-overlay {
  position: fixed;
  top: 0;
  left: 0;
  right: 0;
  bottom: 0;
  background: rgba(0, 0, 0, 0.5);
  backdrop-filter: blur(4px);
  z-index: 10000;
  display: flex;
  align-items: center;
  justify-content: center;
  animation: fadeIn 0.2s ease;
  opacity: 0;
  pointer-events: none;
  transition: opacity 0.2s ease;
}

.comment-modal-overlay.active {
  opacity: 1;
  pointer-events: all;
}

.comment-modal {
  background: var(--bg-card);
  border-radius: var(--radius);
  width: 90%;
  max-width: 480px;
  max-height: 80vh;
  display: flex;
  flex-direction: column;
  box-shadow: var(--shadow-lg);
  border: 1px solid var(--border);
  transform: scale(0.95);
  transition: transform 0.2s ease;
}

.comment-modal-overlay.active .comment-modal {
  transform: scale(1);
}

.comment-modal-header {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 16px 20px;
  border-bottom: 1px solid var(--border);
}

.comment-modal-header .tracking-label {
  font-family: 'SF Mono', 'Fira Code', monospace;
  font-size: 13px;
  font-weight: 600;
  color: var(--text);
  flex: 1;
  word-break: break-all;
}

.comment-modal-header .close-btn {
  width: 28px;
  height: 28px;
  border-radius: 50%;
  border: none;
  background: var(--bg-input);
  color: var(--text-secondary);
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  transition: all var(--transition);
}

.comment-modal-header .close-btn:hover {
  background: var(--danger-bg);
  color: var(--danger);
}

.comment-modal-body {
  flex: 1;
  overflow-y: auto;
  padding: 0;
}

.comment-modal-empty {
  text-align: center;
  color: var(--text-muted);
  font-size: 13px;
  padding: 32px 20px;
}

/* Comments Section Header */
.comments-section-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 16px 20px 12px;
  border-bottom: 1px solid var(--border);
}

.comments-section-header .comments-title {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
  font-weight: 700;
  color: var(--primary);
  letter-spacing: 0.5px;
  text-transform: uppercase;
}

.comments-section-header .comments-title svg {
  width: 16px;
  height: 16px;
}

.comments-add-btn {
  display: flex;
  align-items: center;
  gap: 4px;
  padding: 6px 14px;
  border-radius: var(--radius-sm);
  border: none;
  background: var(--primary-light);
  color: var(--primary);
  font-size: 12px;
  font-weight: 600;
  cursor: pointer;
  transition: all var(--transition);
}

.comments-add-btn:hover {
  background: var(--primary);
  color: white;
}

.comments-add-btn svg {
  width: 14px;
  height: 14px;
}

/* Comment Entry - Screenshot Style */
.comment-entry {
  display: flex;
  align-items: flex-start;
  gap: 12px;
  padding: 14px 20px;
  border-bottom: 1px solid var(--border);
  transition: background var(--transition);
}

.comment-entry:last-child {
  border-bottom: none;
}

.comment-entry:hover {
  background: rgba(0, 0, 0, 0.02);
}

.comment-avatar {
  width: 36px;
  height: 36px;
  border-radius: 50%;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 12px;
  font-weight: 700;
  color: white;
  flex-shrink: 0;
  letter-spacing: 0.5px;
}

.comment-avatar.staff {
  background: #F57224;
}

.comment-avatar.rider {
  background: #22C55E;
}

.comment-avatar.seller {
  background: #3B82F6;
}

.comment-content {
  flex: 1;
  min-width: 0;
}

.comment-author-name {
  font-size: 13px;
  font-weight: 700;
  color: var(--text);
  margin-bottom: 3px;
  line-height: 1.3;
}

.comment-text {
  font-size: 13px;
  color: var(--text-secondary);
  line-height: 1.5;
  word-break: break-word;
}

.comment-timestamp {
  display: flex;
  align-items: center;
  gap: 4px;
  margin-top: 5px;
  font-size: 11px;
  color: var(--text-muted);
}

.comment-timestamp svg {
  width: 12px;
  height: 12px;
  flex-shrink: 0;
}

.comment-modal-footer {
  padding: 12px 20px;
  border-top: 1px solid var(--border);
  display: flex;
  gap: 8px;
}

.comment-modal-footer input {
  flex: 1;
  font-size: 13px;
  padding: 10px 14px;
  border-radius: var(--radius-sm);
  border: 1px solid var(--border);
  background: var(--bg-input);
  color: var(--text);
  outline: none;
  font-family: var(--font);
}

.comment-modal-footer input:focus {
  border-color: var(--primary);
  box-shadow: 0 0 0 3px var(--primary-light);
}

.comment-modal-footer button {
  padding: 10px 18px;
  font-size: 13px;
  font-weight: 600;
  border-radius: var(--radius-sm);
  border: none;
  background: var(--primary);
  color: white;
  cursor: pointer;
  transition: background var(--transition);
}

.comment-modal-footer button:hover {
  background: var(--primary-hover);
}

.comment-modal-footer button:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

/* Reason Modal */
.reason-modal-overlay {
  position: fixed;
  top: 0;
  left: 0;
  right: 0;
  bottom: 0;
  background: rgba(0, 0, 0, 0.5);
  backdrop-filter: blur(4px);
  z-index: 10000;
  display: flex;
  align-items: center;
  justify-content: center;
  animation: fadeIn 0.2s ease;
  opacity: 0;
  pointer-events: none;
  transition: opacity 0.2s ease;
}
.reason-modal-overlay.active {
  opacity: 1;
  pointer-events: all;
}
.reason-modal {
  background: var(--bg-card);
  border-radius: var(--radius);
  width: 90%;
  max-width: 500px;
  padding: 24px;
  box-shadow: var(--shadow-lg);
  border: 1px solid var(--border);
  transform: scale(0.95);
  transition: transform 0.2s ease;
}
.reason-modal-overlay.active .reason-modal {
  transform: scale(1);
}
.reason-modal-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 16px;
  padding-bottom: 12px;
  border-bottom: 1px solid var(--border);
}
.reason-modal-header h3 {
  margin: 0;
  font-size: 14px;
  font-weight: 600;
  color: var(--text);
  display: flex;
  align-items: center;
  gap: 8px;
}
.reason-modal-header .close-btn {
  width: 28px;
  height: 28px;
  border-radius: 50%;
  border: none;
  background: var(--bg-input);
  color: var(--text-secondary);
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  transition: all var(--transition);
}
.reason-modal-header .close-btn:hover {
  background: var(--danger-bg);
  color: var(--danger);
}
.reason-modal-text {
  font-size: 14px;
  line-height: 1.6;
  color: var(--text);
  word-break: break-word;
  white-space: pre-wrap;
}

/* Clickable tracking number */
.dash-scan-track.clickable {
  cursor: pointer;
  transition: color var(--transition);
}

.dash-scan-track.clickable:hover {
  color: var(--primary);
  text-decoration: underline;
}

/* ═══════════════════════════════════════════════════════════
   DASHBOARD TABLE LAYOUT
   ═══════════════════════════════════════════════════════════ */
.dash-table-wrap {
  overflow-x: auto;
  overflow-y: auto;
  max-height: 420px;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
}

.dash-table-wrap::-webkit-scrollbar { width: 6px; height: 6px; }
.dash-table-wrap::-webkit-scrollbar-track { background: var(--bg-input); }
.dash-table-wrap::-webkit-scrollbar-thumb { background: var(--border); border-radius: 3px; }
.dash-table-wrap::-webkit-scrollbar-thumb:hover { background: var(--text-muted); }

.dash-table-wrap-full {
  overflow-x: auto;
  overflow-y: auto;
  max-height: calc(100vh - 280px);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
}

.dash-table-wrap-full::-webkit-scrollbar { width: 6px; height: 6px; }
.dash-table-wrap-full::-webkit-scrollbar-track { background: var(--bg-input); }
.dash-table-wrap-full::-webkit-scrollbar-thumb { background: var(--border); border-radius: 3px; }
.dash-table-wrap-full::-webkit-scrollbar-thumb:hover { background: var(--text-muted); }

.dash-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
  white-space: nowrap;
}

.dash-table thead th {
  position: sticky;
  top: 0;
  z-index: 2;
  background: var(--bg-input);
  color: var(--text-secondary);
  font-size: 11px;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.5px;
  padding: 10px 12px;
  text-align: left;
  border-bottom: 2px solid var(--border);
}

.dash-table thead th.center { text-align: center; }

.dash-table tbody tr {
  transition: background var(--transition);
}

.dash-table tbody tr:nth-child(even) {
  background: var(--bg-input);
}

.dash-table tbody tr:hover {
  background: var(--bg-card-hover);
}

.dash-table tbody td {
  padding: 9px 12px;
  border-bottom: 1px solid var(--border-light);
  vertical-align: middle;
}

.dash-table .td-tracking {
  font-family: 'SF Mono', 'Fira Code', monospace;
  font-weight: 600;
  color: var(--text);
  cursor: pointer;
  transition: color var(--transition);
}

.dash-table .td-tracking:hover {
  color: var(--primary);
  text-decoration: underline;
}

.dash-table .td-center { text-align: center; }

.btn-export {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 4px 10px;
  font-size: 11px;
  font-weight: 600;
  border: 1px solid var(--border);
  border-radius: var(--radius-xs);
  background: var(--bg-input);
  color: var(--text-secondary);
  cursor: pointer;
  transition: all var(--transition);
}

.btn-export:hover {
  border-color: var(--primary);
  color: var(--primary);
  background: var(--primary-light);
}

.btn-export svg { width: 13px; height: 13px; }

/* Author mapping handled via avatar classes */

/* ═══════════════════════════════════════════════════════════
   SECTION VISIBILITY (for nav switching)
   ═══════════════════════════════════════════════════════════ */
.section { display: none; animation: fadeIn 0.25s ease; }
.section.active { display: block; }

@keyframes fadeIn {
  from { opacity: 0; transform: translateY(8px); }
  to { opacity: 1; transform: translateY(0); }
}
</style>
</head>
<body>

<!-- Mobile Header -->
<div class="mobile-header">
  <button class="hamburger" onclick="toggleSidebar()">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 12h18M3 6h18M3 18h18"/></svg>
  </button>
  <span style="font-weight:700;font-size:15px;">WA Automation</span>
  <div class="wa-dot" id="wa-dot-mobile" style="width:10px;height:10px;border-radius:50%;"></div>
</div>
<div class="overlay" id="overlay" onclick="toggleSidebar()"></div>

<!-- Sidebar -->
<aside class="sidebar" id="sidebar">
  <div class="sidebar-header">
    <div class="sidebar-logo">
      <svg viewBox="0 0 24 24"><path d="M17.472 14.382c-.297-.149-1.758-.867-2.03-.967-.273-.099-.471-.148-.67.15-.197.297-.767.966-.94 1.164-.173.199-.347.223-.644.075-.297-.15-1.255-.463-2.39-1.475-.883-.788-1.48-1.761-1.653-2.059-.173-.297-.018-.458.13-.606.134-.133.298-.347.446-.52.149-.174.198-.298.298-.497.099-.198.05-.371-.025-.52-.075-.149-.669-1.612-.916-2.207-.242-.579-.487-.5-.669-.51-.173-.008-.371-.01-.57-.01-.198 0-.52.074-.792.372-.272.297-1.04 1.016-1.04 2.479 0 1.462 1.065 2.875 1.213 3.074.149.198 2.096 3.2 5.077 4.487.709.306 1.262.489 1.694.625.712.227 1.36.195 1.871.118.571-.085 1.758-.719 2.006-1.413.248-.694.248-1.289.173-1.413-.074-.124-.272-.198-.57-.347z"/><path d="M12 2C6.477 2 2 6.477 2 12c0 1.89.525 3.66 1.438 5.168L2 22l4.832-1.438A9.955 9.955 0 0012 22c5.523 0 10-4.477 10-10S17.523 2 12 2zm0 18a8 8 0 01-4.243-1.212l-.304-.182-2.868.852.852-2.868-.182-.304A8 8 0 1112 20z"/></svg>
    </div>
    <div>
      <div class="sidebar-title">WA Automation</div>
      <div class="sidebar-subtitle">Daraz Logistics</div>
    </div>
  </div>

  <nav class="sidebar-nav">
    <button class="nav-item active" data-section="dashboard" onclick="switchSection('dashboard', this)">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/></svg>
      Dashboard
    </button>
    <button class="nav-item" data-section="sellers" onclick="switchSection('sellers', this)">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M17 21v-2a4 4 0 00-4-4H5a4 4 0 00-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 00-3-3.87"/><path d="M16 3.13a4 4 0 010 7.75"/></svg>
      Sellers
      <span class="nav-badge" id="nav-seller-count">0</span>
    </button>
    <button class="nav-item" data-section="mappings" onclick="switchSection('mappings', this)">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M10 13a5 5 0 007.54.54l3-3a5 5 0 00-7.07-7.07l-1.72 1.71"/><path d="M14 11a5 5 0 00-7.54-.54l-3 3a5 5 0 007.07 7.07l1.71-1.71"/></svg>
      Group Mappings
    </button>
    <button class="nav-item" data-section="history" onclick="switchSection('history', this)">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>
      History
    </button>
    <button class="nav-item" data-section="sent" onclick="switchSection('sent', this)">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M22 11.08V12a10 10 0 11-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/></svg>
      Sent
      <span class="nav-badge" id="nav-sent-count">0</span>
    </button>
  </nav>

  <div class="sidebar-footer">
    <div class="wa-status">
      <div class="wa-dot" id="wa-dot"></div>
      <span id="wa-status-text">Checking...</span>
      <button onclick="reconnectWhatsApp()" id="wa-reconnect-btn" title="Reconnect WhatsApp" style="background:none;border:none;cursor:pointer;padding:2px;margin-left:6px;color:var(--text-muted);display:flex;align-items:center;" onmouseover="this.style.color='var(--primary)'" onmouseout="this.style.color='var(--text-muted)'">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="width:14px;height:14px"><polyline points="23 4 23 10 17 10"/><path d="M20.49 15a9 9 0 11-2.12-9.36L23 10"/></svg>
      </button>
    </div>
    <button class="theme-toggle" onclick="toggleTheme()">
      <svg id="theme-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="5"/><line x1="12" y1="1" x2="12" y2="3"/><line x1="12" y1="21" x2="12" y2="23"/><line x1="4.22" y1="4.22" x2="5.64" y2="5.64"/><line x1="18.36" y1="18.36" x2="19.78" y2="19.78"/><line x1="1" y1="12" x2="3" y2="12"/><line x1="21" y1="12" x2="23" y2="12"/><line x1="4.22" y1="19.78" x2="5.64" y2="18.36"/><line x1="18.36" y1="5.64" x2="19.78" y2="4.22"/></svg>
      <span id="theme-label">Dark Mode</span>
    </button>
  </div>
</aside>

<!-- Main Content -->
<div class="main">
  <div class="content">

    <!-- ═══ DASHBOARD SECTION ══ -->
    <div class="section active" id="section-dashboard">
      <div class="page-header">
        <div class="page-title">Dashboard</div>
        <div class="page-actions">
          <button class="btn btn-ghost btn-sm" onclick="refreshAll()">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="width:14px;height:14px"><polyline points="23 4 23 10 17 10"/><path d="M20.49 15a9 9 0 11-2.12-9.36L23 10"/></svg>
            Refresh
          </button>
        </div>
      </div>

      <!-- Stats Row -->
      <div class="grid-3" style="margin-bottom:20px;">
        <div class="card" style="text-align:center;">
          <div style="font-size:28px;font-weight:800;color:var(--primary);" id="stat-packages">0</div>
          <div style="font-size:12px;color:var(--text-muted);margin-top:4px;font-weight:500;">Total Packages</div>
        </div>
        <div class="card" style="text-align:center;">
          <div style="font-size:28px;font-weight:800;color:var(--info);" id="stat-sellers">0</div>
          <div style="font-size:12px;color:var(--text-muted);margin-top:4px;font-weight:500;">Sellers</div>
        </div>
        <div class="card" style="text-align:center;">
          <div style="font-size:28px;font-weight:800;color:var(--success);" id="stat-sent">0</div>
          <div style="font-size:12px;color:var(--text-muted);margin-top:4px;font-weight:500;">Sent via WA</div>
        </div>
      </div>

      <!-- Recent Scans -->
      <div class="card" style="margin-bottom:20px;">
        <div class="card-header">
          <div class="card-title">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 16V8a2 2 0 00-1-1.73l-7-4a2 2 0 00-2 0l-7 4A2 2 0 002 8v8a2 2 0 001 1.73l7 4a2 2 0 002 0l7-4A2 2 0 0022 16z"/></svg>
            Recent Scans
          </div>
          <div style="display:flex;align-items:center;gap:10px;">
            <select id="dash-seller-filter" onchange="filterDashScans()" style="font-size:12px;padding:4px 8px;border-radius:6px;border:1px solid var(--border);background:var(--bg-input);color:var(--text);min-width:130px;max-width:180px;">
              <option value="">All Sellers</option>
            </select>
            <button class="btn-export" onclick="exportRecentScans()" title="Export as CSV">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15v4a2 2 0 01-2 2H5a2 2 0 01-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
              CSV
            </button>
            <span class="card-badge" id="recent-scan-count">0</span>
          </div>
        </div>
        <div id="recent-scans-list">
          <div class="loading"><span class="spinner"></span>Loading...</div>
        </div>
        <div style="display:flex;align-items:center;gap:12px;margin-top:14px;padding-top:14px;border-top:1px solid var(--border);">
          <div style="flex:1;font-size:13px;color:var(--text-secondary);" id="dash-send-info">Select packages above to send via WhatsApp</div>
          <button class="btn btn-success btn-sm" id="dash-send-btn" onclick="sendDashSelected()" disabled>
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="width:15px;height:15px"><line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/></svg>
            Send Selected
          </button>
        </div>
      </div>

      <!-- Sent Packages -->
      <div class="card">
        <div class="card-header">
          <div class="card-title">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M22 11.08V12a10 10 0 11-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/></svg>
            Sent Packages
          </div>
          <span class="card-badge" id="sent-pkg-count">0</span>
          <button class="btn-export" onclick="exportSentPackages()" title="Export as CSV">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15v4a2 2 0 01-2 2H5a2 2 0 01-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
            CSV
          </button>
        </div>
        <div id="sent-packages-list">
          <div class="loading"><span class="spinner"></span>Loading...</div>
        </div>
      </div>
    </div>

    <!-- ═══ SELLERS SECTION ═══ -->
    <div class="section" id="section-sellers">
      <div class="page-header">
        <div class="page-title">Sellers</div>
        <div class="page-actions">
          <button class="btn btn-ghost btn-sm" onclick="loadSellers()">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="width:14px;height:14px"><polyline points="23 4 23 10 17 10"/><path d="M20.49 15a9 9 0 11-2.12-9.36L23 10"/></svg>
            Refresh
          </button>
        </div>
      </div>

      <div class="card">
        <div class="card-header">
          <div class="card-title">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M17 21v-2a4 4 0 00-4-4H5a4 4 0 00-4 4v2"/><circle cx="9" cy="7" r="4"/></svg>
            All Sellers
          </div>
          <span class="card-badge" id="sellers-count">0</span>
        </div>
        <div class="seller-list" id="sellers-list">
          <div class="loading"><span class="spinner"></span>Loading sellers...</div>
        </div>
      </div>
    </div>

    <!-- ═══ MAPPINGS SECTION ═══ -->
    <div class="section" id="section-mappings">
      <div class="page-header">
        <div class="page-title">Group Mappings</div>
      </div>

      <div class="grid-2">
        <div class="card">
          <div class="card-header">
            <div class="card-title">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M10 13a5 5 0 007.54.54l3-3a5 5 0 00-7.07-7.07l-1.72 1.71"/><path d="M14 11a5 5 0 00-7.54-.54l-3 3a5 5 0 007.07 7.07l1.71-1.71"/></svg>
              Add Mapping
            </div>
          </div>
          <p style="font-size:12.5px;color:var(--text-muted);margin-bottom:16px;line-height:1.5;">
            Set the WhatsApp group name for each seller. Packages will be sent to this group automatically.
          </p>
          <div class="form-group">
            <label class="form-label">Seller Name</label>
            <input type="text" id="sg-seller" placeholder="Enter seller name">
          </div>
          <div class="form-group">
            <label class="form-label">WhatsApp Group Name</label>
            <input type="text" id="sg-group" placeholder="Enter WhatsApp group name">
          </div>
          <button class="btn btn-primary" onclick="setSellerGroup()" style="width:100%;">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="width:16px;height:16px"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>
            Save Mapping
          </button>
        </div>

        <div class="card">
          <div class="card-header">
            <div class="card-title">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M9 11l3 3L22 4"/><path d="M21 12v7a2 2 0 01-2 2H5a2 2 0 01-2-2V5a2 2 0 012-2h11"/></svg>
              Existing Mappings
            </div>
            <span class="card-badge" id="mappings-count">0</span>
          </div>
          <div id="sg-existing">
            <div class="loading"><span class="spinner"></span>Loading...</div>
          </div>
        </div>
      </div>
    </div>

    <!-- ═══ HISTORY SECTION ═══ -->
    <div class="section" id="section-history">
      <div class="page-header">
        <div class="page-title">Send History</div>
        <div class="page-actions">
          <button class="btn btn-ghost btn-sm" onclick="loadHistory()">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="width:14px;height:14px"><polyline points="23 4 23 10 17 10"/><path d="M20.49 15a9 9 0 11-2.12-9.36L23 10"/></svg>
            Refresh
          </button>
        </div>
      </div>

      <div class="card">
        <div class="history-list" id="history-list">
          <div class="loading"><span class="spinner"></span>Loading history...</div>
        </div>
      </div>
    </div>

    <!-- ═══ SENT PACKAGES SECTION ═══ -->
    <div class="section" id="section-sent">
      <div class="page-header">
        <div class="page-title">Sent Packages</div>
        <div class="page-actions">
          <button class="btn btn-ghost btn-sm" onclick="loadSentPackages()">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="width:14px;height:14px"><polyline points="23 4 23 10 17 10"/><path d="M20.49 15a9 9 0 11-2.12-9.36L23 10"/></svg>
            Refresh
          </button>
        </div>
      </div>

      <div class="card">
        <div class="card-header">
          <div class="card-title">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M22 11.08V12a10 10 0 11-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/></svg>
            All Sent Packages
          </div>
          <span class="card-badge" id="sent-full-count">0</span>
        </div>
        <div id="sent-packages-full-list">
          <div class="loading"><span class="spinner"></span>Loading sent packages...</div>
        </div>
      </div>
    </div>

  </div>
</div>

<!-- Toast Container -->
<div class="toast-container" id="toast-container"></div>

<script>
/* ═══════════════════════════════════════════════════════════
   STATE
   ═══════════════════════════════════════════════════════════ */
let sellers = [];
let allPackages = [];
let sentPackagesData = [];
let dashSelectedRows = new Set();

/* ═══════════════════════════════════════════════════════════
   THEME
   ═══════════════════════════════════════════════════════════ */
function toggleTheme() {
  const html = document.documentElement;
  const isDark = html.getAttribute('data-theme') === 'dark';
  html.setAttribute('data-theme', isDark ? 'light' : 'dark');
  document.getElementById('theme-label').textContent = isDark ? 'Dark Mode' : 'Light Mode';
  const icon = document.getElementById('theme-icon');
  icon.innerHTML = isDark
    ? '<circle cx="12" cy="12" r="5"/><line x1="12" y1="1" x2="12" y2="3"/><line x1="12" y1="21" x2="12" y2="23"/><line x1="4.22" y1="4.22" x2="5.64" y2="5.64"/><line x1="18.36" y1="18.36" x2="19.78" y2="19.78"/><line x1="1" y1="12" x2="3" y2="12"/><line x1="21" y1="12" x2="23" y2="12"/><line x1="4.22" y1="19.78" x2="5.64" y2="18.36"/><line x1="18.36" y1="5.64" x2="19.78" y2="4.22"/>'
    : '<path d="M21 12.79A9 9 0 1111.21 3 7 7 0 0021 12.79z"/>';
}

/* ═══════════════════════════════════════════════════════════
   NAVIGATION
   ═══════════════════════════════════════════════════════════ */
function switchSection(name, el) {
  document.querySelectorAll('.section').forEach(s => s.classList.remove('active'));
  document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
  document.getElementById('section-' + name).classList.add('active');
  if (el) el.classList.add('active');
  // Close mobile sidebar
  document.getElementById('sidebar').classList.remove('open');
  document.getElementById('overlay').classList.remove('show');
}

function toggleSidebar() {
  document.getElementById('sidebar').classList.toggle('open');
  document.getElementById('overlay').classList.toggle('show');
}

/* ═══════════════════════════════════════════════════════════
   TOAST
   ═══════════════════════════════════════════════════════════ */
function toast(msg, type) {
  const container = document.getElementById('toast-container');
  const t = document.createElement('div');
  t.className = 'toast ' + (type || 'success');
  const icons = {
    success: '<svg viewBox="0 0 24 24" fill="none" stroke="white" stroke-width="2.5" style="width:18px;height:18px;flex-shrink:0"><polyline points="20 6 9 17 4 12"/></svg>',
    error: '<svg viewBox="0 0 24 24" fill="none" stroke="white" stroke-width="2.5" style="width:18px;height:18px;flex-shrink:0"><circle cx="12" cy="12" r="10"/><line x1="15" y1="9" x2="9" y2="15"/><line x1="9" y1="9" x2="15" y2="15"/></svg>',
    info: '<svg viewBox="0 0 24 24" fill="none" stroke="white" stroke-width="2.5" style="width:18px;height:18px;flex-shrink:0"><circle cx="12" cy="12" r="10"/><line x1="12" y1="16" x2="12" y2="12"/><line x1="12" y1="8" x2="12.01" y2="8"/></svg>'
  };
  t.innerHTML = (icons[type] || icons.info) + '<span>' + msg + '</span>';
  container.appendChild(t);
  requestAnimationFrame(() => { t.classList.add('show'); });
  setTimeout(() => { t.classList.remove('show'); setTimeout(() => t.remove(), 350); }, 3500);
}

/* ═══════════════════════════════════════════════════════════
   DATA LOADING
   ═══════════════════════════════════════════════════════════ */
async function loadSellers() {
  const res = await fetch('/api/sellers');
  const data = await res.json();
  if (data.ok) {
    sellers = data.sellers;
    document.getElementById('sellers-count').textContent = sellers.length;
    document.getElementById('nav-seller-count').textContent = sellers.length;
    document.getElementById('stat-sellers').textContent = sellers.length;
    renderSellers();
    updateSellerFilter();
  }
}

async function loadPackages() {
  const res = await fetch('/api/packages');
  const data = await res.json();
  if (data.ok) {
    allPackages = data.packages;
    document.getElementById('stat-packages').textContent = data.count;
    renderRecentScans();
  }
}

function filterDashScans() {
  dashSelectedRows.clear();
  updateDashSendBar();
  renderRecentScans();
}

async function loadSellerGroups() {
  const res = await fetch('/api/seller-groups');
  const data = await res.json();
  const el = document.getElementById('sg-existing');
  document.getElementById('mappings-count').textContent = data.groups ? data.groups.length : 0;
  if (data.ok && data.groups && data.groups.length > 0) {
    el.innerHTML = data.groups.map(g =>
      '<div class="mapping-item">' +
      '<span class="mapping-seller">' + escapeHtml(g.seller) + '</span>' +
      '<span class="mapping-arrow">&rarr;</span>' +
      '<span class="mapping-group">' + escapeHtml(g.whatsapp_group) + '</span>' +
      '</div>'
    ).join('');
  } else {
    el.innerHTML = '<div class="empty-state">No mappings yet. Add your first seller-group mapping.</div>';
  }
}

async function loadHistory() {
  const res = await fetch('/api/history');
  const data = await res.json();
  const list = document.getElementById('history-list');
  if (!data.ok || !data.history || data.history.length === 0) {
    list.innerHTML = '<div class="empty-state"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg><br>No messages sent yet.</div>';
    return;
  }
  list.innerHTML = data.history.map(h =>
    '<div class="history-item status-' + h.status + '">' +
    '<div class="history-time">' + h.timestamp + '</div>' +
    '<div class="history-group">' + escapeHtml(h.group_name) + '</div>' +
    '<div class="history-msg">' + escapeHtml(h.message.substring(0, 250)) + (h.message.length > 250 ? '...' : '') + '</div>' +
    '<span class="history-status ' + h.status + '">' + h.status + '</span>' +
    (h.error ? '<span style="color:var(--danger);font-size:11px;margin-left:10px;">' + escapeHtml(h.error) + '</span>' : '') +
    '</div>'
  ).join('');
}

async function checkWhatsApp() {
  try {
    const res = await fetch('/api/whatsapp-status');
    const data = await res.json();
    const dots = [document.getElementById('wa-dot'), document.getElementById('wa-dot-mobile')];
    const text = document.getElementById('wa-status-text');
    if (data.ok && data.logged_in) {
      dots.forEach(d => { if(d) d.className = 'wa-dot connected'; });
      text.textContent = 'WhatsApp Connected';
    } else if (data.ok && data.consecutive_failures > 0) {
      dots.forEach(d => { if(d) d.className = 'wa-dot disconnected'; });
      text.textContent = 'WhatsApp Connection Lost';
    } else {
      dots.forEach(d => { if(d) d.className = 'wa-dot disconnected'; });
      text.textContent = 'WhatsApp Disconnected';
    }
  } catch (e) {
    console.warn('WhatsApp status check failed:', e);
  }
}

// Auto-poll WhatsApp status every 60 seconds
setInterval(checkWhatsApp, 60000);

async function reconnectWhatsApp() {
  const btn = document.getElementById('wa-reconnect-btn');
  const text = document.getElementById('wa-status-text');
  const origText = text.textContent;
  text.textContent = 'Reconnecting...';
  btn.style.pointerEvents = 'none';
  btn.innerHTML = '<span class="spinner" style="border-color:rgba(245,114,36,0.3);border-top-color:var(--primary);width:12px;height:12px;"></span>';
  try {
    const res = await fetch('/api/whatsapp-reconnect', {method: 'POST'});
    const data = await res.json();
    if (data.ok && data.logged_in) {
      toast('WhatsApp reconnected successfully');
    } else if (data.ok) {
      toast(data.message || 'WhatsApp loaded but not logged in — scan QR if needed', 'info');
    } else {
      toast('Reconnect failed: ' + (data.error || 'Unknown error'), 'error');
    }
  } catch (e) {
    toast('Reconnect error: ' + e.message, 'error');
  }
  btn.style.pointerEvents = '';
  btn.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="width:14px;height:14px"><polyline points="23 4 23 10 17 10"/><path d="M20.49 15a9 9 0 11-2.12-9.36L23 10"/></svg>';
  checkWhatsApp();
}

function refreshAll() {
  // Full page reload with cache bust to ensure fresh content
  window.location.reload(true);
}

/* ══════════════════════════════════════════════════════════
   RENDERING
   ═══════════════════════════════════════════════════════════ */
function escapeHtml(str) {
  const div = document.createElement('div');
  div.textContent = str || '';
  return div.innerHTML;
}

function getInitials(name) {
  return name.split(' ').map(w => w[0]).join('').substring(0, 2).toUpperCase();
}

function renderSellers() {
  const list = document.getElementById('sellers-list');
  if (sellers.length === 0) {
    list.innerHTML = '<div class="empty-state"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M17 21v-2a4 4 0 00-4-4H5a4 4 0 00-4 4v2"/><circle cx="9" cy="7" r="4"/></svg><br>No sellers found. Add tracking-to-seller mappings in the Seller Tracking Map sheet.</div>';
    return;
  }
  list.innerHTML = sellers.map(s =>
    '<div class="seller-item" onclick="filterBySeller(\'' + s.name.replace(/'/g, "\\'") + '\')">' +
    '<div class="seller-avatar">' + getInitials(s.name) + '</div>' +
    '<div class="seller-info">' +
    '<div class="seller-name">' + escapeHtml(s.name) + '</div>' +
    '<div class="seller-meta">' + s.package_count + ' package' + (s.package_count !== 1 ? 's' : '') + '</div>' +
    '</div>' +
    '<div class="seller-pkg">' + s.package_count + '</div>' +
    (s.has_group
      ? '<div class="seller-group-tag has-group">' + escapeHtml(s.whatsapp_group) + '</div>'
      : '<div class="seller-group-tag no-group">No group</div>') +
    '</div>'
  ).join('');
}

function getRemarks(p) {
  let notes = (p.notes || '').trim();
  if (!notes) return '';
  const cat = (p.reason_category || '').trim();
  const sub = (p.sub_reason || '').trim();
  const prefix = (cat ? cat : '') + (cat && sub ? ' > ' : '') + sub;
  if (prefix && notes.indexOf(prefix) === 0) {
    // Legacy combined format: "Category > SubReason | Remarks"
    notes = notes.slice(prefix.length).trim();
    notes = notes.replace(/^[>\-\s|:]+/, '').trim();
    const pipeIdx = notes.indexOf('|');
    if (pipeIdx >= 0) notes = notes.slice(pipeIdx + 1).trim();
  }
  if (!notes) return '';
  // Skip when it only repeats the category / sub-reason
  if (notes === cat || notes === sub) return '';
  if (cat && sub && notes === cat + ' > ' + sub) return '';
  return notes;
}

function buildReasonHtml(p) {
  if (!p.reason_category && !p.notes) return '';
  const parts = [];
  if (p.reason_category) parts.push(p.reason_category);
  if (p.sub_reason) parts.push(p.sub_reason);
  const reasonText = parts.join(' \u203A ');
  const remarks = getRemarks(p);
  const fullText = remarks ? reasonText + ' \u203A ' + remarks : reasonText;
  const escapedFullText = escapeHtml(fullText).replace(/'/g, "\\'");
  return '<div class="dash-scan-reason" onclick="event.stopPropagation();openReasonModal(\'' + escapedFullText + '\')" title="Click to view full details">' +
    '<svg class="reason-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>' +
    '<span class="reason-text">' + escapeHtml(fullText) + '</span></div>';
}

function getLastActivity(p) {
  let last = p.scanned_at || '';
  const comments = p.tracking_comments || p.comments || '';
  if (comments) {
    const lines = comments.split('\n').filter(l => l.trim());
    for (let i = lines.length - 1; i >= 0; i--) {
      const m = lines[i].match(/^\[([^\]]+)\]/);
      if (m && m[1] > last) { last = m[1]; break; }
    }
  }
  return last;
}

function parseActivityDate(s) {
  if (!s) return 0;
  // Handle formats: "2026/9/29 16:23:00", "2026/10/2 11:52:01", "2026-09-29 16:23:00"
  const d = new Date(s.replace(/(\d{4})\/(\d{1,2})\/(\d{1,2})/, '$1-$2-$3'));
  return isNaN(d.getTime()) ? 0 : d.getTime();
}

// ── New Activity Tracking ──────────────────────────────────────────────────
// Track when comments were last viewed per package using localStorage
function getLastViewedTime(tracking) {
  try {
    const viewed = JSON.parse(localStorage.getItem('bt_last_comment_view') || '{}');
    return viewed[tracking] || 0;
  } catch(e) { return 0; }
}

function markPackageViewed(tracking) {
  try {
    const viewed = JSON.parse(localStorage.getItem('bt_last_comment_view') || '{}');
    viewed[tracking] = Date.now();
    localStorage.setItem('bt_last_comment_view', JSON.stringify(viewed));
  } catch(e) {}
}

function hasNewActivity(p) {
  const comments = (p.tracking_comments || p.comments || '').trim();
  if (!comments) return false;
  
  // Only show NEW on the most recent scan for this tracking number
  if (!isMostRecentScan(p)) return false;
  
  const lastViewed = getLastViewedTime(p.tracking);
  if (lastViewed === 0) return true; // Never viewed = all comments are new
  
  // Parse comment timestamps and check if any are newer than lastViewed
  const lines = comments.split('\n').filter(l => l.trim());
  for (let i = lines.length - 1; i >= 0; i--) {
    const m = lines[i].match(/^\[([^\]]+)\]/);
    if (m) {
      const commentTime = parseActivityDate(m[1]);
      if (commentTime > lastViewed) return true;
    }
  }
  return false;
}

function isMostRecentScan(pkg) {
  // Find all packages with the same tracking number
  const sameTracking = allPackages.filter(p => p.tracking === pkg.tracking);
  if (sameTracking.length <= 1) return true; // Only one scan, so this is the most recent
  
  // Find the one with the latest scanned_at timestamp
  let mostRecent = null;
  let mostRecentTime = 0;
  sameTracking.forEach(p => {
    const t = parseActivityDate(p.scanned_at);
    if (t > mostRecentTime) {
      mostRecentTime = t;
      mostRecent = p;
    }
  });
  
  // Return true only if this package is the most recent one
  return mostRecent && mostRecent.row === pkg.row;
}

function renderRecentScans() {
  const list = document.getElementById('recent-scans-list');
  const filterVal = (document.getElementById('dash-seller-filter') || {}).value || '';
  // Show packages that have NOT been sent via WhatsApp, PLUS sent packages that have NEW activity (new comments)
  // This ensures that when a rider comments on an old sent package, it reappears here with the new comment indicator
  let unsent = allPackages.filter(p => !p.is_sent || hasNewActivity(p));
  if (filterVal) {
    unsent = unsent.filter(p => p.seller_name === filterVal);
  }
  // Sort: packages with NEW activity first, then by activity date
  unsent.sort((a, b) => {
    const aNew = hasNewActivity(a);
    const bNew = hasNewActivity(b);
    // If one has NEW activity and the other doesn't, prioritize the one with NEW
    if (aNew && !bNew) return -1;
    if (!aNew && bNew) return 1;
    // Otherwise sort by activity date
    return parseActivityDate(getLastActivity(b)) - parseActivityDate(getLastActivity(a));
  });
  document.getElementById('recent-scan-count').textContent = unsent.length;
  if (unsent.length === 0) {
    list.innerHTML = '<div class="empty-state">' + (filterVal ? 'No pending packages for this seller.' : 'No pending packages.') + '</div>';
    return;
  }
  const allChecked = unsent.every(p => dashSelectedRows.has(p.row));
  let html = '<div class="dash-table-wrap"><table class="dash-table"><thead><tr>' +
    '<th class="center"><input type="checkbox" ' + (allChecked ? 'checked' : '') + ' onchange="toggleAllDashChecks(this.checked)" style="accent-color:var(--primary);cursor:pointer;" /></th>' +
    '<th>Tracking</th>' +
    '<th>Rider</th>' +
    '<th>Seller</th>' +
    '<th>Group</th>' +
    '<th>Reason</th>' +
    '<th>Status</th>' +
    '<th>Scanned At</th>' +
    '<th class="center">Comments</th>' +
    '</tr></thead><tbody>';
  unsent.forEach(p => {
    const statusClass = (p.status || '').toLowerCase() === 'delivered' ? 'delivered' :
                        (p.status || '').toLowerCase() === 'pending' ? 'pending' : 'default';
    const isChecked = dashSelectedRows.has(p.row);
    const reasonHtml = buildReasonHtml(p);
    const hasComments = (p.tracking_comments || p.comments || '').trim().length > 0;
    const allComments = p.tracking_comments || p.comments || '';
    const commentCount = hasComments ? allComments.split('\n').filter(l => l.trim()).length : 0;
    const isSent = p.is_sent;
    const isNew = hasNewActivity(p);
    // Visual indicators - only show for NEW activity
    const updateDot = isNew ? '<span style="display:inline-block;width:8px;height:8px;background:#EF4444;border-radius:50%;margin-right:6px;box-shadow:0 0 0 2px rgba(239,68,68,0.2);flex-shrink:0;animation:pulse 2s infinite;" title="New activity"></span>' : '';
    const updatedBadge = isNew ? '<span style="background:linear-gradient(135deg,#EF4444,#DC2626);color:white;font-size:9px;padding:2px 8px;border-radius:10px;margin-left:6px;font-weight:600;box-shadow:0 2px 4px rgba(239,68,68,0.3);animation:pulse 2s infinite;" title="New activity since last view">NEW</span>' : '';
    const commentBtnHtml = '<button class="comment-icon-btn' + (hasComments ? ' has-comments' : '') + '" onclick="event.stopPropagation();openCommentModal(\'' + escapeHtml(p.tracking).replace(/'/g, "\\'") + '\')" title="Comments">' +
      '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15a2 2 0 01-2 2H7l-4 4V5a2 2 0 012-2h14a2 2 0 012 2z"/></svg>' +
      (commentCount > 0 ? '<span class="comment-badge">' + commentCount + '</span>' : '') +
      '</button>';
    // Highlight row if it has NEW activity (light red background)
    const rowStyle = isChecked ? ' style="background:var(--primary-light);"' : (isNew ? ' style="background:rgba(239,68,68,0.05);"' : '');
    html += '<tr' + rowStyle + '>' +
      '<td class="td-center"><div class="dash-check' + (isChecked ? ' checked' : '') + '" onclick="event.stopPropagation();toggleDashPkg(' + p.row + ')"><svg viewBox="0 0 24 24"><polyline points="20 6 9 17 4 12" fill="none" stroke="white" stroke-width="3"/></svg></div></td>' +
      '<td><div class="td-tracking" onclick="event.stopPropagation();openCommentModal(\'' + escapeHtml(p.tracking).replace(/'/g, "\\'") + '\')" style="display:flex;align-items:center;">' + updateDot + escapeHtml(p.tracking) + updatedBadge + '</div></td>' +
      '<td>' + escapeHtml(p.rider_name || '') + '</td>' +
      '<td>' + (p.seller_name ? '<div class="dash-scan-seller">' + escapeHtml(p.seller_name) + '</div>' : '<span style="color:var(--text-muted);font-size:11px;">--</span>') + '</td>' +
      '<td style="font-size:12px;color:var(--text-secondary);">' + escapeHtml(p.whatsapp_group || '--') + '</td>' +
      '<td>' + reasonHtml + '</td>' +
      '<td><div class="dash-scan-status ' + statusClass + '">' + escapeHtml(p.status || 'Pending') + '</div></td>' +
      '<td style="font-size:11px;color:var(--text-muted);">' + escapeHtml(p.scanned_at || '') + '</td>' +
      '<td class="td-center">' + commentBtnHtml + '</td>' +
      '</tr>';
  });
  html += '</tbody></table></div>';
  list.innerHTML = html;
}

function toggleAllDashChecks(checked) {
  const filterVal = (document.getElementById('dash-seller-filter') || {}).value || '';
  // Show only packages that have NOT been sent via WhatsApp
  let unsent = allPackages.filter(p => !p.is_sent);
  if (filterVal) unsent = unsent.filter(p => p.seller_name === filterVal);
  if (checked) {
    unsent.forEach(p => dashSelectedRows.add(p.row));
  } else {
    unsent.forEach(p => dashSelectedRows.delete(p.row));
  }
  renderRecentScans();
  updateDashSendBar();
}

// Comment Modal Functions
let _currentCommentTracking = '';
let _commentRefreshInterval = null;

function getAuthorDisplay(author) {
  const a = (author || '').trim();
  const lower = a.toLowerCase();
  if (lower === 'dashboard' || lower === 'staff') return { name: 'Staff', type: 'staff' };
  if (lower === 'seller') return { name: 'Seller', type: 'seller' };
  if (lower.startsWith('rider')) return { name: a, type: 'rider' };
  // If it's a rider name (non-empty, not dashboard/seller), treat as rider
  if (a) return { name: 'Rider ' + a, type: 'rider' };
  return { name: 'Staff', type: 'staff' };
}

function getInitials(name) {
  const parts = name.trim().split(/\s+/);
  if (parts.length >= 2) return (parts[0][0] + parts[1][0]).toUpperCase();
  return name.substring(0, 2).toUpperCase();
}

function formatCommentTimestamp(ts) {
  // Try to format as "July 6, 2026, 11:27 a.m." style
  if (!ts) return '';
  const d = new Date(ts);
  if (isNaN(d.getTime())) return ts;
  const months = ['January','February','March','April','May','June','July','August','September','October','November','December'];
  const month = months[d.getMonth()];
  const day = d.getDate();
  const year = d.getFullYear();
  let hours = d.getHours();
  const minutes = d.getMinutes().toString().padStart(2, '0');
  const ampm = hours >= 12 ? 'p.m.' : 'a.m.';
  hours = hours % 12 || 12;
  return month + ' ' + day + ', ' + year + ', ' + hours + ':' + minutes + ' ' + ampm;
}

function renderCommentEntry(author, comment, timestamp) {
  const info = getAuthorDisplay(author);
  const initials = getInitials(info.name);
  const ts = formatCommentTimestamp(timestamp);
  return '<div class="comment-entry">' +
    '<div class="comment-avatar ' + info.type + '">' + escapeHtml(initials) + '</div>' +
    '<div class="comment-content">' +
      '<div class="comment-author-name">' + escapeHtml(info.name) + '</div>' +
      '<div class="comment-text">' + escapeHtml(comment) + '</div>' +
      (ts ? '<div class="comment-timestamp"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>' + escapeHtml(ts) + '</div>' : '') +
    '</div>' +
  '</div>';
}

function refreshComments() {
  if (!_currentCommentTracking) return;
  fetch('/api/comments?tracking=' + encodeURIComponent(_currentCommentTracking))
    .then(res => res.json())
    .then(data => {
      const body = document.getElementById('comment-modal-body');
      if (!data.ok || !data.comments || data.comments.length === 0) {
        const pkg = allPackages.find(p => p.tracking === _currentCommentTracking);
        const legacyComments = pkg ? (pkg.comments || '').trim() : '';
        if (legacyComments) {
          const lines = legacyComments.split('\n').filter(l => l.trim()).reverse();
          body.innerHTML = lines.map(line => {
            const m = line.match(/^\[([^\]]+)\]\s*(.*)$/);
            if (m) {
              const ts = m[1];
              const text = m[2];
              const am = text.match(/^(.*?)\s*\(([^)]+)\)$/);
              if (am) {
                return renderCommentEntry(am[2].trim(), am[1].trim(), ts);
              }
              return renderCommentEntry('dashboard', text, ts);
            }
            return '<div class="comment-entry"><div class="comment-content"><div class="comment-text">' + escapeHtml(line) + '</div></div></div>';
          }).join('');
        } else {
          body.innerHTML = '<div class="comment-modal-empty">No comments yet. Add the first one below.</div>';
        }
      } else {
        body.innerHTML = data.comments.slice().reverse().map(c => {
          return renderCommentEntry(c.author || 'dashboard', c.comment, c.timestamp);
        }).join('');
      }
    })
    .catch(() => {});
}

function openCommentModal(tracking) {
  _currentCommentTracking = tracking;

  // Mark this package as viewed (clears NEW indicator)
  markPackageViewed(tracking);

  // Update modal header
  document.getElementById('comment-modal-tracking').textContent = tracking;

  // Fetch comments from the tracking-based comments API
  const body = document.getElementById('comment-modal-body');
  body.innerHTML = '<div class="loading"><span class="spinner"></span>Loading comments...</div>';

  refreshComments();

  // Clear input
  document.getElementById('comment-modal-input').value = '';
  document.getElementById('comment-modal-submit').disabled = false;
  document.getElementById('comment-modal-submit').textContent = 'Send';

  // Show modal
  const overlay = document.getElementById('comment-modal-overlay');
  overlay.style.display = '';
  overlay.classList.add('active');
  setTimeout(() => document.getElementById('comment-modal-input').focus(), 100);

  // Start auto-refresh every 10 seconds
  if (_commentRefreshInterval) clearInterval(_commentRefreshInterval);
  _commentRefreshInterval = setInterval(refreshComments, 10000);
}

function closeCommentModal() {
  const overlay = document.getElementById('comment-modal-overlay');
  overlay.classList.remove('active');
  overlay.style.display = 'none';
  _currentCommentTracking = '';
  if (_commentRefreshInterval) {
    clearInterval(_commentRefreshInterval);
    _commentRefreshInterval = null;
  }
}

// Reason Modal Functions
function openReasonModal(fullText) {
  document.getElementById('reason-modal-text').textContent = fullText;
  const overlay = document.getElementById('reason-modal-overlay');
  overlay.style.display = '';
  overlay.classList.add('active');
}
function closeReasonModal() {
  const overlay = document.getElementById('reason-modal-overlay');
  overlay.classList.remove('active');
  overlay.style.display = 'none';
}
document.addEventListener('click', function(e) {
  if (e.target.id === 'reason-modal-overlay') closeReasonModal();
});
document.addEventListener('keydown', function(e) {
  if (e.key === 'Escape') closeReasonModal();
});

async function modalSubmitComment() {
  const input = document.getElementById('comment-modal-input');
  const btn = document.getElementById('comment-modal-submit');
  const val = input.value.trim();
  if (!val || !_currentCommentTracking) return;

  btn.disabled = true;
  btn.textContent = '...';

  try {
    const res = await fetch('/api/comments', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({tracking: _currentCommentTracking, comment: val, author: 'dashboard'})
    });
    const data = await res.json();
    if (data.ok) {
      toast('Comment added');
      input.value = '';
      // Refresh comments by re-opening modal
      openCommentModal(_currentCommentTracking);
      // Also refresh packages to update comment counts
      loadPackages();
    } else {
      toast('Failed: ' + (data.error || 'Unknown'), 'error');
    }
  } catch (e) {
    toast('Network error', 'error');
  }
  btn.disabled = false;
  btn.textContent = 'Send';
}

// Close modal on overlay click
document.addEventListener('click', function(e) {
  if (e.target.id === 'comment-modal-overlay') {
    closeCommentModal();
  }
});

// Close modal on Escape key
document.addEventListener('keydown', function(e) {
  if (e.key === 'Escape') {
    closeCommentModal();
  }
});

function renderSentTableRow(p) {
  const reasonHtml = buildReasonHtml(p);
  return '<tr>' +
    '<td><div class="dash-scan-track" style="cursor:default;">' + escapeHtml(p.tracking || '') + '</div></td>' +
    '<td style="font-size:12px;color:var(--text-secondary);">' + escapeHtml(p.group_name || '--') + '</td>' +
    '<td>' + reasonHtml + '</td>' +
    '<td><div class="dash-scan-status delivered">Sent</div></td>' +
    '<td style="font-size:11px;color:var(--text-muted);">' + escapeHtml(p.timestamp || '') + '</td>' +
    '</tr>';
}

async function loadSentPackages() {
  const res = await fetch('/api/sent-packages');
  const data = await res.json();
  if (!data.ok) return;
  const sent = data.packages || [];
  sentPackagesData = sent;
  // Update all count badges
  document.getElementById('stat-sent').textContent = sent.length;
  document.getElementById('nav-sent-count').textContent = sent.length;
  document.getElementById('sent-pkg-count').textContent = sent.length;
  document.getElementById('sent-full-count').textContent = sent.length;
  // Dashboard card (all packages, scrollable) as table
  const dashList = document.getElementById('sent-packages-list');
  if (sent.length === 0) {
    dashList.innerHTML = '<div class="empty-state">No packages sent via WhatsApp yet.</div>';
  } else {
    let html = '<div class="dash-table-wrap"><table class="dash-table"><thead><tr>' +
      '<th>Tracking</th><th>Group</th><th>Reason</th><th>Status</th><th>Sent At</th>' +
      '</tr></thead><tbody>';
    sent.forEach(p => { html += renderSentTableRow(p); });
    html += '</tbody></table></div>';
    dashList.innerHTML = html;
  }
  // Full Sent section as table (taller scroll area)
  const fullList = document.getElementById('sent-packages-full-list');
  if (sent.length === 0) {
    fullList.innerHTML = '<div class="empty-state"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M22 11.08V12a10 10 0 11-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/></svg><br>No packages sent via WhatsApp yet.</div>';
  } else {
    let html = '<div class="dash-table-wrap-full"><table class="dash-table"><thead><tr>' +
      '<th>Tracking</th><th>Group</th><th>Reason</th><th>Status</th><th>Sent At</th>' +
      '</tr></thead><tbody>';
    sent.forEach(p => { html += renderSentTableRow(p); });
    html += '</tbody></table></div>';
    fullList.innerHTML = html;
  }
}

function updateSellerFilter() {
  const sel = document.getElementById('dash-seller-filter');
  if (!sel) return;
  const current = sel.value;
  sel.innerHTML = '<option value="">All Sellers</option>';
  sellers.forEach(s => {
    sel.innerHTML += '<option value="' + escapeHtml(s.name) + '">' + escapeHtml(s.name) + ' (' + s.package_count + ')</option>';
  });
  sel.value = current;
}

function filterBySeller(name) {
  const sel = document.getElementById('dash-seller-filter');
  if (sel) sel.value = name;
  switchSection('dashboard', document.querySelector('.nav-item[data-section="dashboard"]'));
  filterDashScans();
}

function toggleDashPkg(row) {
  if (dashSelectedRows.has(row)) { dashSelectedRows.delete(row); }
  else { dashSelectedRows.add(row); }
  renderRecentScans();
  updateDashSendBar();
}

function updateDashSendBar() {
  const info = document.getElementById('dash-send-info');
  const btn = document.getElementById('dash-send-btn');
  const count = dashSelectedRows.size;
  if (count === 0) {
    info.innerHTML = 'Select packages above to send via WhatsApp';
    btn.disabled = true;
    return;
  }
  const selectedPkgs = allPackages.filter(p => dashSelectedRows.has(p.row));
  const sellerNames = [...new Set(selectedPkgs.map(p => p.seller_name).filter(Boolean))];
  if (sellerNames.length === 0) {
    info.innerHTML = '<strong>' + count + '</strong> selected &mdash; no seller mapped';
    btn.disabled = true;
    return;
  }
  // Support multi-seller selection
  if (sellerNames.length > 1) {
    // Check if all sellers have WhatsApp groups
    let allHaveGroups = true;
    let missingGroupSellers = [];
    for (const seller of sellerNames) {
      const sellerInfo = sellers.find(s => s.name === seller);
      if (!sellerInfo || !sellerInfo.has_group) {
        allHaveGroups = false;
        missingGroupSellers.push(seller);
      }
    }
    if (!allHaveGroups) {
      info.innerHTML = '<strong>' + count + '</strong> from <strong>' + sellerNames.length + ' sellers</strong> &mdash; no group for: ' + missingGroupSellers.join(', ');
      btn.disabled = true;
      return;
    }
    info.innerHTML = '<strong>' + count + '</strong> from <strong>' + sellerNames.length + ' sellers</strong> &mdash; ready to send to multiple groups';
    btn.disabled = false;
    return;
  }
  const seller = sellerNames[0];
  const sellerInfo = sellers.find(s => s.name === seller);
  if (!sellerInfo || !sellerInfo.has_group) {
    info.innerHTML = '<strong>' + count + '</strong> for <strong class="highlight">' + escapeHtml(seller) + '</strong> &mdash; no WhatsApp group set';
    btn.disabled = true;
    return;
  }
  info.innerHTML = '<strong>' + count + '</strong> package' + (count > 1 ? 's' : '') + ' for <strong class="highlight">' + escapeHtml(seller) + '</strong> &rarr; <strong class="highlight">' + escapeHtml(sellerInfo.whatsapp_group) + '</strong>';
  btn.disabled = false;
}

async function sendDashSelected() {
  const selectedPkgs = allPackages.filter(p => dashSelectedRows.has(p.row));
  const sellerNames = [...new Set(selectedPkgs.map(p => p.seller_name).filter(Boolean))];
  if (sellerNames.length === 0) return toast('No sellers mapped for selected packages', 'error');

  const btn = document.getElementById('dash-send-btn');
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner" style="border-color:rgba(255,255,255,0.3);border-top-color:white;width:14px;height:14px;"></span> Loading...';

  try {
    // Multi-seller: use new endpoint and show combined preview
    if (sellerNames.length > 1) {
      // Group packages by seller
      const packagesBySeller = {};
      for (const pkg of selectedPkgs) {
        if (!pkg.seller_name) continue;
        if (!packagesBySeller[pkg.seller_name]) packagesBySeller[pkg.seller_name] = [];
        packagesBySeller[pkg.seller_name].push(pkg.row);
      }

      // Fetch messages for all sellers
      const sellerMessages = [];
      for (const seller of sellerNames) {
        const rows = packagesBySeller[seller] || [];
        const res = await fetch('/api/generate-message', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({seller_name: seller, package_rows: rows})
        });
        const data = await res.json();
        if (!data.ok) {
          toast('Failed for ' + seller + ': ' + data.error, 'error');
          btn.disabled = false;
          btn.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="width:15px;height:15px"><line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/></svg> Send Selected';
          return;
        }
        sellerMessages.push({seller: seller, group: data.group, rows: rows, message: data.message});
      }

      // Show multi-seller preview modal
      showMultiSellerPreviewModal(sellerMessages);
    } else {
      // Single seller: existing flow
      const seller = sellerNames[0];
      const rows = selectedPkgs.map(p => p.row);
      const res = await fetch('/api/generate-message', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({seller_name: seller, package_rows: rows})
      });
      const data = await res.json();
      if (!data.ok) {
        toast('Failed: ' + data.error, 'error');
        btn.disabled = false;
        btn.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="width:15px;height:15px"><line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/></svg> Send Selected';
        return;
      }
      showMessagePreviewModal(seller, data.group, rows, data.message);
    }
  } catch (e) {
    toast('Error: ' + e.message, 'error');
  }
  btn.disabled = false;
  btn.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="width:15px;height:15px"><line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/></svg> Send Selected';
}

function showMessagePreviewModal(seller, group, rows, message) {
  const modal = document.getElementById('msg-preview-modal');
  document.getElementById('msg-preview-seller').textContent = seller;
  document.getElementById('msg-preview-group').textContent = group;
  document.getElementById('msg-preview-text').value = message;
  modal.style.display = 'flex';
  modal._rows = rows;
  modal._seller = seller;
  modal._group = group;
}

function closeMessagePreviewModal() {
  document.getElementById('msg-preview-modal').style.display = 'none';
}

async function sendEditedMessage() {
  const modal = document.getElementById('msg-preview-modal');
  const editedMessage = document.getElementById('msg-preview-text').value;
  const seller = modal._seller;
  const rows = modal._rows;
  const sendBtn = document.getElementById('msg-preview-send-btn');
  sendBtn.disabled = true;
  sendBtn.innerHTML = '<span class="spinner" style="border-color:rgba(255,255,255,0.3);border-top-color:white;width:14px;height:14px;"></span> Sending...';
  try {
    const res = await fetch('/api/send-to-seller', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({seller_name: seller, package_rows: rows, message: editedMessage})
    });
    const data = await res.json();
    if (data.ok) {
      toast('Sent ' + data.package_count + ' package(s) to ' + data.group);
      dashSelectedRows.clear();
      loadPackages();
      loadSellers();
      loadHistory();
      loadSentPackages();
      closeMessagePreviewModal();
      updateDashSendBar();
    } else {
      toast('Failed: ' + data.error, 'error');
    }
  } catch (e) {
    toast('Error: ' + e.message, 'error');
  }
  sendBtn.disabled = false;
  sendBtn.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="width:15px;height:15px"><line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/></svg> Send';
}

function showMultiSellerPreviewModal(sellerMessages) {
  const modal = document.getElementById('multi-seller-preview-modal');
  const container = document.getElementById('multi-seller-messages-container');
  container.innerHTML = '';

  // Build UI for each seller message
  for (const sm of sellerMessages) {
    const div = document.createElement('div');
    div.className = 'multi-seller-msg-block';
    div.innerHTML = `
      <div style="font-weight:600;margin-bottom:6px;">${escapeHtml(sm.seller)} &rarr; <span class="highlight">${escapeHtml(sm.group)}</span> (${sm.rows.length} pkg${sm.rows.length>1?'s':''})</div>
      <textarea class="multi-seller-textarea" data-seller="${escapeHtml(sm.seller)}" data-rows='${JSON.stringify(sm.rows)}' style="width:100%;height:100px;min-height:80px;font-family:monospace;font-size:12px;padding:8px;border:1px solid var(--border);border-radius:6px;background:var(--bg-card);color:var(--text);resize:vertical;">${escapeHtml(sm.message)}</textarea>
    `;
    container.appendChild(div);
  }

  modal.style.display = 'flex';
  modal._sellerMessages = sellerMessages;
}

function closeMultiSellerPreviewModal() {
  document.getElementById('multi-seller-preview-modal').style.display = 'none';
}

async function sendMultiSellerMessages() {
  const modal = document.getElementById('multi-seller-preview-modal');
  const sendBtn = document.getElementById('multi-seller-send-btn');
  const textareas = document.querySelectorAll('.multi-seller-textarea');

  sendBtn.disabled = true;
  sendBtn.innerHTML = '<span class="spinner" style="border-color:rgba(255,255,255,0.3);border-top-color:white;width:14px;height:14px;"></span> Sending...';

  // Collect all messages from textareas
  const messagesBySeller = {};
  for (const ta of textareas) {
    const seller = ta.dataset.seller;
    const rows = JSON.parse(ta.dataset.rows);
    const message = ta.value;
    messagesBySeller[seller] = {rows, message};
  }

  // Build packages_by_seller for the API
  const packages_by_seller = {};
  for (const [seller, data] of Object.entries(messagesBySeller)) {
    packages_by_seller[seller] = data.rows;
  }

  try {
    // Send all at once using the multi-seller endpoint (uses default generated messages)
    // But we need to respect edited messages - so send individually
    const results = [];
    for (const [seller, data] of Object.entries(messagesBySeller)) {
      const res = await fetch('/api/send-to-seller', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({seller_name: seller, package_rows: data.rows, message: data.message})
      });
      const result = await res.json();
      results.push({seller, ...result});
    }

    const successCount = results.filter(r => r.ok).length;
    const failCount = results.length - successCount;

    if (successCount > 0) {
      toast(`Sent to ${successCount} seller${successCount>1?'s':''}` + (failCount > 0 ? `, ${failCount} failed` : ''));
    }
    if (failCount > 0) {
      const failedSellers = results.filter(r => !r.ok).map(r => r.seller + ': ' + (r.error || 'unknown'));
      toast('Failed: ' + failedSellers.join('; '), 'error');
    }

    dashSelectedRows.clear();
    loadPackages();
    loadSellers();
    loadHistory();
    loadSentPackages();
    closeMultiSellerPreviewModal();
    updateDashSendBar();
  } catch (e) {
    toast('Error: ' + e.message, 'error');
  }

  sendBtn.disabled = false;
  sendBtn.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="width:15px;height:15px"><line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/></svg> Send All';
}

/* ═══════════════════════════════════════════════════════════
   ACTIONS
   ═══════════════════════════════════════════════════════════ */
async function setSellerGroup() {
  const seller = document.getElementById('sg-seller').value.trim();
  const group = document.getElementById('sg-group').value.trim();
  if (!seller || !group) return toast('Enter seller name and WhatsApp group', 'error');

  const res = await fetch('/api/seller-groups', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({seller_name: seller, whatsapp_group: group})
  });
  const data = await res.json();
  if (data.ok) {
    document.getElementById('sg-seller').value = '';
    document.getElementById('sg-group').value = '';
    toast('Mapping saved: ' + seller + ' → ' + group);
    loadSellers();
    loadSellerGroups();
    loadPackages();
  } else {
    toast('Error: ' + data.error, 'error');
  }
}

/* ═══════════════════════════════════════════════════════════
   EXPORT CSV
   ═══════════════════════════════════════════════════════════ */
function csvEscape(val) {
  if (val == null) return '';
  const s = String(val);
  if (s.includes(',') || s.includes('"') || s.includes('\n')) {
    return '"' + s.replace(/"/g, '""') + '"';
  }
  return s;
}

function downloadCsv(filename, headers, rows) {
  const csv = headers.map(csvEscape).join(',') + '\n' +
    rows.map(r => r.map(csvEscape).join(',')).join('\n');
  const blob = new Blob([csv], {type: 'text/csv;charset=utf-8;'});
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}

function exportRecentScans() {
  const sel = document.getElementById('dash-seller-filter');
  const sellerVal = sel ? sel.value : '';
  let pkgs = allPackages.filter(p => p.status !== 'sent');
  if (sellerVal) {
    pkgs = pkgs.filter(p => (p.seller_name || '') === sellerVal);
  }
  if (pkgs.length === 0) return toast('No packages to export', 'error');
  const headers = ['Tracking', 'Rider', 'Seller', 'Group', 'Reason', 'Status', 'Scanned At'];
  const rows = pkgs.map(p => [
    p.tracking || '',
    p.rider || '',
    p.seller_name || '',
    p.group_name || '',
    p.reason || '',
    p.status || '',
    p.scanned_at || ''
  ]);
  const dateStr = new Date().toISOString().slice(0, 10);
  downloadCsv('recent-scans-' + dateStr + '.csv', headers, rows);
  toast('Exported ' + rows.length + ' rows');
}

function exportSentPackages() {
  if (!sentPackagesData || sentPackagesData.length === 0) return toast('No sent packages to export', 'error');
  const headers = ['Tracking', 'Group', 'Reason', 'Status', 'Sent At'];
  const rows = sentPackagesData.map(p => [
    p.tracking || '',
    p.group_name || '',
    p.reason || '',
    p.status || '',
    p.timestamp || ''
  ]);
  const dateStr = new Date().toISOString().slice(0, 10);
  downloadCsv('sent-packages-' + dateStr + '.csv', headers, rows);
  toast('Exported ' + rows.length + ' rows');
}

/* ═══════════════════════════════════════════════════════════
   INIT
   ═══════════════════════════════════════════════════════════ */
loadSellers();
loadPackages();
loadSellerGroups();
loadHistory();
loadSentPackages();
checkWhatsApp();
setInterval(checkWhatsApp, 60000);
// Auto-refresh packages every 3 seconds for near-instant sync with rider app
setInterval(loadPackages, 3000);
</script>

<!-- Comment Modal -->
<div class="comment-modal-overlay" id="comment-modal-overlay" style="display:none">
  <div class="comment-modal">
    <div class="comment-modal-header">
      <span class="tracking-label" id="comment-modal-tracking">Tracking Number</span>
      <button class="close-btn" onclick="closeCommentModal()">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="width:14px;height:14px"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
      </button>
    </div>
    <div class="comments-section-header">
      <div class="comments-title">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15a2 2 0 01-2 2H7l-4 4V5a2 2 0 012-2h14a2 2 0 012 2z"/></svg>
        COMMENTS
      </div>
    </div>
    <div class="comment-modal-body" id="comment-modal-body">
      <div class="comment-modal-empty">No comments yet.</div>
    </div>
    <div class="comment-modal-footer">
      <input type="text" id="comment-modal-input" placeholder="Type a comment..." onkeydown="if(event.key==='Enter')modalSubmitComment()">
      <button id="comment-modal-submit" onclick="modalSubmitComment()">Send</button>
    </div>
  </div>
</div>

<!-- Reason Modal -->
<div class="reason-modal-overlay" id="reason-modal-overlay" style="display:none">
  <div class="reason-modal">
    <div class="reason-modal-header">
      <h3>
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="width:18px;height:18px;color:var(--primary)"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>
        Reason Details
      </h3>
      <button class="close-btn" onclick="closeReasonModal()">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="width:14px;height:14px"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
      </button>
    </div>
    <div class="reason-modal-text" id="reason-modal-text"></div>
  </div>
</div>

<!-- Message Preview Modal -->
<div id="msg-preview-modal" style="display:none;position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,0.6);z-index:9999;align-items:center;justify-content:center;">
  <div style="background:var(--bg-card);border-radius:12px;padding:24px;width:90%;max-width:600px;max-height:80vh;display:flex;flex-direction:column;box-shadow:0 10px 40px rgba(0,0,0,0.3);">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px;">
      <h3 style="margin:0;font-size:18px;font-weight:600;">Preview WhatsApp Message</h3>
      <button onclick="closeMessagePreviewModal()" style="background:none;border:none;cursor:pointer;padding:4px;color:var(--text-secondary);">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="width:20px;height:20px"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
      </button>
    </div>
    <div style="margin-bottom:12px;font-size:13px;color:var(--text-secondary);">
      <strong>Seller:</strong> <span id="msg-preview-seller"></span> &nbsp;|&nbsp;
      <strong>Group:</strong> <span id="msg-preview-group"></span>
    </div>
    <textarea id="msg-preview-text" style="flex:1;min-height:300px;padding:12px;border:1px solid var(--border);border-radius:8px;background:var(--bg-input);color:var(--text);font-family:monospace;font-size:13px;resize:vertical;line-height:1.5;"></textarea>
    <div style="display:flex;gap:12px;margin-top:16px;justify-content:flex-end;">
      <button onclick="closeMessagePreviewModal()" style="padding:10px 20px;border:1px solid var(--border);border-radius:8px;background:var(--bg-elevated);color:var(--text);cursor:pointer;font-size:14px;">Cancel</button>
      <button id="msg-preview-send-btn" onclick="sendEditedMessage()" style="padding:10px 20px;border:none;border-radius:8px;background:var(--primary);color:white;cursor:pointer;font-size:14px;display:flex;align-items:center;gap:6px;">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="width:15px;height:15px"><line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/></svg> Send
      </button>
    </div>
  </div>
</div>

<!-- Multi-Seller Preview Modal -->
<div id="multi-seller-preview-modal" style="display:none;position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,0.6);z-index:9999;align-items:center;justify-content:center;">
  <div style="background:var(--bg-card);border-radius:12px;padding:24px;width:90%;max-width:700px;max-height:85vh;display:flex;flex-direction:column;box-shadow:0 10px 40px rgba(0,0,0,0.3);">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px;">
      <h3 style="margin:0;font-size:18px;font-weight:600;">Send to Multiple Sellers</h3>
      <button onclick="closeMultiSellerPreviewModal()" style="background:none;border:none;cursor:pointer;padding:4px;color:var(--text-secondary);">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="width:20px;height:20px"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
      </button>
    </div>
    <div style="margin-bottom:12px;font-size:13px;color:var(--text-secondary);">
      Review and edit messages for each seller before sending.
    </div>
    <div id="multi-seller-messages-container" style="flex:1;overflow-y:auto;display:flex;flex-direction:column;gap:16px;padding-right:8px;">
      <!-- Dynamically populated -->
    </div>
    <div style="display:flex;gap:12px;margin-top:16px;justify-content:flex-end;">
      <button onclick="closeMultiSellerPreviewModal()" style="padding:10px 20px;border:1px solid var(--border);border-radius:8px;background:var(--bg-elevated);color:var(--text);cursor:pointer;font-size:14px;">Cancel</button>
      <button id="multi-seller-send-btn" onclick="sendMultiSellerMessages()" style="padding:10px 20px;border:none;border-radius:8px;background:var(--primary);color:white;cursor:pointer;font-size:14px;display:flex;align-items:center;gap:6px;">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="width:15px;height:15px"><line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/></svg> Send All
      </button>
    </div>
  </div>
</div>

</body>
</html>
"""

# ─── Session Keep-Alive ────────────────────────────────────────────────────────
_session_keepalive_running = False

def _session_keepalive_loop():
    """Background thread to keep WhatsApp session alive by periodic checks."""
    global _session_keepalive_running
    _session_keepalive_running = True
    print("[KEEPALIVE] Session keep-alive started")
    while _session_keepalive_running:
        try:
            if WHATSAPP_AVAILABLE:
                state = get_connection_state()
                # Check every 5 minutes if not recently checked
                if time.time() - state["last_check"] > 300:
                    is_logged_in()
                    print(f"[KEEPALIVE] Session check: logged_in={state['logged_in']}")
        except Exception as e:
            print(f"[KEEPALIVE] Error: {e}")
        time.sleep(300)  # Check every 5 minutes

def start_session_keepalive():
    """Start the session keep-alive background thread."""
    if not WHATSAPP_AVAILABLE:
        print("[KEEPALIVE] WhatsApp module not available, skipping keep-alive")
        return
    t = threading.Thread(target=_session_keepalive_loop, daemon=True)
    t.start()
    print("[KEEPALIVE] Session keep-alive thread started")

if __name__ == "__main__":
    port = int(os.environ.get('PORT', 57800))
    
    # Only start keepalive and open browser for local development
    if os.environ.get('PORT') is None:
        print(f"\n  WhatsApp Automation Dashboard running at http://localhost:{port}")
        print("  Press Ctrl+C to stop.\n")
        start_session_keepalive()
    else:
        print(f"\n  WhatsApp Automation Dashboard running on port {port}\n")
    
    app.run(host="0.0.0.0", port=port, debug=False)

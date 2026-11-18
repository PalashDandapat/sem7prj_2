"""
Endpoints:
    POST /entry/detect   -> capture frame, detect plate, check DB, decide next step
    POST /entry/confirm  -> gatekeeper manually enters mobile + confirms send
    POST /entry/print    -> gatekeeper chooses "print receipt" instead
    POST /exit/detect    -> capture frame, detect plate, free up the slot
    POST /entry/manual   -> same as /entry/detect but plate typed by gatekeeper
    POST /exit/manual    -> same as /exit/detect but plate typed by gatekeeper
    GET  /slots          -> current slot status (for the UI to display)
    GET  /overview       -> total / occupied / free counts
    GET  /activity       -> recent entry/exit events
"""

import re
from datetime import datetime

from fastapi import FastAPI, UploadFile, File, Form
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from database import get_connection, init_db
from anpr import detect_plate
from notify import send_whatsapp, print_receipt
from imp_fucntion import find_free_slot, try_claim_slot, assign_slot,lookup_vehicle,check_already_exist


app = FastAPI(title="Mall Parking System")

# Allow the frontend (served separately or via file://) to call this API
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

init_db()


# ----------------------------------------------------------------- helpers
MIN_PLATE_LEN = 6


def normalize_plate(text: str) -> str:
    """Same cleaning the ANPR uses: only A-Z / 0-9, uppercase."""
    return re.sub(r"[^A-Za-z0-9]", "", text or "").upper()


def process_entry(plate_number: str, gate_id: str) -> dict:
    """Everything that happens AFTER a plate is known (camera OR manual)."""
    existing = check_already_exist(plate_number)
    if existing:
        return {
            "status": "parked",
            "plate_number": plate_number,
            "slot_id": existing["slot_id"],
        }

    vehicle = lookup_vehicle(plate_number)
    slot_id = find_free_slot(gate_id)

    if not slot_id:
        return {"status": "full", "plate_number": plate_number}

    if vehicle and vehicle.get("mobile_number"):
        # Registered vehicle -> auto slot assign + auto WhatsApp
        assigned = assign_slot(plate_number, slot_id, gate_id)
        if not assigned:
            return {"status": "full", "plate_number": plate_number}

        send_whatsapp(
            vehicle["mobile_number"],
            f"Welcome! Your vehicle {plate_number} is parked at slot {assigned}.",
        )
        return {
            "status": "registered",
            "plate_number": plate_number,
            "slot_id": assigned,
            "mobile_number": vehicle["mobile_number"],
        }

    # Unregistered -> gatekeeper must choose: enter mobile OR print receipt.
    # The slot is only suggested here; it is claimed atomically on confirm/print
    # (and replaced with another free slot if someone else took it meanwhile).
    return {
        "status": "unregistered",
        "plate_number": plate_number,
        "suggested_slot_id": slot_id,
    }


def process_exit(plate_number: str, gate_id: str) -> dict:
    """Everything that happens AFTER a plate is known (camera OR manual)."""
    conn = get_connection()
    session = conn.execute(
        "SELECT * FROM parking_sessions WHERE plate_number = ? AND status = 'active'",
        (plate_number,),
    ).fetchone()

    if not session:
        conn.close()
        return {"status": "no_active_session", "plate_number": plate_number}

    conn.execute(
        "UPDATE slots SET status = 'free' WHERE slot_id = ?", (session["slot_id"],)
    )
    conn.execute(
        """UPDATE parking_sessions SET status = 'completed', exit_time = ?, gate_out = ?
           WHERE id = ?""",
        (datetime.now().isoformat(), gate_id, session["id"]),
    )
    conn.commit()
    conn.close()

    return {"status": "exited", "plate_number": plate_number, "slot_id": session["slot_id"]}


# ------------------------------------------------------------------- entry
@app.post("/entry/detect")
async def entry_detect(gate_id: str = Form(...), image: UploadFile = File(...)):
    image_bytes = await image.read()
    # Run the heavy ANPR model in a worker thread so the server stays responsive
    plate_number = await run_in_threadpool(detect_plate, image_bytes)

    if not plate_number:
        return {"status": "no_plate_detected"}

    return process_entry(plate_number, gate_id)


@app.post("/entry/manual")
async def entry_manual(gate_id: str = Form(...), plate_number: str = Form(...)):
    """Camera not working? The gatekeeper types the plate instead."""
    plate = normalize_plate(plate_number)
    if len(plate) < MIN_PLATE_LEN:
        return {"status": "invalid_plate"}
    return process_entry(plate, gate_id)


@app.post("/entry/confirm")
async def entry_confirm(
    plate_number: str = Form(...),
    mobile_number: str = Form(...),
    slot_id: str = Form(...),
    gate_id: str = Form(...),
):
    if check_already_exist(plate_number):
        existing = check_already_exist(plate_number)
        return {"status": "parked", "slot_id": existing["slot_id"]}

    assigned = assign_slot(plate_number, slot_id, gate_id)
    if not assigned:
        return {"status": "full", "slot_id": None}

    # Save mobile number against the plate for next time
    conn = get_connection()
    conn.execute(
        """INSERT INTO vehicles (plate_number, mobile_number) VALUES (?, ?)
           ON CONFLICT(plate_number) DO UPDATE SET mobile_number = excluded.mobile_number""",
        (plate_number, mobile_number),
    )
    conn.commit()
    conn.close()

    send_whatsapp(
        mobile_number,
        f"Welcome! Your vehicle {plate_number} is parked at slot {assigned}.",
    )
    return {"status": "confirmed", "slot_id": assigned}


@app.post("/entry/print")
async def entry_print(
    plate_number: str = Form(...),
    slot_id: str = Form(...),
    gate_id: str = Form(...),
):
    existing = check_already_exist(plate_number)
    if existing:
        return {"status": "parked", "slot_id": existing["slot_id"]}

    assigned = assign_slot(plate_number, slot_id, gate_id)
    if not assigned:
        return {"status": "full", "slot_id": None}

    print_receipt(plate_number, assigned)
    return {"status": "printed", "slot_id": assigned}


# -------------------------------------------------------------------- exit
@app.post("/exit/detect")
async def exit_detect(gate_id: str = Form(...), image: UploadFile = File(...)):
    image_bytes = await image.read()
    plate_number = await run_in_threadpool(detect_plate, image_bytes)

    if not plate_number:
        return {"status": "no_plate_detected"}

    return process_exit(plate_number, gate_id)


@app.post("/exit/manual")
async def exit_manual(gate_id: str = Form(...), plate_number: str = Form(...)):
    """Camera not working? The gatekeeper types the plate instead."""
    plate = normalize_plate(plate_number)
    if len(plate) < MIN_PLATE_LEN:
        return {"status": "invalid_plate"}
    return process_exit(plate, gate_id)


# --------------------------------------------------------------- dashboard
@app.get("/slots")
def get_slots():
    conn = get_connection()
    rows = conn.execute("SELECT * FROM slots ORDER BY slot_id").fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.get("/overview")
def get_overview():
    conn = get_connection()
    total = conn.execute("SELECT COUNT(*) AS c FROM slots").fetchone()["c"]
    occupied = conn.execute(
        "SELECT COUNT(*) AS c FROM slots WHERE status = 'occupied'"
    ).fetchone()["c"]
    conn.close()
    return {"total_slots": total, "occupied": occupied, "free": total - occupied}


@app.get("/activity")
def get_activity(limit: int = 6):
    conn = get_connection()
    rows = conn.execute(
        """SELECT * FROM parking_sessions
           ORDER BY COALESCE(exit_time, entry_time) DESC
           LIMIT ?""",
        (limit,),
    ).fetchall()
    conn.close()

    events = []
    for r in rows:
        if r["status"] == "completed":
            events.append({
                "type": "exit",
                "plate_number": r["plate_number"],
                "slot_id": r["slot_id"],
                "time": r["exit_time"],
            })
        else:
            events.append({
                "type": "entry",
                "plate_number": r["plate_number"],
                "slot_id": r["slot_id"],
                "time": r["entry_time"],
            })
    return events


# Serve the gatekeeper frontend at http://localhost:8000/
app.mount("/", StaticFiles(directory="../frontend", html=True), name="frontend")
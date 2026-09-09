#!/usr/bin/env python3
"""
repetier-octoprint-bridge
=========================
A minimal OctoPrint-compatible API that forwards uploads and status to
Repetier-Server. Point EasyPrint (or any slicer with OctoPrint support) at this
instead of at Repetier directly.

Run:
    pip install fastapi uvicorn httpx python-multipart
    REPETIER_URL=http://192.168.0.50:3344 \
    REPETIER_APIKEY=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx \
    REPETIER_SLUG=Printer_One \
    python repetier_octoprint_bridge.py

Selecting which printer:
    1. Path prefix   -> http://bridge:5000/p/<slug>  (client host field)
    2. API key map   -> BRIDGE_KEYMAP='{"token-a":"Printer_One","token-b":"Printer_Two"}'
    3. REPETIER_SLUG -> single-printer fallback

Everything unmatched under /api is logged with method, headers and body so you
can see exactly what EasyPrint asks for and add it.
"""

import json
import logging
import os
import re
from datetime import datetime
from typing import Optional

import httpx
import uvicorn
from fastapi import FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

def env(name: str, default: str = "") -> str:
    """Read a BRIDGE_<NAME> environment variable."""
    return os.getenv(f"BRIDGE_{name}", default)


REPETIER_URL = os.getenv("REPETIER_URL", "http://127.0.0.1:3344").rstrip("/")
REPETIER_APIKEY = os.getenv("REPETIER_APIKEY", "")
DEFAULT_SLUG = os.getenv("REPETIER_SLUG", "")
KEY_MAP = json.loads(env("KEYMAP", "{}"))
REQUIRE_KEY = env("REQUIRE_KEY")  # if set, incoming key must match this or be in KEY_MAP
LISTEN_HOST = env("HOST", "0.0.0.0")
LISTEN_PORT = int(env("PORT", "5000"))

# Rename uploads using metadata parsed out of the G-code itself. Placeholders:
#   {name} {printer} {material} {nozzle} {layer} {time} {weight} {slug} {date}
# Empty string keeps whatever filename the client sent.
NAME_TEMPLATE = env("NAME_TEMPLATE", "{name}_{printer}_{material}_{nozzle}mm_{time}")
# Case applied to the {name} part only, so printer and material keep their capitals:
#   lower | upper | keep      ... or lower-all / upper-all for the whole filename.
NAME_CASE = env("NAME_CASE", "lower").lower()

# What we claim to be. Some clients gate features on this.
OCTO_VERSION = "1.9.3"
API_VERSION = "0.1"

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s"
)
log = logging.getLogger("repetier-octoprint-bridge")

app = FastAPI(title="repetier-octoprint-bridge")

CORS_OPTS = dict(
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"],
    max_age=3600,
)
try:
    # Starlette >= 0.41 rejects Private Network Access preflights unless this is
    # set. Older versions don't know the kwarg and allow them implicitly.
    app.add_middleware(CORSMiddleware, allow_private_network=True, **CORS_OPTS)
except TypeError:
    app.add_middleware(CORSMiddleware, **CORS_OPTS)


@app.exception_handler(httpx.RequestError)
async def repetier_unreachable(request: Request, exc: httpx.RequestError):
    log.error("Repetier unreachable at %s: %s", REPETIER_URL, exc)
    return JSONResponse(
        {"error": f"Cannot reach Repetier-Server at {REPETIER_URL}: {exc}"},
        status_code=502,
    )


@app.middleware("http")
async def normalize_and_log(request: Request, call_next):
    """
    1. We don't know yet whether EasyPrint sends the token as X-Api-Key, as
       ?apikey=, or as a Bearer header -- accept all three and normalize to the
       header the handlers read.
    2. Chrome's Private Network Access preflight needs an explicit opt-in when a
       public HTTPS page talks to a LAN address.
    """
    headers = dict(request.scope["headers"])
    if b"x-api-key" not in headers:
        key = request.query_params.get("apikey")
        if not key:
            auth = request.headers.get("authorization", "")
            if auth.lower().startswith("bearer "):
                key = auth[7:].strip()
        if key:
            request.scope["headers"] = request.scope["headers"] + [
                (b"x-api-key", key.encode())
            ]

    log.info("--> %s %s  origin=%s", request.method, request.url.path,
             request.headers.get("origin", "-"))
    response = await call_next(request)
    if request.method == "OPTIONS":
        response.headers["Access-Control-Allow-Private-Network"] = "true"
    return response


# --------------------------------------------------------------------------- #
# Repetier helpers
# --------------------------------------------------------------------------- #


def parse_gcode_meta(content: bytes) -> dict:
    """
    Pull slicer metadata out of G-code comments. PrusaSlicer writes a config
    block at the end of the file and the time estimate in the footer, so we look
    at both ends rather than reading the whole thing into a string.
    """
    head = content[:32768].decode("latin-1", "ignore")
    tail = content[-131072:].decode("latin-1", "ignore")
    text = head + "\n" + tail
    meta = {}

    def grab(pattern, key, cast=str):
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            val = m.group(1).strip()
            if val and val.lower() not in ("none", "n/a"):
                meta[key] = cast(val)

    # PrusaSlicer / SuperSlicer / Orca all use these keys, with minor variations.
    grab(r";\s*printer_model\s*=\s*(.+)", "printer")
    grab(r";\s*filament_type\s*=\s*([^;\r\n]+)", "material")
    grab(r";\s*nozzle_diameter\s*=\s*([\d.]+)", "nozzle")
    grab(r";\s*layer_height\s*=\s*([\d.]+)", "layer")
    grab(r";\s*estimated printing time \(normal mode\)\s*=\s*(.+)", "time")
    if "time" not in meta:
        grab(r";\s*(?:TIME|estimated printing time)\s*[:=]\s*(.+)", "time")
    grab(r";\s*(?:total )?filament used \[g\]\s*=\s*([\d.]+)", "weight")

    # Cura fallbacks. Its keys use spaces rather than underscores.
    if "material" not in meta:
        grab(r";\s*(?:FILAMENT_TYPE|Filament[ _]type)\s*[:=]\s*([^;\r\n]+)", "material")
    if "nozzle" not in meta:
        grab(r";\s*(?:NOZZLE_DIAMETER|Nozzle[ _]diameter)\s*[:=]\s*([\d.]+)", "nozzle")
    if "layer" not in meta:
        grab(r";\s*(?:Layer[ _]height|LAYER_HEIGHT)\s*[:=]\s*([\d.]+)", "layer")
    if "printer" not in meta:
        grab(r";\s*(?:TARGET_MACHINE\.NAME|printer_settings_id)\s*[:=]\s*(.+)", "printer")

    # A multi-extruder file lists one value per tool: "PLA;PETG" or "0.4,0.4".
    for key in ("material", "nozzle", "layer"):
        if key in meta:
            meta[key] = re.split(r"[;,]", meta[key])[0].strip()

    if "time" in meta:
        meta["time"] = normalize_time(meta["time"])
    if "weight" in meta:
        meta["weight"] = f"{float(meta['weight']):.0f}g"
    return meta


def normalize_time(raw: str) -> str:
    """'1h 3m 21s' or '3821' (seconds) -> '1h03m'."""
    raw = raw.strip()
    if raw.isdigit():
        secs = int(raw)
        h, m = divmod(secs // 60, 60)
        return f"{h}h{m:02d}m" if h else f"{m}m"
    d = re.search(r"(\d+)\s*d", raw, re.I)
    h = re.search(r"(\d+)\s*h", raw, re.I)
    m = re.search(r"(\d+)\s*m", raw, re.I)
    hours = int(h.group(1)) if h else 0
    if d:
        hours += int(d.group(1)) * 24
    mins = int(m.group(1)) if m else 0
    if hours:
        return f"{hours}h{mins:02d}m"
    return f"{mins}m" if mins else raw.replace(" ", "")


def build_filename(original: str, content: bytes, slug: str) -> str:
    """Rename an upload using BRIDGE_NAME_TEMPLATE and the G-code's own metadata."""
    if not NAME_TEMPLATE:
        return original

    stem, ext = os.path.splitext(original)
    if ext.lower() not in (".gcode", ".gco", ".g"):
        ext = ".gcode"

    meta = parse_gcode_meta(content)
    if NAME_CASE in ("lower", "lower-all"):
        stem = stem.lower()
    elif NAME_CASE in ("upper", "upper-all"):
        stem = stem.upper()
    fields = {
        "name": stem,
        "slug": slug,
        "printer": meta.get("printer", slug),
        "material": meta.get("material", ""),
        "nozzle": meta.get("nozzle", ""),
        "layer": meta.get("layer", ""),
        "time": meta.get("time", ""),
        "weight": meta.get("weight", ""),
        "date": datetime.now().strftime("%Y%m%d"),
    }
    log.info("gcode metadata: %s", {k: v for k, v in meta.items()})

    try:
        name = NAME_TEMPLATE.format(**fields)
    except KeyError as exc:
        log.warning("BRIDGE_NAME_TEMPLATE has unknown placeholder %s, keeping original", exc)
        return original

    # Collapse the separators left behind by fields that came back empty:
    # "cube__PLA_mm_1h02m" -> "cube_PLA_1h02m"
    name = re.sub(r"[_\-.]*mm(?=[_\-.]|$)", lambda m: "" if not fields["nozzle"] else m.group(0), name)
    name = re.sub(r"[_\-]{2,}", "_", name).strip("_-. ")
    name = re.sub(r"\s+", "_", name)
    name = re.sub(r"[^A-Za-z0-9._\-]", "", name)
    if NAME_CASE == "lower-all":
        name = name.lower()
    elif NAME_CASE == "upper-all":
        name = name.upper()
    return (name or stem) + ext


def resolve_slug(path_slug: Optional[str], api_key: Optional[str]) -> str:
    if path_slug:
        return path_slug
    if api_key and api_key in KEY_MAP:
        return KEY_MAP[api_key]
    if DEFAULT_SLUG:
        return DEFAULT_SLUG
    raise HTTPException(400, "No printer slug: use /p/<slug>, BRIDGE_KEYMAP or REPETIER_SLUG")


def check_key(api_key: Optional[str]) -> None:
    if not REQUIRE_KEY and not KEY_MAP:
        return
    if api_key and (api_key == REQUIRE_KEY or api_key in KEY_MAP):
        return
    raise HTTPException(403, "Invalid API key")


async def rep_call(action: str, slug: str = "", data: Optional[dict] = None):
    """Generic Repetier websocket-command-over-REST call."""
    url = f"{REPETIER_URL}/printer/api/{slug}" if slug else f"{REPETIER_URL}/printer/api"
    params = {"a": action}
    if data is not None:
        params["data"] = json.dumps(data)
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.get(url, params=params, headers={"x-api-key": REPETIER_APIKEY})
        r.raise_for_status()
        return r.json()


async def rep_upload(slug: str, filename: str, content: bytes, start_print: bool):
    """
    Upload to Repetier.
      /printer/model/<slug> -> stores in the g-code model list
      /printer/job/<slug>   -> queues it; starts immediately if the queue is idle
    Multipart shape matches the documented curl:
      -F "a=upload" -F "filename=@file.gcode"
    """
    target = "job" if start_print else "model"
    url = f"{REPETIER_URL}/printer/{target}/{slug}"
    fields = {"a": "upload", "name": filename}
    if not start_print:
        fields["overwrite"] = "true"
    files = {"filename": (filename, content, "application/octet-stream")}
    async with httpx.AsyncClient(timeout=600) as c:
        r = await c.post(
            url, data=fields, files=files, headers={"x-api-key": REPETIER_APIKEY}
        )
    log.info("repetier %s %s -> %s", target, filename, r.status_code)
    if r.status_code >= 400:
        raise HTTPException(502, f"Repetier rejected upload ({r.status_code}): {r.text[:300]}")
    return r


async def printer_entry(slug: str) -> dict:
    """One entry out of listPrinter, or {} if not found."""
    try:
        printers = await rep_call("listPrinter")
    except Exception as exc:  # noqa: BLE001
        log.warning("listPrinter failed: %s", exc)
        return {}
    if isinstance(printers, dict):
        printers = printers.get("data", []) or []
    for p in printers:
        if p.get("slug") == slug or p.get("name") == slug:
            return p
    return {}


# --------------------------------------------------------------------------- #
# OctoPrint-shaped endpoints
# --------------------------------------------------------------------------- #


async def version(x_api_key: Optional[str] = Header(None)):
    check_key(x_api_key)
    return {
        "api": API_VERSION,
        "server": OCTO_VERSION,
        "text": f"OctoPrint {OCTO_VERSION}",
    }


async def server_info(x_api_key: Optional[str] = Header(None)):
    check_key(x_api_key)
    return {"version": OCTO_VERSION, "safemode": None}


async def login(x_api_key: Optional[str] = Header(None)):
    check_key(x_api_key)
    return {
        "_is_external_client": True,
        "name": "repetier-octoprint-bridge",
        "active": True,
        "admin": True,
        "user": True,
        "groups": ["admins", "users"],
        "permissions": [],
        "session": "bridge-session",
        "apikey": None,
    }


async def settings(x_api_key: Optional[str] = Header(None)):
    check_key(x_api_key)
    return {
        "api": {"allowCrossOrigin": True},
        "appearance": {"name": "Repetier-Server"},
        "feature": {"sdSupport": False},
        "webcam": {"webcamEnabled": False, "streamUrl": "", "snapshotUrl": ""},
        "plugins": {},
        "printer": {"defaultExtrusionLength": 5},
        "temperature": {"profiles": []},
    }


async def printerprofiles(x_api_key: Optional[str] = Header(None)):
    check_key(x_api_key)
    return {
        "profiles": {
            "_default": {
                "id": "_default",
                "name": "Repetier printer",
                "current": True,
                "default": True,
                "model": "Repetier-Server",
                "heatedBed": True,
                "heatedChamber": False,
                "extruder": {"count": 1, "offsets": [[0.0, 0.0]]},
                "volume": {
                    "formFactor": "rectangular",
                    "origin": "lowerleft",
                    "width": 250,
                    "depth": 210,
                    "height": 210,
                },
            }
        }
    }


async def printer_state(slug: Optional[str] = None, x_api_key: Optional[str] = Header(None)):
    check_key(x_api_key)
    slug = resolve_slug(slug, x_api_key)
    entry = await printer_entry(slug)
    tools, bed = {}, {"actual": 0.0, "target": 0.0, "offset": 0}
    try:
        states = await rep_call("stateList", slug=slug, data={"includeHistory": False})
        st = states.get(slug, {}) if isinstance(states, dict) else {}
        for i, ex in enumerate(st.get("extruder", []) or []):
            tools[f"tool{i}"] = {
                "actual": ex.get("tempRead", 0.0),
                "target": ex.get("tempSet", 0.0),
                "offset": 0,
            }
        beds = st.get("heatedBeds", []) or []
        if beds:
            bed = {
                "actual": beds[0].get("tempRead", 0.0),
                "target": beds[0].get("tempSet", 0.0),
                "offset": 0,
            }
    except Exception as exc:  # noqa: BLE001
        log.warning("stateList failed: %s", exc)

    printing = bool(entry.get("job") and entry.get("job") != "none")
    paused = bool(entry.get("paused"))
    online = bool(entry.get("online"))
    text = "Printing" if printing and not paused else "Paused" if paused else (
        "Operational" if online else "Offline"
    )
    return {
        "state": {
            "text": text,
            "flags": {
                "operational": online,
                "printing": printing and not paused,
                "paused": paused,
                "ready": online and not printing,
                "error": False,
                "cancelling": False,
                "pausing": False,
                "sdReady": False,
                "closedOrError": not online,
            },
        },
        "temperature": {**tools, "bed": bed},
        "sd": {"ready": False},
    }


async def job_state(slug: Optional[str] = None, x_api_key: Optional[str] = Header(None)):
    check_key(x_api_key)
    slug = resolve_slug(slug, x_api_key)
    entry = await printer_entry(slug)
    job = entry.get("job") or None
    if job in ("none", ""):
        job = None
    done = float(entry.get("done") or 0.0)
    print_time = float(entry.get("printTime") or 0.0)
    remaining = float(entry.get("printedTimeComp") or 0.0)
    return {
        "job": {
            "file": {"name": job, "origin": "local", "path": job},
            "estimatedPrintTime": print_time or None,
            "filament": {},
        },
        "progress": {
            "completion": done,
            "printTime": int(print_time),
            "printTimeLeft": int(remaining),
            "filepos": 0,
        },
        "state": "Printing" if job else "Operational",
    }


async def upload_file(
    request: Request,
    slug: Optional[str] = None,
    file: UploadFile = File(...),
    select: Optional[str] = Form(None),
    print_: Optional[str] = Form(None, alias="print"),
    path: Optional[str] = Form(None),
    x_api_key: Optional[str] = Header(None),
):
    check_key(x_api_key)
    slug = resolve_slug(slug, x_api_key)

    def truthy(v):
        return str(v).lower() in ("true", "1", "yes", "on")

    start = truthy(print_)
    name = os.path.basename(file.filename or "upload.gcode")
    if name.lower().endswith(".bgcode"):
        raise HTTPException(
            415,
            "Repetier-Server cannot read binary G-code (.bgcode). "
            "Disable binary G-code in the print profile.",
        )
    content = await file.read()
    original = name
    name = build_filename(original, content, slug)
    if name != original:
        log.info("renamed %s -> %s", original, name)
    log.info("upload %s (%d bytes) slug=%s print=%s", name, len(content), slug, start)
    await rep_upload(slug, name, content, start_print=start)

    body = {
        "done": True,
        "effectiveSelect": truthy(select) or start,
        "effectivePrint": start,
        "files": {
            "local": {
                "name": name,
                "path": f"{path.rstrip('/') + '/' if path else ''}{name}",
                "origin": "local",
                "refs": {
                    "resource": f"{str(request.base_url).rstrip('/')}/api/files/local/{name}",
                    "download": None,
                },
            }
        },
    }
    return JSONResponse(body, status_code=201)


async def list_files(slug: Optional[str] = None, x_api_key: Optional[str] = Header(None)):
    check_key(x_api_key)
    slug = resolve_slug(slug, x_api_key)
    try:
        models = await rep_call("listModels", slug=slug, data={})
        items = models.get("data", models) if isinstance(models, dict) else models
    except Exception as exc:  # noqa: BLE001
        log.warning("listModels failed: %s", exc)
        items = []
    files = [
        {
            "name": m.get("name"),
            "path": m.get("name"),
            "type": "machinecode",
            "origin": "local",
            "size": m.get("length", 0),
        }
        for m in (items or [])
        if isinstance(m, dict)
    ]
    return {"files": files, "free": 1 << 30, "total": 1 << 31}


# --------------------------------------------------------------------------- #
# Route registration (bare + /p/<slug> prefixed)
# --------------------------------------------------------------------------- #

ROUTES = [
    ("/api/version", version, ["GET"]),
    ("/api/server", server_info, ["GET"]),
    ("/api/login", login, ["POST", "GET"]),
    ("/api/settings", settings, ["GET"]),
    ("/api/printerprofiles", printerprofiles, ["GET"]),
    ("/api/printer", printer_state, ["GET"]),
    ("/api/job", job_state, ["GET"]),
    ("/api/files", list_files, ["GET"]),
    ("/api/files/local", list_files, ["GET"]),
    ("/api/files/local", upload_file, ["POST"]),
    ("/api/files/sdcard", upload_file, ["POST"]),
]

for route, handler, methods in ROUTES:
    app.add_api_route(route, handler, methods=methods)
    app.add_api_route("/p/{slug}" + route, handler, methods=methods)


@app.get("/")
async def root():
    return {
        "service": "repetier-octoprint-bridge",
        "repetier": REPETIER_URL,
        "default_slug": DEFAULT_SLUG or None,
        "mapped_keys": list(KEY_MAP),
        "hint": "Point EasyPrint at this host as an OctoPrint instance.",
    }


@app.api_route("/{full_path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def catch_all(full_path: str, request: Request):
    """Log anything EasyPrint asks for that isn't implemented yet."""
    body = (await request.body())[:500]
    log.warning(
        "UNHANDLED %s /%s\n  headers=%s\n  body=%s",
        request.method,
        full_path,
        dict(request.headers),
        body,
    )
    return JSONResponse({"error": "Not implemented in bridge", "path": full_path}, status_code=404)


if __name__ == "__main__":
    log.info("Proxying to %s (default slug: %s)", REPETIER_URL, DEFAULT_SLUG or "-")
    uvicorn.run(app, host=LISTEN_HOST, port=LISTEN_PORT)

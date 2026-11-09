import json
import logging
import re
import azure.functions as func

app = func.FunctionApp(http_auth_level=func.AuthLevel.FUNCTION)

# Fallback: strip "__p01-02.curated.json" from the curated file name
SUFFIX_RE = re.compile(r"__p\d+-\d+\.curated\.json$", re.IGNORECASE)


def derive_pdf_name(source_json, file_name):
    if source_json and source_json.lower().endswith(".json"):
        return source_json[:-5] + ".pdf"
    if file_name and SUFFIX_RE.search(file_name):
        return SUFFIX_RE.sub("", file_name) + ".pdf"
    return None


@app.route(route="combine", methods=["POST"])
def combine(req: func.HttpRequest) -> func.HttpResponse:
    try:
        body = req.get_json()
        records = body["values"]
    except Exception:
        return func.HttpResponse("Invalid request body", status_code=400)

    results = []
    for rec in records:
        record_id = rec.get("recordId")
        try:
            data = rec.get("data") or {}
            pages = data.get("pages") or []
            pages = sorted(pages, key=lambda p: p.get("pageNumber") or 0)

            nums = [p["pageNumber"] for p in pages if p.get("pageNumber") is not None]
            texts = [(p.get("text") or "").strip() for p in pages]

            pdf_name = derive_pdf_name(data.get("sourceJson"), data.get("fileName"))
            warnings = []
            if not pdf_name:
                warnings.append({"message": "Could not derive PDF name"})
            if not nums:
                warnings.append({"message": "No pages found"})

            results.append({
                "recordId": record_id,
                "data": {
                    "contentText": "\n\n".join(t for t in texts if t),
                    "pageFrom": min(nums) if nums else None,
                    "pageTo": max(nums) if nums else None,
                    "sourcePdfName": pdf_name,
                },
                "errors": [],
                "warnings": warnings,
            })
        except Exception as ex:
            logging.exception("Record failed")
            results.append({
                "recordId": record_id,
                "data": {},
                "errors": [{"message": str(ex)}],
                "warnings": [],
            })

    return func.HttpResponse(
        json.dumps({"values": results}),
        mimetype="application/json",
        status_code=200,
    )
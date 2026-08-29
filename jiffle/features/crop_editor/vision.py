import base64
import json
import re

import requests
from PIL import Image, ImageOps

from .workflow import CropFailure, media_path

PROMPT = "Return only JSON with integer pixel coordinates {left, top, right, bottom}. The box must contain every visible detail, signature, watermark, shadow, and ingredient while removing only empty uniform margins."


def analyze_with_vision(connection, settings, media_id):
    if not settings.crop_vision_url or not settings.crop_vision_model:
        raise CropFailure("crop.vision_not_configured", "Crop vision model is not configured.")
    row=connection.execute("SELECT m.active_revision_id,r.file_path FROM media_items m JOIN media_revisions r ON r.id=m.active_revision_id WHERE m.id=? AND m.deleted_at IS NULL",(media_id,)).fetchone()
    if row is None: raise CropFailure("crop.media_not_found","Media item was not found.")
    path=media_path(settings.media_path,row["file_path"])
    if path is None or not path.is_file(): raise CropFailure("crop.file_missing","Media file is unavailable.")
    content=path.read_bytes(); mime=_mime(path.suffix.lower()); encoded=base64.b64encode(content).decode("ascii")
    try:
        if settings.crop_vision_format=="gemini":
            endpoint=f"{settings.crop_vision_url.rstrip('/')}/models/{settings.crop_vision_model}:generateContent"
            response=requests.post(endpoint,params={"key":settings.crop_vision_key},json={"contents":[{"parts":[{"text":PROMPT},{"inline_data":{"mime_type":mime,"data":encoded}}]}]},timeout=120); response.raise_for_status(); raw=response.json()["candidates"][0]["content"]["parts"][0]["text"]
        else:
            headers={"Content-Type":"application/json"};
            if settings.crop_vision_key: headers["Authorization"]=f"Bearer {settings.crop_vision_key}"
            response=requests.post(settings.crop_vision_url,headers=headers,json={"model":settings.crop_vision_model,"messages":[{"role":"user","content":[{"type":"text","text":PROMPT},{"type":"image_url","image_url":{"url":f"data:{mime};base64,{encoded}"}}]}]},timeout=120); response.raise_for_status(); raw=response.json()["choices"][0]["message"]["content"]
        payload=json.loads(re.sub(r"^```(?:json)?\s*|\s*```$","",raw.strip())); box=tuple(int(payload[k]) for k in ("left","top","right","bottom"))
        with Image.open(path) as source: width,height=ImageOps.exif_transpose(source).size
        if not (0<=box[0]<box[2]<=width and 0<=box[1]<box[3]<=height): raise ValueError
    except requests.HTTPError as error:
        status=error.response.status_code if error.response is not None else 0
        code="crop.vision_too_large" if status==413 else "crop.vision_failed"
        message="The original is too large for the vision model." if status==413 else "Vision analysis failed."
        raise CropFailure(code,message) from error
    except (requests.RequestException,KeyError,IndexError,TypeError,ValueError,json.JSONDecodeError) as error:
        raise CropFailure("crop.vision_invalid_response","Vision model did not return a safe crop box.") from error
    removed=100*(1-(box[2]-box[0])*(box[3]-box[1])/(width*height))
    connection.execute("INSERT INTO crop_analyses (media_item_id,revision_id,method,status,left_px,top_px,right_px,bottom_px,confidence,removed_area,parameters_json) VALUES (?,?,?,'pending',?,?,?,?,?,?,?) ON CONFLICT(revision_id,method) DO UPDATE SET left_px=excluded.left_px,top_px=excluded.top_px,right_px=excluded.right_px,bottom_px=excluded.bottom_px,confidence=excluded.confidence,removed_area=excluded.removed_area,status='pending'",(media_id,row["active_revision_id"],"vision",*box,70,removed,json.dumps({"model":settings.crop_vision_model}))); connection.commit()
    return connection.execute("SELECT * FROM crop_analyses WHERE revision_id=? AND method='vision'",(row["active_revision_id"],)).fetchone()


def _mime(suffix): return {".png":"image/png",".webp":"image/webp",".gif":"image/gif"}.get(suffix,"image/jpeg")

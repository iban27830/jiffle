from hashlib import sha256
import json
import os
from uuid import uuid4

from PIL import Image, ImageChops, ImageOps


class CropFailure(Exception):
    def __init__(self, code, message, details=None):
        super().__init__(message)
        self.code, self.message, self.details = code, message, details or {}


def media_path(root, stored):
    root = root.resolve()
    candidate = (root / stored).resolve()
    return candidate if candidate.is_relative_to(root) else None


def analyze_image(connection, settings, media_id, method="local", min_area=10, padding=.02, tolerance=18):
    row = connection.execute(
        "SELECT m.id,m.media_type,m.active_revision_id,r.file_path FROM media_items m "
        "JOIN media_revisions r ON r.id=m.active_revision_id WHERE m.id=? AND m.deleted_at IS NULL",
        (media_id,),
    ).fetchone()
    if row is None:
        raise CropFailure("crop.media_not_found", "Media item was not found.")
    if row["media_type"] != "image":
        raise CropFailure("crop.unsupported_media", "Only static images can be cropped.")
    decision = connection.execute("SELECT status FROM crop_analyses WHERE revision_id=? AND method=?",(row["active_revision_id"],method)).fetchone()
    if decision and decision["status"] in {"cropped","no_crop_needed"}:
        return None
    path = media_path(settings.media_path, row["file_path"])
    if path is None or not path.is_file():
        raise CropFailure("crop.file_missing", "Media file is unavailable.")
    if method != "local":
        raise CropFailure("crop.vision_unavailable", "Vision analysis is not configured.")
    try:
        with Image.open(path) as source:
            if getattr(source, "is_animated", False):
                raise CropFailure("crop.animated_unsupported", "Animated images are not scanned.")
            original = ImageOps.exif_transpose(source).convert("RGBA")
            ow, oh = original.size
            preview = original.copy()
            preview.thumbnail((1600, 1600), Image.Resampling.LANCZOS)
            box, confidence = _detect_box(preview, tolerance)
            sx, sy = ow / preview.width, oh / preview.height
            scaled = (int(box[0]*sx), int(box[1]*sy), min(ow, int(box[2]*sx+.999)), min(oh, int(box[3]*sy+.999)))
            left, top, right, bottom = _verify_full_resolution(original, scaled, tolerance)
            pad_x, pad_y = max(1, round(ow*padding)), max(1, round(oh*padding))
            left, top, right, bottom = max(0,left-pad_x), max(0,top-pad_y), min(ow,right+pad_x), min(oh,bottom+pad_y)
            removed = 100 * (1 - (right-left)*(bottom-top)/(ow*oh))
            if removed < min_area:
                return None
    except CropFailure:
        raise
    except (OSError, ValueError) as error:
        raise CropFailure("crop.decode_failed", "Image could not be analyzed.") from error
    parameters = json.dumps({"min_area":min_area,"padding":padding,"tolerance":tolerance,"source_size":[ow,oh],"algorithm_version":1})
    connection.execute(
        "INSERT INTO crop_analyses (media_item_id,revision_id,method,status,left_px,top_px,right_px,bottom_px,confidence,removed_area,parameters_json) "
        "VALUES (?,?,?,'pending',?,?,?,?,?,?,?) ON CONFLICT(revision_id,method) DO UPDATE SET "
        "left_px=excluded.left_px,top_px=excluded.top_px,right_px=excluded.right_px,bottom_px=excluded.bottom_px,confidence=excluded.confidence,removed_area=excluded.removed_area,parameters_json=excluded.parameters_json,"
        "status=CASE WHEN crop_analyses.status IN ('cropped','no_crop_needed') THEN crop_analyses.status ELSE 'pending' END",
        (media_id,row["active_revision_id"],method,left,top,right,bottom,confidence,removed,parameters),
    )
    connection.commit()
    return connection.execute("SELECT * FROM crop_analyses WHERE revision_id=? AND method=?", (row["active_revision_id"],method)).fetchone()


def _detect_box(image, tolerance):
    w,h=image.size; px=image.load(); corners=(px[0,0],px[w-1,0],px[0,h-1],px[w-1,h-1]); ref=tuple(sum(c[i] for c in corners)//4 for i in range(4))
    def background(pixel): return pixel[3]<12 or max(abs(pixel[i]-ref[i]) for i in range(3))<=tolerance
    ys=range(0,h,max(1,h//160)); xs=range(0,w,max(1,w//160)); left=0
    while left<w and sum(not background(px[left,y]) for y in ys)<=1:left+=1
    right=w
    while right>left and sum(not background(px[right-1,y]) for y in ys)<=1:right-=1
    top=0
    while top<h and sum(not background(px[x,top]) for x in xs)<=1:top+=1
    bottom=h
    while bottom>top and sum(not background(px[x,bottom-1]) for x in xs)<=1:bottom-=1
    confidence=max(1,min(99,75+(left+w-right+top+h-bottom)*20/max(w+h,1)))
    return (left,top,right,bottom),confidence


def _verify_full_resolution(image, box, tolerance):
    w,h=image.size; refs=(image.getpixel((0,0)),image.getpixel((w-1,0)),image.getpixel((0,h-1)),image.getpixel((w-1,h-1)))
    rgb=image.convert("RGB"); masks=[]
    for ref in refs:
        difference=ImageChops.difference(rgb,Image.new("RGB",image.size,ref[:3])); channels=difference.split(); maximum=ImageChops.lighter(channels[0],ImageChops.lighter(channels[1],channels[2])); masks.append(maximum.point(lambda value:255 if value>tolerance else 0))
    detail=masks[0]
    for mask in masks[1:]: detail=ImageChops.darker(detail,mask)
    visible=image.getchannel("A").point(lambda value:255 if value>=12 else 0); detail=ImageChops.darker(detail,visible); bounds=detail.getbbox()
    if not bounds:return box
    left,top,right,bottom=box; return min(left,bounds[0]),min(top,bounds[1]),max(right,bounds[2]),max(bottom,bounds[3])


def apply_crop(connection, settings, analysis_id, box):
    row=connection.execute("SELECT a.*,m.active_revision_id,r.file_path FROM crop_analyses a JOIN media_items m ON m.id=a.media_item_id JOIN media_revisions r ON r.id=m.active_revision_id WHERE a.id=? AND a.status='pending' AND a.revision_id=m.active_revision_id",(analysis_id,)).fetchone()
    if row is None: raise CropFailure("crop.stale_analysis","Crop analysis is missing or stale.")
    source=media_path(settings.media_path,row["file_path"])
    if source is None or not source.is_file(): raise CropFailure("crop.file_missing","Media file is unavailable.")
    try: left,top,right,bottom=map(int,box)
    except (TypeError,ValueError): raise CropFailure("crop.invalid_box","Crop box must contain four integers.")
    target=None
    try:
        with Image.open(source) as opened:
            image=ImageOps.exif_transpose(opened); original_format=opened.format or "PNG"
            if not (0<=left<right<=image.width and 0<=top<bottom<=image.height): raise CropFailure("crop.invalid_box","Crop box is outside the image.")
            cropped=image.crop((left,top,right,bottom)); suffix=source.suffix.lower(); directory=settings.media_path/"revisions"; directory.mkdir(parents=True,exist_ok=True)
            target=directory/f"media-{row['media_item_id']}-{analysis_id}-{uuid4().hex}{suffix}"; temporary=target.with_suffix(target.suffix+".tmp"); kwargs={}
            if suffix in {".jpg",".jpeg"}: kwargs={"quality":95,"subsampling":0,"icc_profile":opened.info.get("icc_profile"),"exif":opened.getexif().tobytes()}
            elif opened.info.get("icc_profile"): kwargs["icc_profile"]=opened.info["icc_profile"]
            cropped.save(temporary,format=original_format,**{k:v for k,v in kwargs.items() if v}); os.replace(temporary,target)
        digest=_hash_file(target); duplicate=connection.execute("SELECT id FROM media_items WHERE content_hash=? AND id<>? AND deleted_at IS NULL",(digest,row["media_item_id"])).fetchone()
        if duplicate: target.unlink(missing_ok=True); raise CropFailure("crop.duplicate_result","The cropped result already exists in the library.",{"media_id":int(duplicate[0])})
        rel=target.relative_to(settings.media_path).as_posix(); size=target.stat().st_size
        details=json.dumps({"analysis_id":analysis_id,"box":[left,top,right,bottom],"method":row["method"],"source_revision_id":row["active_revision_id"]})
        cursor=connection.execute("INSERT INTO media_revisions (media_item_id,parent_revision_id,file_path,operation,width,height,file_size,content_hash,details_json) VALUES (?,?,?,?,?,?,?,?,?)",(row["media_item_id"],row["active_revision_id"],rel,"crop",cropped.width,cropped.height,size,digest,details)); revision_id=int(cursor.lastrowid)
        connection.execute("UPDATE media_items SET active_revision_id=?,file_path=?,width=?,height=?,file_size=?,content_hash=? WHERE id=?",(revision_id,rel,cropped.width,cropped.height,size,digest,row["media_item_id"])); connection.execute("UPDATE crop_analyses SET status='cropped',resolved_at=CURRENT_TIMESTAMP WHERE id=?",(analysis_id,)); connection.execute("DELETE FROM media_fingerprints WHERE media_item_id=?",(row["media_item_id"],)); connection.execute("INSERT INTO operation_history (event_type,entity_type,entity_id,details_json) VALUES ('crop.applied','media',?,?)",(row["media_item_id"],json.dumps({"analysis_id":analysis_id,"revision_id":revision_id,"box":[left,top,right,bottom]}))); connection.commit(); return revision_id
    except Exception:
        connection.rollback()
        if target: target.unlink(missing_ok=True)
        raise


def list_revisions(connection, media_id):
    return connection.execute("SELECT r.*,r.id=(SELECT active_revision_id FROM media_items WHERE id=?) active FROM media_revisions r WHERE r.media_item_id=? ORDER BY r.id DESC",(media_id,media_id)).fetchall()


def activate_revision(connection, settings, media_id, revision_id):
    row=connection.execute("SELECT * FROM media_revisions WHERE id=? AND media_item_id=?",(revision_id,media_id)).fetchone()
    if row is None: raise CropFailure("crop.revision_not_found","Revision was not found.")
    path=media_path(settings.media_path,row["file_path"])
    if path is None or not path.is_file(): raise CropFailure("crop.file_missing","Revision file is unavailable.")
    connection.execute("UPDATE media_items SET active_revision_id=?,file_path=?,width=?,height=?,file_size=?,content_hash=? WHERE id=?",(revision_id,row["file_path"],row["width"],row["height"],row["file_size"],row["content_hash"],media_id)); connection.execute("DELETE FROM media_fingerprints WHERE media_item_id=?",(media_id,)); connection.execute("INSERT INTO operation_history (event_type,entity_type,entity_id,details_json) VALUES ('revision.activated','media',?,?)",(media_id,json.dumps({"revision_id":revision_id}))); connection.commit()


def reset_to_original(connection, settings, media_id):
    row=connection.execute("SELECT id FROM media_revisions WHERE media_item_id=? AND parent_revision_id IS NULL AND operation='original' ORDER BY id LIMIT 1",(media_id,)).fetchone()
    if row is None: raise CropFailure("crop.original_not_found","The original version was not found.")
    activate_revision(connection,settings,media_id,int(row["id"]))
    connection.execute("INSERT INTO operation_history (event_type,entity_type,entity_id,details_json) VALUES ('editor.reset','media',?,'{}')",(media_id,)); connection.commit()
    return int(row["id"])


def resolve_analysis(connection, analysis_id, status):
    if status not in {"no_crop_needed","deferred"}: raise CropFailure("crop.invalid_status","Crop status is invalid.")
    cursor=connection.execute("UPDATE crop_analyses SET status=?,resolved_at=CURRENT_TIMESTAMP WHERE id=? AND status='pending'",(status,analysis_id))
    if cursor.rowcount!=1: raise CropFailure("crop.analysis_not_found","Pending crop analysis was not found.")
    connection.execute("INSERT INTO operation_history (event_type,entity_type,entity_id,details_json) VALUES (?,?,?,?)",(f"crop.{status}","crop_analysis",analysis_id,"{}"))
    connection.commit()


def reset_analysis(connection, analysis_id):
    cursor=connection.execute("UPDATE crop_analyses SET status='pending',resolved_at=NULL WHERE id=?",(analysis_id,))
    if cursor.rowcount!=1: raise CropFailure("crop.analysis_not_found","Crop analysis was not found.")
    connection.commit()


def _hash_file(path):
    digest=sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda:file.read(1024*1024),b""): digest.update(chunk)
    return digest.hexdigest()

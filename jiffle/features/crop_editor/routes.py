import json
from hashlib import sha256
import sqlite3
from threading import Thread
from flask import Blueprint, current_app, jsonify, request, send_file
from jiffle.infrastructure.database.connection import get_database
from .workflow import CropFailure, activate_revision, analyze_image, apply_crop, list_revisions, media_path, reset_analysis, reset_to_original, resolve_analysis
from jiffle.infrastructure.media_revisions import active_edit_operations, revision_details
from .vision import analyze_with_vision

crop_blueprint=Blueprint("crop_editor",__name__)

@crop_blueprint.post("/api/v1/crop-analyses")
def create_analysis():
    payload=request.get_json(silent=True) or {}; media_id=payload.get("media_id")
    try:
        row=analyze_image(get_database(),current_app.config["JIFFLE_SETTINGS"],int(media_id),"local",float(payload.get("min_area",10)),float(payload.get("padding",.02)),int(payload.get("tolerance",18)))
    except (TypeError,ValueError,CropFailure) as e: return _crop_error(e)
    return jsonify(_serialize(row) if row else {"status":"no_candidate"}),201

@crop_blueprint.post("/api/v1/crop-scan-jobs")
def create_scan_job():
    payload=request.get_json(silent=True) or {}
    parameters={"min_area":float(payload.get("min_area",10)),"padding":float(payload.get("padding",.02)),"tolerance":int(payload.get("tolerance",18)),"algorithm_version":1}
    connection=get_database(); cursor=connection.execute("INSERT INTO background_jobs (job_type,status,result_json) VALUES ('crop_scan','pending',?)",(json.dumps(parameters),)); job_id=int(cursor.lastrowid); connection.execute("INSERT INTO crop_scan_jobs (job_id,parameters_json) VALUES (?,?)",(job_id,json.dumps(parameters))); connection.commit()
    settings=current_app.config["JIFFLE_SETTINGS"]; args=(settings.database_path,settings,job_id,parameters)
    if settings.run_jobs_inline: _run_scan(*args)
    else: Thread(target=_run_scan,args=args,daemon=True).start()
    return jsonify({"job_id":job_id,"status_url":f"/api/v1/jobs/{job_id}"}),202

@crop_blueprint.post("/api/v1/media/<int:media_id>/crop-vision-analysis")
def create_vision_analysis(media_id):
    try: row=analyze_with_vision(get_database(),current_app.config["JIFFLE_SETTINGS"],media_id)
    except CropFailure as error:return _crop_error(error)
    return jsonify(_serialize(row)),201

@crop_blueprint.post("/api/v1/crop-scan-jobs/<int:job_id>/cancel")
def cancel_scan(job_id):
    cursor=get_database().execute("UPDATE crop_scan_jobs SET cancel_requested=1 WHERE job_id=?",(job_id,)); get_database().commit()
    if cursor.rowcount!=1:return _error("crop.scan_not_found","Crop scan was not found.",404)
    return jsonify({"status":"cancelling"})

@crop_blueprint.get("/api/v1/crop-scan-jobs/active")
def active_scan():
    row=get_database().execute(
        "SELECT b.id,b.status,b.progress,c.cancel_requested,c.scanned_count,c.candidate_count "
        "FROM background_jobs b JOIN crop_scan_jobs c ON c.job_id=b.id "
        "WHERE b.status IN ('pending','running') ORDER BY b.id DESC LIMIT 1"
    ).fetchone()
    if row is None:return jsonify({"job":None})
    total=get_database().execute("SELECT COUNT(*) FROM media_items WHERE deleted_at IS NULL AND media_type='image'").fetchone()[0]
    return jsonify({"job":{"id":row["id"],"status":row["status"],"progress":row["progress"],"cancel_requested":bool(row["cancel_requested"]),"scanned":row["scanned_count"],"candidates":row["candidate_count"],"total":total,"status_url":f"/api/v1/jobs/{row['id']}"}})

@crop_blueprint.get("/api/v1/crop-analyses")
def list_analyses():
    status=request.args.get("status","pending")
    where="" if status=="all" else "WHERE a.status=?"; params=() if status=="all" else (status,)
    rows=get_database().execute(f"SELECT a.*,r.width media_width,r.height media_height FROM crop_analyses a JOIN media_revisions r ON r.id=a.revision_id {where} ORDER BY a.confidence DESC,a.removed_area DESC",params).fetchall()
    return jsonify({"items":[_serialize(r) for r in rows]})

@crop_blueprint.get("/api/v1/crop-analyses/<int:analysis_id>")
def get_analysis(analysis_id):
    row=get_database().execute(
        "SELECT a.*,r.width media_width,r.height media_height FROM crop_analyses a "
        "JOIN media_revisions r ON r.id=a.revision_id WHERE a.id=?",
        (analysis_id,),
    ).fetchone()
    if row is None:return _error("crop.analysis_not_found","Crop analysis was not found.",404)
    return jsonify(_serialize(row))

@crop_blueprint.post("/api/v1/crop-analyses/<int:analysis_id>/apply")
def apply(analysis_id):
    payload=request.get_json(silent=True) or {}; box=payload.get("box")
    try: revision=apply_crop(get_database(),current_app.config["JIFFLE_SETTINGS"],analysis_id,box)
    except (TypeError,ValueError,CropFailure) as e: return _crop_error(e)
    return jsonify({"status":"cropped","revision_id":revision})

@crop_blueprint.post("/api/v1/crop-analyses/<int:analysis_id>/<status>")
def resolve(analysis_id,status):
    try: resolve_analysis(get_database(),analysis_id,status)
    except CropFailure as e: return _crop_error(e)
    return jsonify({"status":status})

@crop_blueprint.post("/api/v1/crop-analyses/<int:analysis_id>/reset")
def reset(analysis_id):
    try:reset_analysis(get_database(),analysis_id)
    except CropFailure as error:return _crop_error(error)
    return jsonify({"status":"pending"})

@crop_blueprint.get("/api/v1/media/<int:media_id>/revisions")
def revisions(media_id):
    rows=list_revisions(get_database(),media_id)
    active=next((r for r in rows if r["active"]),None)
    active_chain=set()
    by_id={int(r["id"]):r for r in rows}
    cursor=active
    while cursor:
        active_chain.add(int(cursor["id"])); cursor=by_id.get(int(cursor["parent_revision_id"])) if cursor["parent_revision_id"] else None
    return jsonify({"items":[{"id":r["id"],"parent_revision_id":r["parent_revision_id"],"operation":r["operation"],"details":revision_details(r["details_json"]),"width":r["width"],"height":r["height"],"file_size":r["file_size"],"created_at":r["created_at"],"active":bool(r["active"]),"in_active_chain":int(r["id"]) in active_chain,"content_url":f"/api/v1/media/{media_id}/revisions/{r['id']}/content"} for r in rows]})

@crop_blueprint.get("/api/v1/media/<int:media_id>/editor-state")
def editor_state(media_id):
    connection=get_database(); media=connection.execute("SELECT id,active_revision_id FROM media_items WHERE id=? AND deleted_at IS NULL AND media_type='image'",(media_id,)).fetchone()
    if media is None:return _error("crop.media_not_found","Media item was not found.",404)
    analyses=connection.execute("SELECT id,revision_id,status,method FROM crop_analyses WHERE media_item_id=? ORDER BY id DESC",(media_id,)).fetchall()
    operations=active_edit_operations(connection,media_id)
    return jsonify({"media_id":media_id,"active_revision_id":media["active_revision_id"],"is_edited":bool(operations),"edit_operations":list(operations),"analyses":[dict(row) for row in analyses],"content_url":f"/api/v1/media/{media_id}/content?revision={media['active_revision_id']}"})

@crop_blueprint.post("/api/v1/media/<int:media_id>/reset-to-original")
def reset_media(media_id):
    try:revision_id=reset_to_original(get_database(),current_app.config["JIFFLE_SETTINGS"],media_id)
    except CropFailure as error:return _crop_error(error)
    return jsonify({"status":"reset","revision_id":revision_id})

@crop_blueprint.get("/api/v1/media/<int:media_id>/revisions/<int:revision_id>/content")
def revision_content(media_id,revision_id):
    row=get_database().execute("SELECT file_path FROM media_revisions WHERE id=? AND media_item_id=?",(revision_id,media_id)).fetchone(); path=media_path(current_app.config["JIFFLE_SETTINGS"].media_path,row["file_path"]) if row else None
    if path is None or not path.is_file():return _error("crop.revision_not_found","Revision was not found.",404)
    return send_file(path,conditional=True)

@crop_blueprint.post("/api/v1/media/<int:media_id>/revisions/<int:revision_id>/activate")
def restore_revision(media_id,revision_id):
    try:activate_revision(get_database(),current_app.config["JIFFLE_SETTINGS"],media_id,revision_id)
    except CropFailure as error:return _crop_error(error)
    return jsonify({"status":"activated","revision_id":revision_id})

def _serialize(row):
    return {"id":row["id"],"media_id":row["media_item_id"],"revision_id":row["revision_id"],"method":row["method"],"status":row["status"],"box":[row["left_px"],row["top_px"],row["right_px"],row["bottom_px"]],"confidence":row["confidence"],"removed_area":row["removed_area"],"width":row["media_width"] if "media_width" in row.keys() else None,"height":row["media_height"] if "media_height" in row.keys() else None,"content_url":f"/api/v1/media/{row['media_item_id']}/revisions/{row['revision_id']}/content","thumbnail_url":f"/api/v1/media/{row['media_item_id']}/revisions/{row['revision_id']}/content"}
def _error(code,message,status): return jsonify({"error":{"code":code,"message":message,"details":{}}}),status

def _crop_error(error):
    if not isinstance(error,CropFailure):return _error("crop.invalid_request",str(error),400)
    status=404 if error.code.endswith(("not_found","file_missing")) else 409 if error.code in {"crop.stale_analysis","crop.duplicate_result"} else 400
    return jsonify({"error":{"code":error.code,"message":error.message,"details":error.details}}),status

def _run_scan(database_path,settings,job_id,parameters):
    connection=sqlite3.connect(database_path); connection.row_factory=sqlite3.Row; connection.execute("PRAGMA foreign_keys=ON")
    try:
        rows=connection.execute("SELECT id,active_revision_id FROM media_items WHERE deleted_at IS NULL AND media_type='image' ORDER BY id").fetchall(); state=connection.execute("SELECT scanned_count,candidate_count FROM crop_scan_jobs WHERE job_id=?",(job_id,)).fetchone(); start=int(state[0] or 0); candidates=int(state[1] or 0); signature=_scan_signature(parameters); connection.execute("UPDATE background_jobs SET status='running',started_at=COALESCE(started_at,CURRENT_TIMESTAMP) WHERE id=?",(job_id,)); connection.commit()
        for index,row in enumerate(rows[start:],start=start):
            if connection.execute("SELECT cancel_requested FROM crop_scan_jobs WHERE job_id=?",(job_id,)).fetchone()[0]:
                result=json.dumps({"scanned":index,"candidates":candidates,"cancelled":True}); connection.execute("UPDATE background_jobs SET status='completed',progress=100,result_json=?,finished_at=CURRENT_TIMESTAMP WHERE id=?",(result,job_id)); connection.commit(); return
            cached=connection.execute("SELECT candidate_found FROM crop_scan_results WHERE revision_id=? AND parameter_signature=?",(row["active_revision_id"],signature)).fetchone()
            try:
                if cached is None:
                    found=analyze_image(connection,settings,int(row["id"]),min_area=parameters["min_area"],padding=parameters["padding"],tolerance=parameters["tolerance"]); candidates+=int(found is not None)
                    connection.execute("INSERT OR REPLACE INTO crop_scan_results (revision_id,parameter_signature,candidate_found) VALUES (?,?,?)",(row["active_revision_id"],signature,int(found is not None)))
            except CropFailure:pass
            progress=1+int(98*(index+1)/max(len(rows),1)); connection.execute("UPDATE background_jobs SET progress=? WHERE id=?",(progress,job_id)); connection.execute("UPDATE crop_scan_jobs SET scanned_count=?,candidate_count=? WHERE job_id=?",(index+1,candidates,job_id)); connection.commit()
        result=json.dumps({"scanned":len(rows),"candidates":candidates,"cancelled":False}); connection.execute("UPDATE background_jobs SET status='completed',progress=100,result_json=?,finished_at=CURRENT_TIMESTAMP WHERE id=?",(result,job_id)); connection.commit()
    except Exception:
        connection.rollback(); connection.execute("UPDATE background_jobs SET status='failed',error_code='crop.scan_failed',error_message='Crop scan failed.',finished_at=CURRENT_TIMESTAMP WHERE id=?",(job_id,)); connection.commit(); raise
    finally:connection.close()

def _scan_signature(parameters):
    canonical={"algorithm_version":int(parameters.get("algorithm_version",1)),"min_area":float(parameters["min_area"]),"padding":float(parameters["padding"]),"tolerance":int(parameters["tolerance"])}
    return sha256(json.dumps(canonical,sort_keys=True,separators=(",", ":")).encode("utf-8")).hexdigest()

def resume_crop_scans(database_path,settings):
    connection=sqlite3.connect(database_path); connection.row_factory=sqlite3.Row
    try:
        rows=connection.execute("SELECT b.id,c.parameters_json FROM background_jobs b JOIN crop_scan_jobs c ON c.job_id=b.id WHERE b.status IN ('pending','running') AND c.cancel_requested=0 ORDER BY b.id").fetchall()
    finally:connection.close()
    for row in rows:
        Thread(target=_run_scan,args=(database_path,settings,int(row["id"]),json.loads(row["parameters_json"])),daemon=True).start()

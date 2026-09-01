import json
import shutil
from pathlib import Path
from uuid import uuid4
from flask import Blueprint, current_app, jsonify, request, send_file
from PIL import Image, ImageFilter
from jiffle.infrastructure.database.connection import get_database
from jiffle.features.crop_editor.workflow import CropFailure, media_path, _hash_file

background_blueprint = Blueprint('background_editor', __name__)

def _root(settings):
    path = settings.database_path.parent / 'backgrounds'; path.mkdir(parents=True, exist_ok=True); return path

@background_blueprint.get('/api/v1/background-assets')
def list_assets():
    rows = get_database().execute('SELECT * FROM background_assets ORDER BY id DESC').fetchall()
    return jsonify({'items':[dict(r, content_url=f'/api/v1/background-assets/{r["id"]}/content') for r in rows]})

@background_blueprint.post('/api/v1/background-assets/import')
def import_asset():
    payload=request.get_json(silent=True) or {}; source=Path(str(payload.get('path',''))).expanduser()
    if not source.is_file(): return _error('background.file_missing','Background file was not found.',404)
    try:
        with Image.open(source) as image: width,height=image.size
    except Exception: return _error('background.decode_failed','Background image could not be opened.',400)
    target=_root(current_app.config['JIFFLE_SETTINGS']) / f'{uuid4().hex}{source.suffix.lower()}'
    shutil.copy2(source,target); db=get_database(); cur=db.execute('INSERT INTO background_assets(file_path,original_name,width,height) VALUES (?,?,?,?)',(str(target.relative_to(_root(current_app.config['JIFFLE_SETTINGS']))),source.name,width,height)); db.commit()
    return jsonify({'id':cur.lastrowid}),201

@background_blueprint.get('/api/v1/background-assets/<int:asset_id>/content')
def asset_content(asset_id):
    row=get_database().execute('SELECT file_path FROM background_assets WHERE id=?',(asset_id,)).fetchone()
    path=media_path(_root(current_app.config['JIFFLE_SETTINGS']),row['file_path']) if row else None
    if not path or not path.is_file(): return _error('background.not_found','Background was not found.',404)
    return send_file(path,conditional=True)

@background_blueprint.post('/api/v1/media/<int:media_id>/background-compose')
def compose(media_id):
    payload=request.get_json(silent=True) or {}; asset_id=int(payload.get('background_id',0)); blur=max(0,min(100,float(payload.get('blur',0))))
    db=get_database(); row=db.execute('SELECT m.active_revision_id,r.file_path FROM media_items m JOIN media_revisions r ON r.id=m.active_revision_id WHERE m.id=? AND m.media_type="image" AND m.deleted_at IS NULL',(media_id,)).fetchone(); bg=db.execute('SELECT file_path FROM background_assets WHERE id=?',(asset_id,)).fetchone()
    if not row or not bg:return _error('background.invalid_request','Media or background was not found.',404)
    settings=current_app.config['JIFFLE_SETTINGS']; source=media_path(settings.media_path,row['file_path']); background=media_path(_root(settings),bg['file_path'])
    try:
        with Image.open(source) as fg, Image.open(background) as bgim:
            fg=fg.convert('RGBA'); bgim=bgim.convert('RGBA').resize(fg.size,Image.Resampling.LANCZOS)
            if blur: bgim=bgim.filter(ImageFilter.GaussianBlur(blur/5))
            # Prefer RMBG-2.0 when installed; it returns an alpha matte.
            try:
                from rembg import remove
                fg=remove(fg)
            except ImportError: return _error('background.model_missing','Install the local RMBG-2.0 model runtime to remove backgrounds.',409)
            result=Image.alpha_composite(bgim,fg); target=settings.media_path/'revisions'/f'media-{media_id}-background-{uuid4().hex}.png'; target.parent.mkdir(parents=True,exist_ok=True); result.save(target,'PNG')
    except Exception as error: return _error('background.compose_failed',str(error),400)
    digest=_hash_file(target); rel=target.relative_to(settings.media_path).as_posix(); size=target.stat().st_size; details=json.dumps({'background_id':asset_id,'blur':blur,'model':'RMBG-2.0','source_revision_id':row['active_revision_id']})
    cur=db.execute('INSERT INTO media_revisions(media_item_id,parent_revision_id,file_path,operation,width,height,file_size,content_hash,details_json) VALUES (?,?,?,?,?,?,?,?,?)',(media_id,row['active_revision_id'],rel,'background_replace',result.width,result.height,size,digest,details)); rid=cur.lastrowid; db.execute('UPDATE media_items SET active_revision_id=?,file_path=?,width=?,height=?,file_size=?,content_hash=? WHERE id=?',(rid,rel,result.width,result.height,size,digest,media_id)); db.commit(); return jsonify({'status':'completed','revision_id':rid})

def _error(code,message,status): return jsonify({'error':{'code':code,'message':message,'details':{}}}),status

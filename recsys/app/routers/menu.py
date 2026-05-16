from fastapi import APIRouter, UploadFile, File, Form, Depends, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session
from app.database import get_db, Restaurant, Menu
from app.services.pdf_parser import parse_menu_pdf as extract_menu
from app.routers.plivo_hooks import bust_menu_cache
import json, os, shutil

router = APIRouter()
UPLOAD_DIR = os.path.expanduser("~/work/recsys/data/uploads")

@router.post("/api/menu/upload")
async def upload_menu(
    restaurant_name: str = Form(...),
    plivo_number: str = Form(...),
    file: UploadFile = File(...),
    db: Session = Depends(get_db)
):
    if not file.filename.endswith('.pdf'):
        raise HTTPException(400, "Only PDF files accepted")

    # Read bytes (needed for both save and parse)
    pdf_bytes = await file.read()

    # Save PDF
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    path = os.path.join(UPLOAD_DIR, file.filename)
    with open(path, 'wb') as f:
        f.write(pdf_bytes)

    # Parse (passes bytes so vision fallback can also convert pages)
    parsed = extract_menu(pdf_bytes)

    # Upsert restaurant
    rest = db.query(Restaurant).filter(Restaurant.plivo_number == plivo_number).first()
    if not rest:
        rest = Restaurant(name=restaurant_name, plivo_number=plivo_number)
        db.add(rest)
        db.commit()
        db.refresh(rest)
    else:
        rest.name = restaurant_name
        db.commit()

    # Save menu
    menu = Menu(
        restaurant_id=rest.id,
        filename=file.filename,
        raw_text=parsed['raw_text'],
        items_json=json.dumps(parsed['items'])
    )
    db.add(menu); db.commit()
    bust_menu_cache(plivo_number)

    return {"status": "ok", "restaurant_id": rest.id,
            "items_found": len(parsed['items']), "filename": file.filename}

@router.get("/api/menu/list")
def list_menus(db: Session = Depends(get_db)):
    restaurants = db.query(Restaurant).all()
    result = []
    for r in restaurants:
        menu = db.query(Menu).filter(Menu.restaurant_id == r.id).order_by(Menu.id.desc()).first()
        result.append({
            "id": r.id, "name": r.name, "plivo_number": r.plivo_number,
            "menu_file": menu.filename if menu else None,
            "items": len(json.loads(menu.items_json)) if menu and menu.items_json else 0,
            "uploaded_at": str(menu.uploaded_at) if menu else None
        })
    return result

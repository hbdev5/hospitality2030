"""
Migrates Curry Bliss (apppod_beta merchantId=51934) menu into recsys structured
schema. Idempotent — re-running clears the target restaurant's menu and re-imports.
"""
import sys, pymysql
sys.path.insert(0, '/home/azureuser/work/recsys')
from app.database import (
    SessionLocal, init_db, Restaurant,
    MenuCategory, MenuItem, MenuModifierGroup, MenuModifierOption, MenuItemModifierGroup,
)
from sqlalchemy import text as sql_text

SRC_MERCHANT_ID = 51934
TARGET_RESTAURANT_ID = 1
TARGET_RESTAURANT_NAME = "Curry Bliss"
RESET_FIRST = True  # wipe target restaurant's menu before importing


def main():
    init_db()
    db = SessionLocal()

    if RESET_FIRST:
        print(f"Wiping existing menu for restaurant {TARGET_RESTAURANT_ID}…")
        # Delete in dependency order: link table → options → groups → items → categories
        db.execute(sql_text(
            "DELETE m FROM menu_item_modifier_groups m "
            "JOIN menu_items i ON i.id = m.item_id "
            "WHERE i.restaurant_id = :rid"), {"rid": TARGET_RESTAURANT_ID})
        db.execute(sql_text(
            "DELETE o FROM menu_modifier_options o "
            "JOIN menu_modifier_groups g ON g.id = o.group_id "
            "WHERE g.restaurant_id = :rid"), {"rid": TARGET_RESTAURANT_ID})
        db.execute(sql_text(
            "DELETE FROM menu_modifier_groups WHERE restaurant_id = :rid"),
            {"rid": TARGET_RESTAURANT_ID})
        db.execute(sql_text(
            "DELETE FROM menu_items WHERE restaurant_id = :rid"),
            {"rid": TARGET_RESTAURANT_ID})
        db.execute(sql_text(
            "DELETE FROM menu_categories WHERE restaurant_id = :rid"),
            {"rid": TARGET_RESTAURANT_ID})
        db.commit()

    # Rename the restaurant so callers know what they're hearing
    rest = db.query(Restaurant).filter(Restaurant.id == TARGET_RESTAURANT_ID).first()
    if rest:
        rest.name = TARGET_RESTAURANT_NAME
        db.commit()
        print(f"Renamed restaurant {TARGET_RESTAURANT_ID} → {TARGET_RESTAURANT_NAME}")

    # Source: apppod_beta (Java production DB on same MySQL host)
    src = pymysql.connect(host='localhost', user='root', password='change12@',
                          database='apppod_beta', cursorclass=pymysql.cursors.DictCursor)
    sc = src.cursor()

    # ── Categories ────────────────────────────────────────────────────────
    sc.execute("SELECT CATEGORYID, CATEGORY FROM CATEGORYMERCHANT WHERE MERCHANTID=%s ORDER BY CATEGORY",
               (SRC_MERCHANT_ID,))
    cat_rows = sc.fetchall()
    cat_id_map = {}   # external_id (CATEGORYID) → new local id
    for i, row in enumerate(cat_rows):
        cat = MenuCategory(
            restaurant_id = TARGET_RESTAURANT_ID,
            name          = (row['CATEGORY'] or 'General').strip(),
            sort_order    = i,
            external_id   = row['CATEGORYID'],
        )
        db.add(cat)
        db.flush()
        cat_id_map[row['CATEGORYID']] = cat.id
    print(f"Imported {len(cat_rows)} categories")

    # ── Items ─────────────────────────────────────────────────────────────
    sc.execute(
        "SELECT INVENTORYID, NAME, DESCRIPTION, PRICE, CATEGORYID "
        "FROM INVENTORYMERCHANT WHERE MERCHANTID=%s ORDER BY NAME", (SRC_MERCHANT_ID,))
    item_rows = sc.fetchall()
    item_id_map = {}
    for row in item_rows:
        # Skip the duplicate "Regular (T-Bone Steak)" with NULL category
        if not row.get('NAME'):
            continue
        item = MenuItem(
            restaurant_id = TARGET_RESTAURANT_ID,
            category_id   = cat_id_map.get(row['CATEGORYID']),
            name          = row['NAME'].strip(),
            price_cents   = int(row['PRICE'] or 0),
            description   = (row['DESCRIPTION'] or '').strip() or None,
            external_id   = row['INVENTORYID'],
        )
        db.add(item)
        db.flush()
        item_id_map[row['INVENTORYID']] = item.id
    print(f"Imported {len(item_id_map)} items")

    # ── Required/min/max per group (from COMBOITEMS — the source of truth for
    #    combo configuration: required = MINMAX>0, single-select when MAXIMUM==1).
    grp_minmax = {}   # groupname.lower() -> (min_select, max_select)
    try:
        sc.execute(
            "SELECT GROUPNAME, MAX(MINMAX) mn, MAX(MAXIMUM) mx "
            "FROM COMBOITEMS WHERE MERCHANTID=%s AND GROUPNAME IS NOT NULL "
            "GROUP BY GROUPNAME", (SRC_MERCHANT_ID,))
        for r in sc.fetchall():
            grp_minmax[(r['GROUPNAME'] or '').strip().lower()] = (
                int(r['mn'] or 0), int(r['mx'] or 0))
    except Exception as e:
        print(f"COMBOITEMS min/max lookup skipped: {e}")

    # ── Modifier groups (distinct) ────────────────────────────────────────
    sc.execute(
        "SELECT DISTINCT MODIFIERGROUPID, MODIFIERGROUPNAME, SELECTIONTYPE "
        "FROM MODIFIERMERCHANT WHERE MERCHANTID=%s AND MODIFIERGROUPID IS NOT NULL",
        (SRC_MERCHANT_ID,))
    grp_rows = sc.fetchall()
    grp_id_map = {}
    for row in grp_rows:
        gname = (row['MODIFIERGROUPNAME'] or 'Options').strip()
        mn, mx = grp_minmax.get(gname.lower(), (0, 0))
        sel = (row.get('SELECTIONTYPE') or '').strip().lower()
        selection_type = 'single' if (sel == 'single' or mx == 1) else 'multi'
        grp = MenuModifierGroup(
            restaurant_id = TARGET_RESTAURANT_ID,
            name          = gname,
            selection_type= selection_type,
            min_select    = mn,                       # >=1 => required
            max_select    = mx if mx > 0 else 99,
            external_id   = row['MODIFIERGROUPID'],
        )
        db.add(grp)
        db.flush()
        grp_id_map[row['MODIFIERGROUPID']] = grp.id
    print(f"Imported {len(grp_rows)} modifier groups")

    # ── Modifier options (distinct per group) ─────────────────────────────
    sc.execute(
        "SELECT DISTINCT MODIFIERGROUPID, MODIFIERID, NAME, PRICE "
        "FROM MODIFIERMERCHANT WHERE MERCHANTID=%s AND MODIFIERGROUPID IS NOT NULL "
        "ORDER BY MODIFIERGROUPID, NAME", (SRC_MERCHANT_ID,))
    opt_rows = sc.fetchall()
    seen_opts = set()  # (group_id, name_lower) — dedupe
    for row in opt_rows:
        gid = grp_id_map.get(row['MODIFIERGROUPID'])
        if not gid:
            continue
        key = (gid, (row['NAME'] or '').lower().strip())
        if key in seen_opts:
            continue
        seen_opts.add(key)
        opt = MenuModifierOption(
            group_id    = gid,
            name        = (row['NAME'] or '').strip(),
            price_cents = int(row['PRICE'] or 0),
            external_id = row['MODIFIERID'],
        )
        db.add(opt)
    db.flush()
    print(f"Imported {len(seen_opts)} unique modifier options")

    # ── Item ↔ ModifierGroup links ────────────────────────────────────────
    sc.execute(
        "SELECT DISTINCT INVENTORYID, MODIFIERGROUPID FROM MODIFIERMERCHANT "
        "WHERE MERCHANTID=%s AND INVENTORYID IS NOT NULL AND MODIFIERGROUPID IS NOT NULL",
        (SRC_MERCHANT_ID,))
    link_rows = sc.fetchall()
    n_links = 0
    for row in link_rows:
        iid = item_id_map.get(row['INVENTORYID'])
        gid = grp_id_map.get(row['MODIFIERGROUPID'])
        if iid and gid:
            db.add(MenuItemModifierGroup(item_id=iid, group_id=gid))
            n_links += 1
    db.commit()
    print(f"Linked {n_links} item ↔ modifier-group rows")

    print("\n=== SUMMARY ===")
    print(f"Restaurant: {TARGET_RESTAURANT_NAME} (id={TARGET_RESTAURANT_ID})")
    print(f"Categories: {len(cat_rows)}, Items: {len(item_id_map)}, "
          f"Mod groups: {len(grp_rows)}, Mod options: {len(seen_opts)}, "
          f"Item-Group links: {n_links}")

    db.close()
    src.close()


if __name__ == '__main__':
    main()

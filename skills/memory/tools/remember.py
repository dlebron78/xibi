import sqlite3
import uuid
import os
from pathlib import Path

def run(params):
    """Store an item in the memory ledger."""
    content = params.get("content")
    category = params.get("category", "note")
    entity = params.get("entity")
    status = params.get("status")
    due = params.get("due")
    notes = params.get("notes")
    
    workdir = params.get("_workdir") or os.environ.get("BREGGER_WORKDIR", os.path.expanduser("~/.bregger"))
    db_path = Path(workdir) / "data" / "bregger.db"
    
    if not db_path.exists():
        return {"status": "error", "message": f"Database not found at {db_path}"}
    
    item_id = str(uuid.uuid4())
    
    
    try:
        with sqlite3.connect(db_path) as conn:
            if category in ["preference", "fact", "contact", "interest"]:
                key = entity if entity else content[:50]
                # Invalidate any existing current record for this key before inserting the new one
                conn.execute('''
                    UPDATE beliefs 
                    SET valid_until = CURRENT_TIMESTAMP 
                    WHERE key = ? AND valid_until IS NULL
                ''', (key,))
                
                # Insert the new record (valid_until defaults to NULL)
                conn.execute('''
                    INSERT INTO beliefs (key, value, type, visibility)
                    VALUES (?, ?, ?, ?)
                ''', (key, content, "passive_memory", "user"))
            else:
                # Determine decay_days based on category
                decay_mapping = {
                    "deadline": 7,
                    "commitment": 30,
                    "event": 14,
                    # Others like "task", "note", "triage_rule" are NULL (permanent)
                }
                decay_days = decay_mapping.get(category)
                
                conn.execute('''
                    INSERT INTO ledger (id, category, content, entity, status, due, notes, decay_days)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ''', (item_id, category, content, entity, status, due, notes, decay_days))
            conn.commit()
        
        label = f"{category}: {content[:50]}"
        if entity:
            label += f" ({entity})"
            
        return {
            "status": "success",
            "message": f"Got it. Remembered: {label}",
            "id": item_id
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}

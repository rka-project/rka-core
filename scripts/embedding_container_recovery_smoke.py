"""Offline recovery smoke against the installed image, no model/downloads."""

import asyncio
import json
from pathlib import Path

from rka.infra.database import Database
from rka.services.embedding_config import DEFAULT_CONFIG, EmbeddingConfigService
from rka.services.embedding_index import embedding_space_signature, reconcile_embedding_index
from rka.services.embedding_inspection import inspect_embedding_index
from rka.services.embedding_recovery import recover_embedding_index


async def main():
    folder = Path("/data/recovery-image-smoke")
    if folder.exists():
        raise RuntimeError("requires disposable empty /data")
    folder.mkdir()
    db = Database(str(folder / "rka.db"))
    await db.connect()
    try:
        await db.initialize_schema()
        await db.initialize_phase2_schema()
        assert db.vec_available
        await db.execute("INSERT INTO journal (id,type,content,source,project_id) VALUES ('jrn_smoke', 'note', 'synthetic', 'pi', 'proj_default')")
        EmbeddingConfigService(folder).save_config(DEFAULT_CONFIG, "system")
        await reconcile_embedding_index(db, space_signature=embedding_space_signature(DEFAULT_CONFIG), model_name=DEFAULT_CONFIG.config["model_name"], dim=768)
    finally:
        await db.close()
    report = await inspect_embedding_index(data_dir=folder)
    assert report["assessment"]["complete"] and report["vectors_available"]
    candidate = folder / "candidate.json"
    candidate.write_text(json.dumps({"backend": "fastembed", "config": {"model_name": "BAAI/bge-small-en-v1.5", "dim": 384}}))
    result = await recover_embedding_index(data_dir=folder, target_config=candidate, peers_stopped=True)
    assert result["status"] == "prepared"
    assert (Path(result["backup_directory"]) / "database.sqlite").is_file()
    report = await inspect_embedding_index(data_dir=folder)
    assert report["generation"]["dimensions"] == 384
    assert report["totals"]["source_rows"] == 1
    assert all(table["dimensions"] == 384 for table in report["physical_tables"].values())
    print("Installed image embedding inspection/admission/backup/offline transition passed; no inference.")


if __name__ == "__main__":
    asyncio.run(main())

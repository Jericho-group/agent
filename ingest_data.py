"""
Скрипт для загрузки базы знаний в ChromaDB.

Запуск:
  python ingest_data.py                        # загрузить sample_knowledge.json
  python ingest_data.py --file ./data/my.json  # загрузить свой файл
  python ingest_data.py --reset                # очистить и перезагрузить

Формат JSON файла — список объектов:
  [
    {
      "id": "unique_id",          (обязательно)
      "category": "features",     (обязательно: features/pricing/howto/faq/sales_scripts)
      "title": "Название",        (обязательно)
      "content": "Текст..."       (обязательно)
    },
    ...
  ]
"""

import argparse
import json
import sys
from pathlib import Path


def load_and_ingest(file_path: str, reset: bool = False) -> int:
    """
    Читает JSON файл, разбивает на чанки и загружает в ChromaDB.
    Возвращает количество загруженных документов.
    """
    from knowledge.vector_store import VectorStore

    path = Path(file_path)
    if not path.exists():
        print(f"[ERROR] File not found: {file_path}")
        sys.exit(1)

    with open(path, encoding="utf-8") as f:
        data = json.load(f)

    if not isinstance(data, list):
        print("[ERROR] JSON must be a list of objects")
        sys.exit(1)

    store = VectorStore()

    if reset:
        print("[ingest] Clearing existing collection...")
        store.delete_all()

    ids: list[str] = []
    documents: list[str] = []
    metadatas: list[dict] = []

    for item in data:
        if not all(k in item for k in ("id", "category", "title", "content")):
            print(f"[WARN] Skipping item missing required fields: {item.get('id', '?')}")
            continue

        # Формируем текст для embedding: title + content
        doc_text = f"{item['title']}\n\n{item['content']}"

        ids.append(item["id"])
        documents.append(doc_text)
        metadatas.append({
            "category": item["category"],
            "title": item["title"],
            "source": str(path.name),
        })

    if not ids:
        print("[ERROR] No valid documents found in file")
        sys.exit(1)

    print(f"[ingest] Uploading {len(ids)} documents to ChromaDB...")
    store.upsert(ids=ids, documents=documents, metadatas=metadatas)

    total = store.count()
    print(f"[ingest] Done. Total documents in KB: {total}")
    return total


def main():
    parser = argparse.ArgumentParser(description="Ingest knowledge base into ChromaDB")
    parser.add_argument(
        "--file",
        default="./data/sample_knowledge.json",
        help="Path to JSON knowledge base file",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Clear existing collection before ingesting",
    )
    args = parser.parse_args()
    load_and_ingest(args.file, args.reset)


if __name__ == "__main__":
    main()

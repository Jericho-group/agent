"""CLI-бутстрап: создать нового PRM-партнёра и выпустить API-ключ.

Запуск внутри контейнера chatbot_app:
    docker exec chatbot_app python prm_bootstrap.py \\
        --name "PRM Online" \\
        --email "dev@prmonline.ru" \\
        --origin "https://cabinet.prmonline.ru" \\
        --origin "https://dev.prmonline.ru"

Ключ выводится в stdout ОДИН РАЗ. Сохранить в 1Password/защищённый канал заказчика.
Повторно достать ключ нельзя (в БД bcrypt-hash).
"""

from __future__ import annotations

import argparse
import asyncio
import secrets
import sys

sys.path.insert(0, "/app")

from prm_iframe import _gen_api_key, _hash_key


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", required=True, help="Название партнёра (например 'PRM Online')")
    parser.add_argument("--email", required=True, help="Ответственный разработчик")
    parser.add_argument("--origin", action="append", required=True,
                        help="Разрешённый домен для iframe (можно несколько раз)")
    parser.add_argument("--webhook-url", default=None, help="URL для v2 webhook")
    args = parser.parse_args()

    from memory.dialogue_memory import _get_pool as _b404_pool
    pool = await _b404_pool()

    # Создаём партнёра
    partner_id = await pool.fetchval(
        """INSERT INTO prm_partners (name, contact_email, allowed_origins, webhook_url,
                                     default_plan_id)
           VALUES ($1, $2, $3, $4,
                   (SELECT id FROM plans WHERE code='trial'))
           RETURNING id""",
        args.name, args.email, args.origin, args.webhook_url,
    )

    # Генерируем ключ
    full_key, prefix = _gen_api_key()
    webhook_secret = "whs_" + secrets.token_hex(16)  # для v2 HMAC входящих
    key_hash = _hash_key(full_key)

    await pool.execute(
        """INSERT INTO prm_partner_api_keys (partner_id, key_prefix, key_hash, status)
           VALUES ($1, $2, $3, 'active')""",
        partner_id, prefix, key_hash,
    )
    await pool.execute(
        "UPDATE prm_partners SET webhook_secret=$1 WHERE id=$2",
        webhook_secret, partner_id,
    )

    print("=" * 70)
    print(f"Партнёр создан: id={partner_id}, name={args.name!r}")
    print(f"Разрешённые домены: {args.origin}")
    print()
    print("API-KEY (сохранить ТОЛЬКО ОДИН РАЗ, вывести в 1Password и передать защищённо):")
    print(f"  {full_key}")
    print()
    print(f"Webhook-secret (для валидации входящих от нас):")
    print(f"  {webhook_secret}")
    print()
    print("Использование клиентом:")
    print(f"  curl -H 'Authorization: Bearer {full_key}' \\\\")
    print("       https://admin.dirizher404.ru/prm/api/partners")
    print("=" * 70)


if __name__ == "__main__":
    asyncio.run(main())

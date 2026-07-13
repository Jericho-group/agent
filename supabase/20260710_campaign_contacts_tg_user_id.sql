-- Проактивность / TG-кампании: колонка для tg_user_id клиента.
-- Код (main.py:1318,1329,1335 и proactive/campaign_manager.py:317,376) обращается к
-- campaign_contacts.tg_user_id, но при накатке фичи миграцию не добавили —
-- в результате обработчик входящих сообщений TG-профиля падал с
-- "column cc.tg_user_id does not exist", бот НЕ подключался к диалогу
-- и «взять управление / отправить» отдавало 500.
ALTER TABLE campaign_contacts ADD COLUMN IF NOT EXISTS tg_user_id BIGINT;
CREATE INDEX IF NOT EXISTS ix_campaign_contacts_tg_user_id
  ON campaign_contacts(tg_user_id) WHERE tg_user_id IS NOT NULL;

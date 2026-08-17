-- ─────────────────────────────────────────────────────────────────
-- Триггеры для авто-записи событий в integration_events.
-- Заря (parent_integration_id IS NULL) не задействуется — триггер
-- проверяет NULL и пропускает.
-- ─────────────────────────────────────────────────────────────────

CREATE OR REPLACE FUNCTION trg_lead_to_event() RETURNS TRIGGER AS $$
DECLARE v_iid INT;
BEGIN
  SELECT parent_integration_id INTO v_iid FROM tenants WHERE id = NEW.tenant_id;
  IF v_iid IS NOT NULL THEN
    INSERT INTO integration_events (integration_id, tenant_id, actor_id, actor_type, event_type, payload)
    VALUES (
      v_iid, NEW.tenant_id, NEW.assigned_contact_id,
      CASE WHEN NEW.assigned_contact_id IS NOT NULL THEN 'contact' ELSE NULL END,
      'lead_created',
      jsonb_build_object(
        'lead_id',    NEW.id,
        'session_id', NEW.session_id,
        'phone',      NEW.phone,
        'status',     NEW.status
      )
    );
  END IF;
  RETURN NEW;
END; $$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS lead_to_event ON bot_404_leads;
CREATE TRIGGER lead_to_event AFTER INSERT ON bot_404_leads
  FOR EACH ROW EXECUTE FUNCTION trg_lead_to_event();


CREATE OR REPLACE FUNCTION trg_lead_status_to_event() RETURNS TRIGGER AS $$
DECLARE v_iid INT;
BEGIN
  IF OLD.status IS DISTINCT FROM NEW.status THEN
    SELECT parent_integration_id INTO v_iid FROM tenants WHERE id = NEW.tenant_id;
    IF v_iid IS NOT NULL THEN
      INSERT INTO integration_events (integration_id, tenant_id, actor_id, actor_type, event_type, payload)
      VALUES (
        v_iid, NEW.tenant_id, NEW.assigned_contact_id,
        CASE WHEN NEW.assigned_contact_id IS NOT NULL THEN 'contact' ELSE NULL END,
        'lead_status_changed',
        jsonb_build_object('lead_id', NEW.id, 'old_status', OLD.status, 'new_status', NEW.status)
      );
    END IF;
  END IF;
  RETURN NEW;
END; $$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS lead_status_to_event ON bot_404_leads;
CREATE TRIGGER lead_status_to_event AFTER UPDATE ON bot_404_leads
  FOR EACH ROW EXECUTE FUNCTION trg_lead_status_to_event();

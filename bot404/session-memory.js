/**
 * Session memory: extract facts из диалога + инжект в systemPrompt.
 *
 * Гибрид (вариант 3+2):
 *  - Структурированные слоты (volume, avg_check, industry, CRM, и т.д.)
 *    извлекаются через LLM-проход в JSON, мерж только новых/непустых полей.
 *  - last_summary — раз в 5 in-сообщений сжимается LLM до 1-2 строк.
 *
 * extractFacts(pool, sid, tid, history, latestUserMsg) — async, в фоне
 *   после ответа клиенту (не блокирует).
 *
 * loadFacts(pool, sid) — fetch текущих фактов, для inject в systemPrompt.
 * buildFactsBlock(facts) — формирует блок для системного промпта.
 */

import https from 'node:https';

const AIKEY = process.env.BOT_LLM_KEY || process.env.AITUNNEL_API_KEY || '';
const FACTS_MODEL = process.env.BOT_LLM_MODEL_FACTS || 'gemini-2.5-flash-lite';
const tlsAgent = new https.Agent({ rejectUnauthorized: true });

const EXTRACT_PROMPT_TEMPLATE = `Ты — экстрактор фактов из диалога клиент↔менеджер.
Получаешь последние сообщения и текущее состояние "что мы знаем о клиенте".
Твоя задача — заметить НОВЫЕ факты из последнего сообщения клиента и вернуть JSON ТОЛЬКО с теми полями, что появились/изменились.

Что мы знаем сейчас о клиенте:
__CURRENT_STATE__

Последние сообщения (последнее — клиента):
__RECENT_DIALOG__

ИЗВЛЕКИ ТОЛЬКО если КЛИЕНТ сам сказал. Не выдумывай. Не предполагай.

Возможные поля (только заполненные включай в ответ):
- industry (string): отрасль/ниша словами. "недвижимость", "EdTech", "МФО", "интернет-магазин", "автодилер".
- team_size (int): количество менеджеров в команде.
- volume_per_day (int): сколько звонков/лидов/обращений в день.
- avg_check_rub (number): средний чек в рублях (если в тысячах — переведи: "200 тысяч" → 200000).
- current_crm (string): какая CRM ("amoCRM", "Битрикс24", "нет CRM", "Excel").
- current_telephony (string): телефония ("Mango Office", "Sipuni", "своя АТС").
- budget_rub (number): озвученный бюджет в рублях.
- decision_role (string): "РОП", "директор", "CEO", "маркетолог", "владелец".
- product_interest (string): "echolytics", "orchestra", "phonex", "coach", "несколько".
- demo_promised (boolean): соглашался ли клиент на демо/пилот в этом диалоге.
- contact_email (string): email если назвал.
- contact_phone (string): телефон если назвал.
- contact_telegram (string): @username если назвал.
- new_pains (array of strings): новые боли, кратко 2-4 слова каждое. ["менеджеры сливают лиды", "конверсия упала"].
- new_objections (array of strings): новые возражения. ["дорого", "у нас уже Битрикс"].

ВАЖНО: поля industry/team_size/volume_per_day/avg_check_rub/current_crm/current_telephony/budget_rub/decision_role/product_interest — ТОЛЬКО для B2B-продаж бизнесу. Если клиент физлицо и речь о личных долгах, банкротстве, услуге гражданину — эти поля НЕ заполняй вообще. Доход, зарплату, сумму долга клиента НИКОГДА не пиши в volume_per_day, avg_check_rub, budget_rub или contact_email. Для таких диалогов заполняй только new_pains, new_objections и contact_phone/email/telegram.

Если в последнем сообщении НИЧЕГО нового — верни {}.
ОТВЕТ строго JSON, без преамбулы.`;

function buildCurrentState(facts) {
  if (!facts) return '(ничего не известно)';
  const parts = [];
  if (facts.industry) parts.push('отрасль: ' + facts.industry);
  if (facts.team_size) parts.push('команда: ' + facts.team_size + ' менеджеров');
  if (facts.volume_per_day) parts.push('объём: ' + facts.volume_per_day + '/день');
  if (facts.avg_check_rub) parts.push('чек: ' + Number(facts.avg_check_rub).toLocaleString('ru-RU') + ' ₽');
  if (facts.current_crm) parts.push('CRM: ' + facts.current_crm);
  if (facts.current_telephony) parts.push('телефония: ' + facts.current_telephony);
  if (facts.budget_rub) parts.push('бюджет: ' + Number(facts.budget_rub).toLocaleString('ru-RU') + ' ₽');
  if (facts.decision_role) parts.push('роль: ' + facts.decision_role);
  if (facts.product_interest) parts.push('интерес: ' + facts.product_interest);
  if (facts.demo_promised) parts.push('демо обещано: да');
  if (facts.contact_email) parts.push('email: ' + facts.contact_email);
  if (facts.contact_phone) parts.push('телефон: ' + facts.contact_phone);
  if (facts.contact_telegram) parts.push('tg: ' + facts.contact_telegram);
  if (facts.mentioned_pains && facts.mentioned_pains.length) parts.push('боли: ' + facts.mentioned_pains.join(', '));
  if (facts.mentioned_objections && facts.mentioned_objections.length) parts.push('возражения: ' + facts.mentioned_objections.join(', '));
  return parts.length ? parts.join('; ') : '(ничего не известно)';
}

function buildRecentDialog(history, latestUserMsg) {
  const lines = [];
  for (const m of (history || []).slice(-8)) {
    const role = m.role === 'user' ? 'Клиент' : 'Специалист';
    lines.push(role + ': ' + String(m.content || '').slice(0, 400));
  }
  lines.push('Клиент: ' + String(latestUserMsg || '').slice(0, 400));
  return lines.join('\n');
}

// Восстанавливает обрезанный JSON-объект от LLM (flash-lite часто рубит по max_tokens
// посреди строки или массива). Обрезаем до последнего целого поля, дозакрываем скобки.
function tryRepairJSON(s) {
  if (!s || typeof s !== 'string') return null;
  const start = s.indexOf('{');
  if (start < 0) return null;
  // Стриппим markdown-фенсы если есть
  let src = s.slice(start);
  const endFence = src.lastIndexOf('```');
  if (endFence > 0) src = src.slice(0, endFence);
  try { return JSON.parse(src); } catch {}
  // Идём по строке, считаем depth скобок и внутри-ли-строки; ищем последнее закрытие корневого объекта
  let depth = 0, inStr = false, esc = false, lastRootClose = -1;
  for (let i = 0; i < src.length; i++) {
    const ch = src[i];
    if (esc) { esc = false; continue; }
    if (inStr) {
      if (ch === '\\') esc = true;
      else if (ch === '"') inStr = false;
      continue;
    }
    if (ch === '"') { inStr = true; }
    else if (ch === '{' || ch === '[') depth++;
    else if (ch === '}' || ch === ']') { depth--; if (depth === 0 && ch === '}') lastRootClose = i; }
  }
  if (lastRootClose > 0) {
    try { return JSON.parse(src.slice(0, lastRootClose + 1)); } catch {}
  }
  // Обрезано — обрезаем до последней запятой перед последним ключом и дозакрываем
  let repair = src;
  if (inStr) {
    // закрываем висящую строку
    const lastQuote = repair.lastIndexOf('"');
    if (lastQuote > 0) repair = repair.slice(0, lastQuote); // отбрасываем неполное строковое значение
    // теперь надо срезать до последней ',' или '{'
  }
  const lastComma = repair.lastIndexOf(',');
  const lastOpenBrace = repair.lastIndexOf('{');
  const cutAt = Math.max(lastComma, lastOpenBrace);
  if (cutAt > 0) repair = repair.slice(0, cutAt);
  // подсчитаем сколько скобок надо дозакрыть
  let d = 0, iS = false, e2 = false;
  for (const ch of repair) {
    if (e2) { e2 = false; continue; }
    if (iS) { if (ch === '\\') e2 = true; else if (ch === '"') iS = false; continue; }
    if (ch === '"') iS = true;
    else if (ch === '{' || ch === '[') d++;
    else if (ch === '}' || ch === ']') d--;
  }
  if (d > 0) repair += '}'.repeat(d);
  try { return JSON.parse(repair); } catch { return null; }
}

async function callExtract(prompt) {
  return new Promise((resolve) => {
    const body = JSON.stringify({
      model: FACTS_MODEL,
      messages: [{ role: 'user', content: prompt }],
      temperature: 0,
      response_format: { type: 'json_object' },
      max_tokens: 800,
    });
    const opts = {
      method: 'POST',
      hostname: 'api.aitunnel.ru',
      port: 443,
      path: '/v1/chat/completions',
      headers: {
        'Content-Type': 'application/json',
        'Content-Length': Buffer.byteLength(body),
        'Authorization': 'Bearer ' + AIKEY,
      },
      agent: tlsAgent,
      timeout: 30000,
    };
    const req = https.request(opts, (res) => {
      res.setEncoding('utf8');
      let data = '';
      res.on('data', (c) => { data += c; });
      res.on('end', () => {
        let raw = '';
        try {
          const j = JSON.parse(data);
          raw = j && j.choices && j.choices[0] && j.choices[0].message && j.choices[0].message.content || '{}';
          resolve(JSON.parse(raw));
        } catch (e) {
          const repaired = tryRepairJSON(raw);
          if (repaired && typeof repaired === 'object') {
            resolve(repaired);
            return;
          }
          console.warn('[session-facts] parse fail:', String(e?.message || e).slice(0, 100), 'raw:', String(raw).slice(0, 300));
          resolve({});
        }
      });
    });
    req.on('error', (e) => { console.warn('[session-facts] http err:', e.message); resolve({}); });
    req.on('timeout', () => { console.warn('[session-facts] timeout 30s'); req.destroy(); resolve({}); });
    req.write(body);
    req.end();
  });
}

/** Загружает текущие факты из БД (для inject в prompt). */
export async function loadFacts(pool, sid) {
  try {
    const r = await pool.query("SELECT * FROM bot_404_session_facts WHERE session_id=$1", [sid]);
    return r.rows[0] || null;
  } catch { return null; }
}

/** Строит блок для systemPrompt. '' если данных нет. */
export function buildFactsBlock(facts) {
  if (!facts) return '';
  const state = buildCurrentState(facts);
  if (state === '(ничего не известно)') return '';
  let block = '\n\n=== ЗАПОМНЕННОЕ О КЛИЕНТЕ (точные факты из истории, цитируй цифры дословно — НЕ переспрашивай) ===\n' + state;
  if (facts.last_summary) {
    block += '\n— Контекст: ' + facts.last_summary;
  }
  block += '\nПРАВИЛА ИСПОЛЬЗОВАНИЯ ЭТОГО БЛОКА:\n'
        + '1. Если клиент возвращается (приветствие после прошлого разговора, «помните меня?», «это снова я») — ОБЯЗАТЕЛЬНО подтверди что помнишь и кратко напомни 2-3 ключевых факта (отрасль/объём/что обсуждали).\n'
        + '2. Не задавай вопросов на которые клиент уже ответил (нет повторного «сколько звонков», «какая отрасль», и т.п. если эти поля заполнены выше).\n'
        + '3. Двигайся ВПЕРЁД от того места где остановились в прошлый раз (см. Контекст).\n'
        + '=== /ЗАПОМНЕННОЕ ===';
  return block;
}

const SUMMARY_PROMPT_TEMPLATE = `Ты — стенографист диалога. Сожми диалог клиент↔специалист в 1-2 короткие строки (max 350 символов) на русском. НЕ используй имён собственных бота — пиши «специалист».
Что важно сохранить: о чём договорились / какие темы уже обсудили / какие возражения клиент уже снял / на каком этапе воронки находимся / какие обещания дал бот (например "пришлю КП на email").
Что не нужно: цифры (они в отдельных слотах), приветствия, шаблонные фразы.

Диалог:
__DIALOG__

ОТВЕТ только текст summary, без кавычек, без преамбулы.`;

async function callSummary(prompt) {
  return new Promise((resolve) => {
    const body = JSON.stringify({
      model: FACTS_MODEL,
      messages: [{ role: 'user', content: prompt }],
      temperature: 0.2,
      max_tokens: 200,
    });
    const opts = {
      method: 'POST',
      hostname: 'api.aitunnel.ru',
      port: 443,
      path: '/v1/chat/completions',
      headers: {
        'Content-Type': 'application/json',
        'Content-Length': Buffer.byteLength(body),
        'Authorization': 'Bearer ' + AIKEY,
      },
      agent: tlsAgent,
      timeout: 30000,
    };
    const req = https.request(opts, (res) => {
      res.setEncoding('utf8');
      let data = '';
      res.on('data', (c) => { data += c; });
      res.on('end', () => {
        try {
          const j = JSON.parse(data);
          const raw = j && j.choices && j.choices[0] && j.choices[0].message && j.choices[0].message.content || '';
          resolve(String(raw).trim().slice(0, 500));
        } catch { resolve(''); }
      });
    });
    req.on('error', (e) => { console.warn('[session-summary] http err:', e.message); resolve(''); });
    req.on('timeout', () => { req.destroy(); resolve(''); });
    req.write(body);
    req.end();
  });
}

/** Свободное summary истории. Триггер: каждые 5 in-сообщений (5, 10, 15...). */
export async function maybeUpdateSummary(pool, sid, tid, history) {
  if (!AIKEY) return;
  try {
    const inCount = (history || []).filter(m => m.role === 'user').length;
    if (inCount < 5) return; // первые 4 хода клиента — слотов достаточно
    // Проверяем когда последний раз делали summary
    const r = await pool.query("SELECT msg_count_at_last_extract FROM bot_404_session_facts WHERE session_id=$1", [sid]);
    const lastCount = r.rows[0]?.msg_count_at_last_extract || 0;
    if (inCount - lastCount < 5) return; // не дотянули до 5 новых in-сообщений

    const dialog = (history || []).slice(-12).map(m => {
      const who = m.role === 'user' ? 'Клиент' : 'Специалист';
      return who + ': ' + String(m.content || '').slice(0, 300);
    }).join('\n');

    const prompt = SUMMARY_PROMPT_TEMPLATE.replace('__DIALOG__', dialog);
    const summary = await callSummary(prompt);
    if (!summary || summary.length < 10) return;

    await pool.query(
      "INSERT INTO bot_404_session_facts(session_id, tenant_id, last_summary, last_summary_at, msg_count_at_last_extract, updated_at) " +
      "VALUES($1, $2, $3, now(), $4, now()) " +
      "ON CONFLICT (session_id) DO UPDATE SET last_summary=EXCLUDED.last_summary, last_summary_at=now(), msg_count_at_last_extract=EXCLUDED.msg_count_at_last_extract, updated_at=now()",
      [sid, tid, summary, inCount]
    );
    console.log('[session-summary] ' + sid + ' (at ' + inCount + ' in-msgs): ' + summary.slice(0, 80));
  } catch (e) {
    console.warn('[session-summary]', e && e.message || e);
  }
}

/**
 * Когда клиент в новой сессии оставил контакт (email/phone/@telegram) —
 * ищем самую свежую старую сессию с тем же контактом и копируем оттуда факты.
 * Текущие значения не перетираем — только заполняем пробелы (COALESCE).
 * last_summary дополняется пометкой что это объединение.
 *
 * Возвращает кол-во слотов, которые были подтянуты (0 если нечего мержить).
 */
export async function mergeFactsFromContact(pool, sid, tid, contact) {
  if (!contact || (!contact.phone && !contact.email && !contact.telegram)) return 0;
  try {
    // Найти session_id с тем же контактом, но не текущую
    const older = await pool.query(
      `SELECT DISTINCT l.session_id, COALESCE(f.updated_at, l.created_at) AS rank_at
         FROM bot_404_leads l
         LEFT JOIN bot_404_session_facts f ON f.session_id = l.session_id
        WHERE l.tenant_id = $1
          AND l.session_id <> $2
          AND ( ($3::text IS NOT NULL AND l.phone    = $3)
             OR ($4::text IS NOT NULL AND l.email    = $4)
             OR ($5::text IS NOT NULL AND l.telegram = $5) )
        ORDER BY rank_at DESC NULLS LAST
        LIMIT 5`,
      [tid, sid, contact.phone || null, contact.email || null, contact.telegram || null]
    );
    if (!older.rows.length) return 0;

    // Берём первую сессию с непустыми facts
    let donor = null;
    for (const row of older.rows) {
      const f = await pool.query("SELECT * FROM bot_404_session_facts WHERE session_id=$1", [row.session_id]);
      if (f.rows[0]) { donor = f.rows[0]; break; }
    }
    if (!donor) return 0;

    // Скалярные поля — копируем только если в текущей пусто
    const scalarCols = ['industry','team_size','volume_per_day','avg_check_rub','current_crm','current_telephony','budget_rub','decision_role','product_interest','contact_email','contact_phone','contact_telegram'];
    const setParts = [];
    const values = [sid, tid];
    let idx = 3;
    let copied = 0;
    for (const c of scalarCols) {
      if (donor[c] !== null && donor[c] !== undefined && donor[c] !== '') {
        setParts.push(`${c} = COALESCE(bot_404_session_facts.${c}, $${idx})`);
        values.push(donor[c]);
        idx++;
        copied++;
      }
    }
    // Массивы — объединяем
    if (Array.isArray(donor.mentioned_pains) && donor.mentioned_pains.length) {
      setParts.push(`mentioned_pains = array(SELECT DISTINCT unnest(coalesce(bot_404_session_facts.mentioned_pains,'{}'::text[]) || $${idx}::text[]))`);
      values.push(donor.mentioned_pains);
      idx++;
      copied++;
    }
    if (Array.isArray(donor.mentioned_objections) && donor.mentioned_objections.length) {
      setParts.push(`mentioned_objections = array(SELECT DISTINCT unnest(coalesce(bot_404_session_facts.mentioned_objections,'{}'::text[]) || $${idx}::text[]))`);
      values.push(donor.mentioned_objections);
      idx++;
      copied++;
    }
    // last_summary — берём из donor + пометка
    if (donor.last_summary) {
      const merged = '(объединено с прошлой сессией) ' + String(donor.last_summary).slice(0, 300);
      setParts.push(`last_summary = COALESCE(bot_404_session_facts.last_summary, $${idx})`);
      values.push(merged);
      idx++;
    }
    if (!copied) return 0;

    // INSERT-if-absent + UPDATE текущих пробелов
    const insertCols = ['session_id', 'tenant_id', 'updated_at'];
    const insertPh = ['$1', '$2', 'now()'];
    const insertValues = [sid, tid];
    let insIdx = 3;
    for (const c of scalarCols) {
      if (donor[c] !== null && donor[c] !== undefined && donor[c] !== '') {
        insertCols.push(c); insertPh.push('$' + insIdx); insertValues.push(donor[c]); insIdx++;
      }
    }
    if (Array.isArray(donor.mentioned_pains) && donor.mentioned_pains.length) {
      insertCols.push('mentioned_pains'); insertPh.push('$' + insIdx + '::text[]'); insertValues.push(donor.mentioned_pains); insIdx++;
    }
    if (Array.isArray(donor.mentioned_objections) && donor.mentioned_objections.length) {
      insertCols.push('mentioned_objections'); insertPh.push('$' + insIdx + '::text[]'); insertValues.push(donor.mentioned_objections); insIdx++;
    }
    if (donor.last_summary) {
      insertCols.push('last_summary'); insertPh.push('$' + insIdx); insertValues.push('(объединено с прошлой сессией) ' + String(donor.last_summary).slice(0, 300)); insIdx++;
    }

    const sql = `INSERT INTO bot_404_session_facts(${insertCols.join(', ')})
                 VALUES(${insertPh.join(', ')})
                 ON CONFLICT (session_id) DO UPDATE SET ${setParts.join(', ')}, updated_at = now()`;
    await pool.query(sql, insertValues);
    console.log(`[session-facts] merged ${copied} slots into ${sid} from donor ${donor.session_id}`);
    return copied;
  } catch (e) {
    console.warn('[session-facts] mergeFromContact:', e && e.message || e);
    return 0;
  }
}

/** Извлекает факты из последнего сообщения и мержит в БД. В фоне. */
export async function extractAndStoreFacts(pool, sid, tid, history, latestUserMsg) {
  if (!AIKEY || !latestUserMsg || latestUserMsg.length < 3) return;
  try {
    const current = await loadFacts(pool, sid);
    const prompt = EXTRACT_PROMPT_TEMPLATE
      .replace('__CURRENT_STATE__', buildCurrentState(current))
      .replace('__RECENT_DIALOG__', buildRecentDialog(history, latestUserMsg));
    const updates = await callExtract(prompt);
    if (!updates || typeof updates !== 'object' || Object.keys(updates).length === 0) return;

    const cols = ['industry','team_size','volume_per_day','avg_check_rub','current_crm','current_telephony','budget_rub','decision_role','product_interest','demo_promised','contact_email','contact_phone','contact_telegram'];
    const insertCols = ['session_id', 'tenant_id', 'updated_at'];
    const insertPh   = ['$1', '$2', 'now()'];
    const updateSet  = ['updated_at = now()'];
    const values     = [sid, tid];
    let idx = 3;
    for (const c of cols) {
      if (updates[c] !== undefined && updates[c] !== null && updates[c] !== '') {
        insertCols.push(c);
        insertPh.push('$' + idx);
        updateSet.push(c + ' = $' + idx);
        values.push(updates[c]);
        idx++;
      }
    }
    if (Array.isArray(updates.new_pains) && updates.new_pains.length) {
      insertCols.push('mentioned_pains');
      insertPh.push('$' + idx + '::text[]');
      updateSet.push("mentioned_pains = array(SELECT DISTINCT unnest(coalesce(bot_404_session_facts.mentioned_pains,'{}'::text[]) || $" + idx + "::text[]))");
      values.push(updates.new_pains);
      idx++;
    }
    if (Array.isArray(updates.new_objections) && updates.new_objections.length) {
      insertCols.push('mentioned_objections');
      insertPh.push('$' + idx + '::text[]');
      updateSet.push("mentioned_objections = array(SELECT DISTINCT unnest(coalesce(bot_404_session_facts.mentioned_objections,'{}'::text[]) || $" + idx + "::text[]))");
      values.push(updates.new_objections);
      idx++;
    }
    if (updateSet.length === 1) return; // только updated_at

    const sql = "INSERT INTO bot_404_session_facts(" + insertCols.join(', ') + ") VALUES(" + insertPh.join(', ') + ") ON CONFLICT (session_id) DO UPDATE SET " + updateSet.join(', ');
    await pool.query(sql, values);
    console.log('[session-facts] ' + sid + ': ' + Object.keys(updates).join(','));
  } catch (e) {
    console.warn('[session-facts]', e && e.message || e);
  }
}

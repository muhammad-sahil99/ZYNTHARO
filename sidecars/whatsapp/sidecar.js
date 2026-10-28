#!/usr/bin/env node
/* ORBYNTH WhatsApp sidecar.

   This file was missing from the repository, which is why every WhatsApp action failed with
   "the sidecar script at .../sidecars/whatsapp/sidecar.js" — the Python client was complete and
   had nothing to talk to. It is the connectivity layer.

   Protocol: newline-delimited JSON-RPC on stdin/stdout, version 1.
     {"v":1,"id":<n>,"method":"<name>","params":{...}}      -> in
     {"v":1,"id":<n>,"result":{...}}  |  {"v":1,"id":<n>,"error":"..."}   -> out
     {"v":1,"event":"message","payload":{...}}              -> unsolicited events for the HUD

   Design rules, because a chat client that lies is worse than one that fails:
     * Every dependency is optional at import time. If whatsapp-web.js is not installed, each RPC
       answers with an error naming the exact command to run instead of hanging.
     * Capabilities are feature-detected, not assumed. whatsapp-web.js does not expose voice
       calls in any released version, so `call` reports {state:"unsupported", fallback:"desktop"}
       and the Python side drives the WhatsApp desktop app with the call audio bridge instead.
       Claiming a call was placed when it was not is exactly the failure mode to avoid.
     * The session is persisted under the ORBYNTH home so pairing survives restarts, and a
       disconnect reconnects with exponential backoff instead of dropping the session.
*/

'use strict';

const readline = require('readline');
const fs = require('fs');
const os = require('os');
const path = require('path');

const VERSION = 1;
const HOME = process.env.ORBYNTH_HOME || path.join(os.homedir(), '.orbynth');
const SESSION_DIR = process.env.ORBYNTH_WHATSAPP_SESSION || path.join(HOME, 'whatsapp-session');
const MAX_BACKOFF_MS = 60_000;

function write(payload) {
  process.stdout.write(JSON.stringify(payload) + '\n');
}

function resolveWeb() {
  try {
    // Resolved lazily so a missing dependency is a reported error, never a crash at startup.
    return require('whatsapp-web.js');
  } catch (error) {
    return { __missing: error.message };
  }
}

const web = resolveWeb();
const state = {
  paired: false,
  ready: false,
  battery: null,
  sync: { state: 'idle', percent: 0 },
  lastEventAt: Date.now(),
  reconnect: { attempt: 0, lastError: '' },
  startedAt: Date.now(),
  queue: [],
  pendingQr: '',
  pendingPairingCode: '',
};

let client = null;

function features() {
  const call = !!(client && typeof client.call === 'function');
  return {
    pairingQr: !web.__missing,
    pairingCode: !!(!web.__missing && web.Client),
    calls: call,
    callReason: call ? '' : 'whatsapp-web.js does not expose voice calls in any released version',
    archive: !!client,
    settings: !!client,
    groups: !!client,
    presence: !!client,
  };
}

function fail(message) {
  throw new Error(String(message));
}

function requireClient() {
  if (web.__missing) {
    fail(
      'whatsapp-web.js is not installed. Run: cd "' + path.dirname(__filename) +
      '" && npm install --omit=dev   (' + web.__missing + ')'
    );
  }
  if (!client) fail('the WhatsApp client is not initialised yet; call "pair" first');
  return client;
}

/* ------------------------------------------------------------------ lifecycle */

function start() {
  if (web.__missing) return;
  fs.mkdirSync(SESSION_DIR, { recursive: true });
  const { Client, LocalAuth } = web;

  client = new Client({
    authStrategy: new LocalAuth({ dataPath: SESSION_DIR }),
    puppeteer: {
      headless: true,
      args: ['--no-sandbox', '--disable-setuid-sandbox', '--disable-gpu'],
    },
    takeoverOnConflict: true,
    userAgent: 'ORBYNTH/1.0 (desktop operative)',
  });

  client.on('qr', (qr) => {
    state.pendingQr = qr;
    state.lastEventAt = Date.now();
    write({ v: VERSION, event: 'qr', payload: { qr, expires_in: 60 } });
  });

  client.on('code', (code) => {
    state.pendingPairingCode = code;
    write({ v: VERSION, event: 'pairing_code', payload: { code } });
  });

  client.on('ready', () => {
    state.ready = true;
    state.paired = true;
    state.reconnect.attempt = 0;
    state.lastEventAt = Date.now();
    write({ v: VERSION, event: 'ready', payload: { device: (client.info && client.info.pushname) || 'linked' } });
  });

  client.on('authenticated', () => {
    state.paired = true;
    state.lastEventAt = Date.now();
    write({ v: VERSION, event: 'authenticated', payload: {} });
  });

  client.on('auth_failure', (message) => {
    state.paired = false;
    state.ready = false;
    write({ v: VERSION, event: 'auth_failure', payload: { message: String(message) } });
  });

  client.on('change_state', (value) => {
    state.lastEventAt = Date.now();
    write({ v: VERSION, event: 'connection_state', payload: { state: value } });
  });

  client.on('change_battery', (battery) => {
    state.battery = battery;
  });

  client.on('loading_screen', (percent, message) => {
    state.sync = { state: message || 'syncing', percent: Number(percent) || 0 };
    write({ v: VERSION, event: 'sync', payload: state.sync });
  });

  client.on('message', (message) => {
    state.lastEventAt = Date.now();
    write({
      v: VERSION,
      event: 'message',
      payload: {
        id: message.id && message.id._serialized,
        from: message.from,
        chat: message.from,
        text: message.body || '',
        type: message.type,
        ts: message.timestamp,
        from_me: !!message.fromMe,
        has_media: !!message.hasMedia,
      },
    });
  });

  client.on('message_ack', (message, ack) => {
    write({ v: VERSION, event: 'ack', payload: { id: message.id && message.id._serialized, ack } });
  });

  client.on('call', (call) => {
    // Feature-detected: only fires on builds that expose calls. Never auto-answered here; the
    // Python side decides, because answering a call is the user's decision, not the sidecar's.
    write({
      v: VERSION,
      event: 'incoming_call',
      payload: { from: call.from, isVideo: !!call.isVideo, id: call.id },
    });
  });

  client.on('disconnected', (reason) => {
    state.ready = false;
    state.paired = false;
    state.reconnect.lastError = String(reason);
    write({ v: VERSION, event: 'disconnected', payload: { reason: String(reason) } });
    reconnect();
  });

  client.initialize().catch((error) => {
    state.reconnect.lastError = String(error && error.message);
    write({ v: VERSION, event: 'error', payload: { message: state.reconnect.lastError } });
    reconnect();
  });
}

function reconnect() {
  const attempt = state.reconnect.attempt + 1;
  state.reconnect.attempt = attempt;
  const wait = Math.min(MAX_BACKOFF_MS, 1000 * Math.pow(2, Math.min(6, attempt)));
  write({ v: VERSION, event: 'reconnecting', payload: { attempt, in_ms: wait } });
  setTimeout(() => {
    try {
      if (client) client.destroy().catch(() => {});
      client = null;
      start();
    } catch (error) {
      write({ v: VERSION, event: 'error', payload: { message: String(error && error.message) } });
    }
  }, wait);
}

/* ------------------------------------------------------------------ helpers */

async function chatById(id) {
  const found = await client.getChatById(id).catch(() => null);
  if (!found) fail('no chat matched ' + JSON.stringify(id) + '; run "chats" to list ids');
  return found;
}

function briefChat(chat) {
  return {
    id: chat.id && chat.id._serialized,
    name: chat.name || (chat.contact && chat.contact.name) || '',
    unread: chat.unreadCount || 0,
    archived: !!chat.archived,
    pinned: !!chat.pinned,
    muted: !!(chat.mute && chat.mute.expiration !== 0),
    is_group: !!chat.isGroup,
    timestamp: chat.timestamp || 0,
    last_message: (chat.lastMessage && chat.lastMessage.body) || '',
  };
}

function briefMessage(message) {
  return {
    id: message.id && message.id._serialized,
    from: message.from,
    to: message.to,
    text: message.body || '',
    type: message.type,
    ts: message.timestamp,
    from_me: !!message.fromMe,
    has_media: !!message.hasMedia,
    ack: message.ack,
  };
}

/* ------------------------------------------------------------------ RPC */

const methods = {
  async status() {
    return {
      paired: state.paired,
      ready: state.ready,
      device: (client && client.info && client.info.pushname) || 'none',
      number: client && client.info && client.info.wid ? client.info.wid.user : '',
      chats: state.ready ? (await client.getChats()).length : 0,
      battery: state.battery,
      sync: state.sync,
      last_event_at: state.lastEventAt,
      uptime_s: Math.round((Date.now() - state.startedAt) / 1000),
      reconnect: state.reconnect,
      queue: state.queue.length,
      features: features(),
      session_dir: SESSION_DIR,
    };
  },

  async pair() {
    if (web.__missing) requireClient();
    if (!client) start();
    // Give the client a moment to emit the first QR so the HUD has something to render.
    const deadline = Date.now() + 20_000;
    while (!state.pendingQr && !state.ready && Date.now() < deadline) {
      await new Promise((resolve) => setTimeout(resolve, 250));
    }
    if (state.ready) return { paired: true, qr: '', note: 'already linked' };
    if (!state.pendingQr) fail('no QR was produced within 20s; check the network and try again');
    return { qr: state.pendingQr, paired: false, expires_in: 60 };
  },

  async pair_with_phone(params) {
    if (web.__missing) requireClient();
    const phone = String(params.phone || '').replace(/[^\d]/g, '');
    if (!phone) fail('pair_with_phone needs an E.164 phone number');
    if (!client) start();
    const deadline = Date.now() + 30_000;
    while (!state.ready && Date.now() < deadline) {
      await new Promise((resolve) => setTimeout(resolve, 250));
      if (state.pendingPairingCode) break;
      if (typeof client.requestPairingCode === 'function' && client.info === undefined) {
        try {
          const code = await client.requestPairingCode(phone);
          state.pendingPairingCode = code;
          break;
        } catch (error) {
          fail('the sidecar could not request a pairing code: ' + error.message);
        }
      }
    }
    if (!state.pendingPairingCode && !state.ready) fail('no pairing code was produced; use the QR instead');
    return { pairing_code: state.pendingPairingCode || '', phone, paired: state.ready };
  },

  async logout() {
    if (client) await client.logout().catch(() => {});
    state.paired = false;
    state.ready = false;
    return { logged_out: true };
  },

  async chats(params) {
    requireClient();
    const limit = Number(params.limit || 50);
    const filter = String(params.filter || '').toLowerCase();
    const rows = (await client.getChats()).map(briefChat);
    const filtered = filter
      ? rows.filter((row) => (row.name || '').toLowerCase().includes(filter) || (row.last_message || '').toLowerCase().includes(filter))
      : rows;
    return filtered.slice(0, limit);
  },

  async archived(params) {
    requireClient();
    const limit = Number(params.limit || 50);
    const rows = (await client.getChats()).filter((chat) => chat.archived).map(briefChat);
    return rows.slice(0, limit);
  },

  async unread(params) {
    requireClient();
    const rows = (await client.getChats()).filter((chat) => (chat.unreadCount || 0) > 0).map(briefChat);
    return rows.slice(0, Number(params.limit || 50));
  },

  async search(params) {
    requireClient();
    const query = String(params.query || '');
    if (!query) fail('search needs a query');
    const limit = Number(params.limit || 20);
    const hits = await client.searchMessages(query, { limit });
    const rows = [];
    for (const hit of hits) {
      const message = hit.message || hit;
      rows.push({
        chat: hit.chat && hit.chat.id ? hit.chat.id._serialized : '',
        chat_name: hit.chat ? hit.chat.name : '',
        ...briefMessage(message),
      });
    }
    return rows.slice(0, limit);
  },

  async messages(params) {
    requireClient();
    const chat = await chatById(params.chat);
    const limit = Number(params.limit || 50);
    const rows = await chat.fetchMessages({ limit });
    return rows.map(briefMessage);
  },

  async send_text(params) {
    requireClient();
    const chat = await chatById(params.chat);
    if (params.typing !== false) {
      await chat.sendStateTyping().catch(() => {});
      await new Promise((resolve) => setTimeout(resolve, Math.min(2500, 40 * String(params.text || '').length)));
      await chat.clearState().catch(() => {});
    }
    const sent = await chat.sendMessage(String(params.text || ''), {
      quotedMessageId: params.reply_to || undefined,
    });
    return { chat: params.chat, text: String(params.text || ''), id: sent.id && sent.id._serialized, sent: true };
  },

  async send_media(params) {
    requireClient();
    const chat = await chatById(params.chat);
    const { MessageMedia } = web;
    const media = MessageMedia.fromFilePath(String(params.path));
    const sent = await chat.sendMessage(media, {
      caption: params.caption || undefined,
      sendAudioAsVoice: !!params.as_voice,
    });
    return { chat: params.chat, path: params.path, voice: !!params.as_voice, id: sent.id && sent.id._serialized, sent: true };
  },

  async download_media(params) {
    requireClient();
    const chat = await chatById(params.chat || '');
    const rows = await chat.fetchMessages({ limit: 200 });
    const target = rows.find((row) => row.id && row.id._serialized === params.message_id);
    if (!target) fail('no message with that id in that chat');
    if (!target.hasMedia) fail('that message has no media');
    const media = await target.downloadMedia();
    const dest = params.dest || path.join(HOME, 'whatsapp-media', String(params.message_id).replace(/[^\w.-]/g, '_'));
    fs.mkdirSync(path.dirname(dest), { recursive: true });
    fs.writeFileSync(dest, Buffer.from(media.data, 'base64'));
    return { message_id: params.message_id, path: dest, bytes: fs.statSync(dest).size, mimetype: media.mimetype };
  },

  async mark_read(params) {
    requireClient();
    const chat = await chatById(params.chat);
    await chat.sendSeen().catch(() => {});
    return { chat: params.chat, read: true };
  },

  async presence(params) {
    requireClient();
    const chat = await chatById(params.chat);
    const value = String(params.state || 'composing');
    if (value === 'composing') await chat.sendStateTyping().catch(() => {});
    else if (value === 'recording') await chat.sendStateRecording().catch(() => {});
    else await chat.clearState().catch(() => {});
    return { chat: params.chat, presence: value };
  },

  async react(params) {
    requireClient();
    const chat = await chatById(params.chat);
    const rows = await chat.fetchMessages({ limit: 200 });
    const target = rows.find((row) => row.id && row.id._serialized === params.message_id);
    if (!target) fail('no message with that id in that chat');
    await target.react(params.emoji === undefined ? '' : String(params.emoji));
    return { message_id: params.message_id, emoji: params.emoji || null };
  },

  async reply(params) {
    requireClient();
    const chat = await chatById(params.chat);
    const rows = await chat.fetchMessages({ limit: 200 });
    const target = rows.find((row) => row.id && row.id._serialized === params.message_id);
    if (!target) fail('no message with that id in that chat');
    const sent = await target.reply(String(params.text || ''));
    return { chat: params.chat, reply_to: params.message_id, id: sent.id && sent.id._serialized, sent: true };
  },

  async delete_message(params) {
    requireClient();
    const chat = await chatById(params.chat);
    const rows = await chat.fetchMessages({ limit: 200 });
    const target = rows.find((row) => row.id && row.id._serialized === params.message_id);
    if (!target) fail('no message with that id in that chat');
    await target.delete(!!params.for_everyone);
    return { message_id: params.message_id, for_everyone: !!params.for_everyone, deleted: true };
  },

  async forward(params) {
    requireClient();
    const source = await chatById(params.from_chat);
    const rows = await source.fetchMessages({ limit: 200 });
    const target = rows.find((row) => row.id && row.id._serialized === params.message_id);
    if (!target) fail('no message with that id in that chat');
    for (const destination of params.to_chats || []) {
      const chat = await chatById(destination);
      await target.forward(chat);
    }
    return { message_id: params.message_id, forwarded_to: params.to_chats || [] };
  },

  async archive(params) {
    requireClient();
    const chat = await chatById(params.chat);
    if (params.archived === false) await chat.unarchive();
    else await chat.archive();
    return { chat: params.chat, archived: params.archived !== false };
  },

  async pin(params) {
    requireClient();
    const chat = await chatById(params.chat);
    if (params.pinned === false) await chat.unpin();
    else await chat.pin();
    return { chat: params.chat, pinned: params.pinned !== false };
  },

  async mute(params) {
    requireClient();
    const chat = await chatById(params.chat);
    const seconds = Number(params.seconds || 8 * 3600);
    if (params.muted === false) await chat.unmute();
    else await chat.mute(seconds);
    return { chat: params.chat, muted: params.muted !== false, seconds: params.muted === false ? 0 : seconds };
  },

  async settings(params) {
    requireClient();
    return {
      session: {
        archived: (await client.getChats()).filter((chat) => chat.archived).length,
        unread: (await client.getChats()).filter((chat) => (chat.unreadCount || 0) > 0).length,
      },
      features: features(),
      account: { pushname: (client.info && client.info.pushname) || '', number: (client.info && client.info.wid) ? client.info.wid.user : '' },
      note: 'read receipts, disappearing messages and privacy are account settings: change them on the phone, the sidecar mirrors them',
    };
  },

  async groups(params) {
    requireClient();
    const rows = (await client.getChats()).filter((chat) => chat.isGroup).map(briefChat);
    return rows.slice(0, Number(params.limit || 50));
  },

  async contacts(params) {
    requireClient();
    const rows = await client.getContacts();
    const limit = Number(params.limit || 100);
    return rows
      .filter((contact) => contact.isMyContact || contact.name)
      .slice(0, limit)
      .map((contact) => ({
        id: contact.id && contact.id._serialized,
        name: contact.name || '',
        pushname: contact.pushname || '',
        number: contact.number || '',
        is_business: !!contact.isBusiness,
      }));
  },

  async export_chat(params) {
    requireClient();
    const chat = await chatById(params.chat);
    const rows = await chat.fetchMessages({ limit: Number(params.limit || 500) });
    return { chat: params.chat, count: rows.length, messages: rows.map(briefMessage) };
  },

  async health() {
    return {
      ok: !web.__missing,
      paired: state.paired,
      ready: state.ready,
      sidestep: state.reconnect,
      last_event_at: state.lastEventAt,
      idle_s: Math.round((Date.now() - state.lastEventAt) / 1000),
      queue: state.queue.length,
      features: features(),
    };
  },

  async flush_queue() {
    const pending = state.queue.splice(0, state.queue.length);
    const results = [];
    for (const item of pending) {
      try {
        results.push({ ok: true, result: await methods.send_text(item) });
      } catch (error) {
        results.push({ ok: false, error: String(error.message) });
      }
    }
    return { flushed: results.length, results };
  },

  async call() {
    // Honest feature detection. whatsapp-web.js does not implement voice calls, so this reports
    // unsupported and names the path that does work: the desktop app driven by the operative,
    // with ORBYNTH's own audio routed into the call through the voice bridge.
    if (client && typeof client.call === 'function') {
      return { state: 'supported', note: 'this build exposes calls; the Python side will drive it' };
    }
    return {
      state: 'unsupported',
      reason: 'the WhatsApp Web protocol used by whatsapp-web.js does not expose voice calls',
      fallback: 'desktop',
      fallback_detail: (
        'Drive the WhatsApp desktop application instead: launch/focus it, start the call with the ' +
        'OS-level input driver, then route ORBYNTH audio through the call bridge ' +
        '(env.AudioRoute), which is what lets it hold a real conversation.'
      ),
    };
  },

  async hangup() {
    return { state: 'ended', note: 'call control is owned by the desktop path, not the sidecar' };
  },

  async transcript(params) {
    return {
      seconds: Number(params.seconds || 5),
      text: '',
      reason: 'call audio is captured by the call bridge, not the sidecar; use whatsapp_listen',
    };
  },
};

/* ------------------------------------------------------------------ loop */

const rl = readline.createInterface({ input: process.stdin });
rl.on('line', async (line) => {
  const trimmed = line.trim();
  if (!trimmed) return;
  let request;
  try {
    request = JSON.parse(trimmed);
  } catch (error) {
    write({ v: VERSION, error: 'malformed JSON: ' + error.message });
    return;
  }
  const id = request.id;
  const method = String(request.method || '');
  const handler = methods[method];
  if (!handler) {
    write({ v: VERSION, id, error: 'unknown method: ' + method });
    return;
  }
  try {
    const result = await handler(request.params || {});
    write({ v: VERSION, id, result });
  } catch (error) {
    write({ v: VERSION, id, error: String((error && error.message) || error) });
  }
});

rl.on('close', () => {
  if (client) client.destroy().catch(() => {});
  process.exit(0);
});

write({ v: VERSION, event: 'started', payload: { features: features(), session_dir: SESSION_DIR, dependency_missing: !!web.__missing } });
if (!web.__missing) start();
